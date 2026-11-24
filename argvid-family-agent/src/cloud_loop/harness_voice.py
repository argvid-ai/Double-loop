"""Wake gate for transcribed utterances; acoustic detection is a separate adapter.

The compatibility field ``accepted`` describes this gate only. ``cloud_accepted``
and ``status`` describe delivery. Neither enqueue nor receipt polling opens a
followup window; the local output adapter must acknowledge actual reply display.
"""

import json

from .contracts import canonical, digest
from .harness_contracts import (
    ForwardedVoice,
    VoiceReplyDelivery,
    VoiceUtterance,
    WakeConfiguration,
    validate_streaming_utterance,
)
from .harness_voice_forward import VoiceForwarder
from .harness_voice_notifications import VoiceNotifications
from .store import Conflict
from .voice_wake import match_wake


class VoiceGateway:
    def __init__(self, host):
        self.host, self.store = host, host.store
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS voice_ingress (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, adapter TEXT NOT NULL,
                    body TEXT NOT NULL, decision TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS voice_windows (
                    principal TEXT PRIMARY KEY, until REAL NOT NULL
                );
            """)
        self.forward = VoiceForwarder(host)
        self.notifications = VoiceNotifications(host)
        # Old releases opened these windows on enqueue, without delivery proof.
        # Retain new delivery-backed windows across restart, not legacy authority.
        with self.store.connect() as db:
            db.execute("""DELETE FROM voice_windows WHERE principal NOT IN
                (SELECT d.principal FROM voice_delivery_receipts d JOIN voice_ingress v ON v.id=d.id)""")

    def recover_pending(self, limit=16):
        """Bounded migration of pre-outbox ingress; never replay old edge execution."""
        if self.host.side != "edge":
            return []
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid voice recovery limit")
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM voice_ingress WHERE status='pending' ORDER BY rowid LIMIT ?",
                (limit,),
            ).fetchall()
        recovered = []
        for row in rows:
            utterance = VoiceUtterance.model_validate_json(row["body"])
            with self.store.connect() as db:
                accepted = db.execute(
                    "SELECT 1 FROM harness_events WHERE id=? AND principal=?",
                    (row["id"], row["principal"]),
                ).fetchone()
            if accepted:
                # A pre-0.5 local execution must not be executed a second time in cloud.
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE voice_ingress SET status='legacy_local' WHERE id=?", (row["id"],)
                    )
                status = "legacy_local"
            else:
                try:
                    status = self.ingest(utterance, row["principal"], adapter=row["adapter"])[
                        "status"
                    ]
                except Conflict:
                    with self.store.connect() as db:
                        db.execute(
                            "UPDATE voice_ingress SET status='rejected' WHERE id=?", (row["id"],)
                        )
                    status = "rejected"
            recovered.append({"event_id": row["id"], "status": status})
            self.store.audit(
                None,
                "voice.recovered",
                {"event_id": row["id"], "principal": row["principal"], "status": status},
            )
        return recovered

    def configuration(self, *, db=None):
        if db is None:
            with self.store.connect() as conn:
                return self.configuration(db=conn)
        row = db.execute("SELECT value FROM metadata WHERE key='voice_wake_config'").fetchone()
        return WakeConfiguration.model_validate_json(row["value"]) if row else WakeConfiguration()

    def configure(self, config):
        config = WakeConfiguration.model_validate(config.model_dump())
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('voice_wake_config',?)",
                (canonical(config),),
            )
            db.execute("DELETE FROM voice_windows")
            # Reapplying even identical settings resets input authority. A
            # wake-only prompt from the previous configuration must not revive
            # its one-use collection window just because the words match again.
            db.execute(
                """UPDATE voice_ingress SET decision=json_set(
                    decision,'$.collect_until',0,'$.collect_cancelled','configuration_changed')
                WHERE adapter='streaming-asr' AND json_extract(decision,'$.collect_until')>0"""
            )
            self.store._audit(
                db,
                None,
                "voice.configured",
                {"configuration": config.model_dump(), "acoustic_detector_installed": False},
            )
        return config.model_dump()

    def acknowledge_delivery(self, delivery: VoiceReplyDelivery, principal):
        """Local output calls this only after showing the exact reply to the user."""
        if self.host.side != "edge":
            raise Conflict("voice reply delivery is edge-owned")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            notice = self.notifications.acknowledge(db, principal, delivery)
            if notice is not None:
                return notice
            status = self._status(db, principal, delivery.event_id)
            if status.get("reply") is None or status.get("reply_digest") != delivery.reply_digest:
                raise Conflict("reply is not ready or delivery identity mismatches")
            old = db.execute(
                "SELECT * FROM voice_delivery_receipts WHERE id=?", (delivery.event_id,)
            ).fetchone()
            if old:
                if (old["principal"], old["reply_digest"]) != (principal, delivery.reply_digest):
                    raise Conflict("voice delivery identity collision")
                return {**status, "duplicate": True}
            now = self.store.clock()
            db.execute(
                "INSERT INTO voice_delivery_receipts VALUES (?,?,?,?)",
                (delivery.event_id, principal, delivery.reply_digest, now),
            )
            streaming_prompt = (
                status.get("adapter") == "streaming-asr"
                and status.get("reply", {}).get("local_ack") is True
            )
            scenario = db.execute("""SELECT 1 FROM voice_ingress WHERE id=?
                AND adapter='mock-transcript'
                AND json_extract(body,'$.asr.scenario_read_only')=1""",
                (delivery.event_id,)).fetchone()
            if status["channel"] == "voice" and not streaming_prompt and not scenario:
                db.execute(
                    "INSERT OR REPLACE INTO voice_windows VALUES (?,?)",
                    (principal, now + self.configuration(db=db).followup_seconds),
                )
            db.execute(
                "UPDATE voice_forward_outbox SET status='delivered' WHERE id=? AND status='reply_ready'",
                (delivery.event_id,),
            )
            db.execute(
                "UPDATE voice_ingress SET status='delivered' WHERE id=? AND status='reply_ready'",
                (delivery.event_id,),
            )
            return {**self._status(db, principal, delivery.event_id), "duplicate": False}

    def _status(self, db, principal, ident):
        row = db.execute(
            "SELECT * FROM voice_ingress WHERE id=? AND principal=?", (ident, principal)
        ).fetchone()
        if row is None:
            return self.forward.status(principal, ident, db=db)
        decision = json.loads(row["decision"])
        if row["status"] == "forwarded":
            return {**decision, **self.forward.status(principal, ident, db=db)}
        reply = decision.get("reply")
        delivered = db.execute(
            "SELECT delivered_at FROM voice_delivery_receipts WHERE id=?", (ident,)
        ).fetchone()
        text = (
            reply.get("text", "")
            if reply
            else {
                "ignored": "未唤醒或仅为媒体数据，未提交用户指令。",
                "expired": "这条语音在首次受理前已过期，未提交云端。",
                "rejected": "语音入口已不可用，未提交云端。",
                "legacy_local": "此输入已在旧版端侧受理，不重复提交云端。",
            }.get(row["status"], "")
        )
        return {
            **decision,
            "channel": "voice",
            "status": row["status"],
            "cloud_accepted": False,
            "reply_digest": digest(reply) if reply is not None else None,
            "delivered_at": delivered["delivered_at"] if delivered else None,
            "text": text,
            "audio_generated": False,
        }

    def status(self, principal, ident):
        with self.store.connect() as db:
            return self._status(db, principal, ident)

    @staticmethod
    def match(text, words):
        return match_wake(text, words)

    def ingest(
        self,
        utterance: VoiceUtterance,
        principal,
        *,
        adapter="mock-transcript",
        explicit_activation=False,
    ):
        if self.host.side != "edge" or "inputs" not in self.host.binding["capabilities"]:
            raise Conflict("voice input adapter unavailable")
        if adapter not in {"mock-transcript", "file-asr", "streaming-asr"}:
            raise Conflict("voice adapter unavailable")
        if adapter == "streaming-asr":
            try:
                proof = validate_streaming_utterance(utterance)
            except ValueError as exc:
                raise Conflict("streaming ASR provenance is invalid") from exc
            if (
                self.host.settings.voice_stream_sources.get(utterance.source_ref)
                != proof.source_kind
            ):
                raise Conflict("streaming voice source is not installed for this input kind")
            installed = (proof.source_simulated, "user_command")
        else:
            provider = "file-asr" if adapter == "file-asr" else "mock-user"
            installed = self.host.voice_sources.get(provider)
            if installed is None or installed[0] is not True:
                raise Conflict("voice source not installed or simulation provenance mismatch")
        if adapter == "file-asr" and (
            not utterance.audio_sha256
            or not utterance.asr.get("model")
            or utterance.replayed_at is None
            or utterance.asr.get("recognition_simulated") is True
        ):
            raise Conflict("file ASR requires hash, model provenance and replay timestamp")
        wire = canonical(utterance)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            now = self.store.clock()
            config = self.configuration(db=db)
            previous = db.execute(
                "SELECT * FROM voice_ingress WHERE id=?", (utterance.event_id,)
            ).fetchone()
            if previous:
                if (previous["principal"], previous["adapter"], previous["body"]) != (
                    principal,
                    adapter,
                    wire,
                ):
                    raise Conflict("voice ingress identity collision")
                decision = json.loads(previous["decision"])
            else:
                if (
                    now >= utterance.received_at.timestamp() + 60
                    or utterance.received_at.timestamp() > now + 30
                ):
                    raise Conflict("utterance receipt is stale or in the future")
                wake, text = self.match(utterance.transcript, config.wake_words)
                window = db.execute(
                    "SELECT until FROM voice_windows WHERE principal=?", (principal,)
                ).fetchone()
                scenario = adapter == "mock-transcript" and utterance.asr.get("scenario_read_only") is True
                followup = bool(window and now < window["until"] and not scenario)
                # A headless microphone can hear a wake word, a pause, then the
                # command. This is pre-command collection, not a fabricated
                # reply-delivery ACK or an indefinitely open followup window.
                collecting = None
                if adapter == "streaming-asr" and not wake and not followup:
                    collecting = db.execute(
                        """SELECT id,decision FROM voice_ingress WHERE principal=?
                        AND adapter='streaming-asr'
                        AND json_extract(body,'$.source_ref')=?
                        AND json_extract(decision,'$.collect_until')>?
                        AND json_extract(decision,'$.collect_config')=?
                        AND json_extract(decision,'$.collect_consumed')=0
                        ORDER BY rowid DESC LIMIT 1""",
                        (principal, utterance.source_ref, now, canonical(config)),
                    ).fetchone()
                media = utterance.purpose == "media_content" or installed[1] == "media_content"
                accepted = bool(wake or followup or collecting or explicit_activation) and not media
                reason = (
                    "media_data_only"
                    if media
                    else "wake_word"
                    if wake
                    else "push_to_talk"
                    if explicit_activation
                    else "followup_window"
                    if followup
                    else "wake_collection"
                    if collecting
                    else "no_wake"
                )
                decision = {
                    "event_id": utterance.event_id,
                    "accepted": accepted,
                    "reason": reason,
                    "wake_word": wake,
                    "gate_accepted": accepted,
                    "request_text": text if wake else utterance.transcript,
                    "adapter": adapter,
                    "simulated": utterance.asr.get("source_simulated", True)
                    or adapter == "mock-transcript",
                    "recognition_simulated": adapter == "mock-transcript",
                    "acoustic_wake_verified": False,
                    "received_at": utterance.received_at.isoformat(),
                }
                if accepted and collecting:
                    parent = json.loads(collecting["decision"])
                    parent["collect_consumed"] = True
                    db.execute(
                        "UPDATE voice_ingress SET decision=? WHERE id=?",
                        (canonical(parent), collecting["id"]),
                    )
                    decision["wake_event_id"] = collecting["id"]
                if accepted and wake and not text.strip() and adapter == "streaming-asr":
                    decision.update(
                        collect_until=now + config.followup_seconds,
                        collect_config=canonical(config),
                        collect_consumed=False,
                    )
                db.execute(
                    "INSERT INTO voice_ingress VALUES (?,?,?,?,?,?)",
                    (
                        utterance.event_id,
                        principal,
                        adapter,
                        wire,
                        canonical(decision),
                        "pending" if accepted else "ignored",
                    ),
                )
                self.store._audit(
                    db,
                    None,
                    "voice.gated",
                    {
                        "event_id": utterance.event_id,
                        "principal": principal,
                        "decision": decision,
                        "provenance": utterance.model_dump(mode="json"),
                    },
                )
            if decision["accepted"] and (previous is None or previous["status"] == "pending"):
                if (
                    previous
                    and db.execute(
                        "SELECT 1 FROM harness_events WHERE id=?", (utterance.event_id,)
                    ).fetchone()
                ):
                    db.execute(
                        "UPDATE voice_ingress SET status='legacy_local' WHERE id=?",
                        (utterance.event_id,),
                    )
                elif now >= utterance.received_at.timestamp() + 60:
                    db.execute(
                        "UPDATE voice_ingress SET status='expired' WHERE id=?",
                        (utterance.event_id,),
                    )
                elif not decision["request_text"].strip():
                    # Ready for local display; the followup window starts at its delivery ACK.
                    decision["reply"] = {
                        "text": "我在，请说。",
                        "event_id": utterance.event_id,
                        "principal": principal,
                        "audio_generated": False,
                        "local_ack": True,
                    }
                    db.execute(
                        "UPDATE voice_ingress SET status='reply_ready',decision=? WHERE id=?",
                        (canonical(decision), utterance.event_id),
                    )
                else:
                    envelope = ForwardedVoice(
                        principal=principal,
                        adapter=adapter,
                        utterance=utterance,
                        request_text=decision["request_text"],
                        gate_reason=decision["reason"],
                        wake_word=decision["wake_word"],
                    )
                    self.forward.enqueue(db, envelope)
                    db.execute(
                        "UPDATE voice_ingress SET status='forwarded' WHERE id=?",
                        (utterance.event_id,),
                    )
            return {**self._status(db, principal, utterance.event_id), "duplicate": bool(previous)}
