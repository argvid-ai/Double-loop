"""Same-ID edge voice/text delivery and cloud acceptance in the existing node DB.

Only voice has a first-admission TTL; ordinary text has none. Lookup precedes
every retry, including after the voice deadline. A lost
acceptance response is uncertain, not expired. Receipt polling never resubmits
an accepted event or starts a model. Local rendering is acknowledged separately.
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import datetime, timezone

import httpx
from pydantic import TypeAdapter

from .contracts import canonical, digest
from .harness_contracts import (
    ForwardedInput,
    ForwardedText,
    ForwardedVoice,
    UserInput,
    VoiceInput,
    VoiceReceiptQuery,
)
from .store import Conflict


def _admission_expired(envelope: ForwardedInput, now: float) -> bool:
    # Reception provenance is not a device-freshness or task-version guard.
    return isinstance(envelope, ForwardedVoice) and now >= envelope.received_at.timestamp() + 60


class VoiceForwarder:
    """Shared ingress transport; retain legacy table/class names for voice data."""

    def __init__(self, host):
        self.host, self.store = host, host.store
        self._delivery_lock = asyncio.Lock()
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS voice_forward_outbox (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, body TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', accepted_at REAL,
                    cloud_status TEXT, conversation_id TEXT, reply TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                    send_attempted INTEGER NOT NULL DEFAULT 0, error TEXT
                );
                CREATE TABLE IF NOT EXISTS voice_forward_inbox (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, body TEXT NOT NULL,
                    admission TEXT NOT NULL, accepted_at REAL
                );
                CREATE TABLE IF NOT EXISTS voice_delivery_receipts (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL,
                    reply_digest TEXT NOT NULL, delivered_at REAL NOT NULL
                );
            """)

    @staticmethod
    def enqueue(db, envelope):
        wire = canonical(envelope)
        ident = envelope.event_id
        if db.execute("SELECT 1 FROM harness_events WHERE id=?", (ident,)).fetchone():
            raise Conflict("forwarded input identity belongs to local history")
        old = db.execute("SELECT body FROM voice_forward_outbox WHERE id=?", (ident,)).fetchone()
        if old and old["body"] != wire:
            raise Conflict("forwarded voice identity collision")
        db.execute(
            "INSERT OR IGNORE INTO voice_forward_outbox(id,principal,body) VALUES (?,?,?)",
            (ident, envelope.principal, wire),
        )

    def enqueue_text(self, message: UserInput, principal):
        """Persist once, preserving original text reception provenance on retries."""
        message = UserInput.model_validate(message.model_dump())
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM voice_forward_outbox WHERE id=?", (message.event_id,)
            ).fetchone()
            if old:
                envelope = TypeAdapter(ForwardedInput).validate_json(old["body"])
                if (
                    not isinstance(envelope, ForwardedText)
                    or old["principal"] != principal
                    or canonical(envelope.message) != canonical(message)
                ):
                    raise Conflict("forwarded text identity collision")
                return {**self.status(principal, message.event_id, db=db), "duplicate": True}
            legacy = db.execute(
                "SELECT * FROM harness_events WHERE id=?", (message.event_id,)
            ).fetchone()
            if legacy:
                if (legacy["principal"], legacy["channel"], legacy["kind"], legacy["body"]) != (
                    principal,
                    "web",
                    "user",
                    canonical(message),
                ):
                    raise Conflict("text identity belongs to another local input")
                return {
                    "event_id": message.event_id,
                    "duplicate": True,
                    "status": "legacy_local",
                    "cloud_accepted": False,
                    "read_only_history": True,
                    "reply": json.loads(legacy["reply"]) if legacy["reply"] else None,
                    "text": "此输入属于旧版端侧历史，不重复提交云端。",
                }
            if db.execute("SELECT 1 FROM voice_ingress WHERE id=?", (message.event_id,)).fetchone():
                raise Conflict("text identity belongs to voice ingress")
            envelope = ForwardedText(
                principal=principal,
                message=message,
                received_at=datetime.fromtimestamp(self.store.clock(), timezone.utc),
            )
            self.enqueue(db, envelope)
            return {**self.status(principal, message.event_id, db=db), "duplicate": False}

    def _receipt(self, db, row):
        envelope = TypeAdapter(ForwardedInput).validate_json(row["body"])
        result = {
            "event_id": row["id"],
            "principal": row["principal"],
            "request_digest": digest(envelope),
            "accepted": row["admission"] == "accepted",
            "accepted_at": row["accepted_at"],
            "status": row["admission"],
            "conversation_id": None,
            "reply": None,
        }
        if result["accepted"]:
            event = self.store.event(row["id"], row["principal"], _db=db)
            result.update(
                status=event["status"],
                conversation_id=event["body"]["conversation_id"],
                reply=event["reply"],
            )
        return result

    def lookup(self, query: VoiceReceiptQuery, principal):
        if self.host.side != "cloud" or query.principal != principal:
            raise Conflict("voice receipt principal or destination mismatch")
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM voice_forward_inbox WHERE id=?", (query.event_id,)
            ).fetchone()
            if row is None:
                return {
                    **query.model_dump(),
                    "accepted": False,
                    "status": "not_found",
                    "accepted_at": None,
                    "conversation_id": None,
                    "reply": None,
                }
            if (
                row["principal"] != principal
                or digest(json.loads(row["body"])) != query.request_digest
            ):
                raise Conflict("voice receipt identity collision")
            return self._receipt(db, row)

    def accept(self, envelope: ForwardedInput, principal):
        """Called exclusively by the dedicated node-authenticated cloud route."""
        envelope = TypeAdapter(ForwardedInput).validate_python(envelope.model_dump())
        if self.host.side != "cloud" or envelope.principal != principal:
            raise Conflict("voice ingress principal or destination mismatch")
        ident, wire = envelope.event_id, canonical(envelope)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM voice_forward_inbox WHERE id=?", (ident,)).fetchone()
            if old:
                if old["principal"] != principal or old["body"] != wire:
                    raise Conflict("forwarded voice identity collision")
                return self._receipt(db, old)  # accepted receipts outlive delivery TTL
            if db.execute("SELECT 1 FROM harness_events WHERE id=?", (ident,)).fetchone():
                raise Conflict("voice identity belongs to another input channel")
            now = self.store.clock()
            received = envelope.received_at.timestamp()
            if received > now + 30:
                raise Conflict("voice receipt timestamp is in the future")
            expired = _admission_expired(envelope, now)
            db.execute(
                "INSERT INTO voice_forward_inbox VALUES (?,?,?,?,?)",
                (
                    ident,
                    principal,
                    wire,
                    "expired" if expired else "accepted",
                    None if expired else now,
                ),
            )
            if not expired:
                main = self.store.conversations.main(principal, db=db)
                if isinstance(envelope, ForwardedVoice):
                    message = VoiceInput(
                        event_id=ident,
                        text=envelope.request_text,
                        mode="query" if envelope.adapter == "mock-transcript"
                        and envelope.utterance.asr.get("scenario_read_only") is True else "interact",
                        observed_at=envelope.received_at,
                        source_ref=envelope.utterance.source_ref,
                        simulated=envelope.utterance.asr.get("source_simulated", True)
                        or envelope.adapter == "mock-transcript",
                        conversation_id=main["id"],
                    )
                    self.host.submit_user(
                        message,
                        principal,
                        channel="voice",
                        _db=db,
                        _voice_forward=envelope.model_dump(mode="json"),
                    )
                else:
                    message = UserInput(
                        event_id=ident,
                        text=envelope.message.text,
                        mode=envelope.message.mode,
                        interaction_id=envelope.message.interaction_id,
                        conversation_id=main["id"],
                    )
                    self.host.submit_user(
                        message, principal, _db=db, _text_forward=envelope.model_dump(mode="json")
                    )
            row = db.execute("SELECT * FROM voice_forward_inbox WHERE id=?", (ident,)).fetchone()
            return self._receipt(db, row)

    def status(self, principal, ident, *, db=None):
        if db is None:
            with self.store.connect() as conn:
                return self.status(principal, ident, db=conn)
        row = db.execute(
            "SELECT * FROM voice_forward_outbox WHERE id=? AND principal=?", (ident, principal)
        ).fetchone()
        if row is None:
            raise KeyError(ident)
        reply = json.loads(row["reply"]) if row["reply"] else None
        delivered = db.execute(
            "SELECT delivered_at FROM voice_delivery_receipts WHERE id=? AND principal=?",
            (ident, principal),
        ).fetchone()
        descriptions = {
            "pending": "等待云端确认受理，尚不能确认已执行。",
            "accepted": "云端已受理，正在等待回复。",
            "reply_ready": "云端回复已到达，等待本地展示确认。",
            "delivered": "回复已在本地展示。",
            "expired": "这条输入在首次受理前已过期，未执行；请重新发出。",
            "failed": "本次输入投递或处理失败，请查看状态后重新发出。",
        }
        channel = "web" if json.loads(row["body"])["adapter"] == "edge-text" else "voice"
        return {
            "event_id": ident,
            "channel": channel,
            "destination": "cloud/main",
            "status": row["status"],
            "cloud_accepted": row["accepted_at"] is not None,
            "accepted_at": row["accepted_at"],
            "cloud_status": row["cloud_status"],
            "conversation_id": row["conversation_id"],
            "reply": reply,
            "reply_digest": digest(reply) if reply is not None else None,
            "audio": self.host.voice_audio.status(principal, ident)
            if hasattr(self.host, "voice_audio")
            else None,
            "delivered_at": delivered["delivered_at"] if delivered else None,
            "acceptance_uncertain": bool(
                row["send_attempted"]
                and row["accepted_at"] is None
                and row["status"] in {"pending", "failed"}
            ),
            "error": row["error"],
            "text": descriptions[row["status"]],
            "audio_generated": False,
        }

    def _retry(self, ident, error):
        with self.store.connect() as db:
            db.execute(
                """UPDATE voice_forward_outbox SET attempts=attempts+1,
                next_try=?,error=? WHERE id=? AND status IN ('pending','accepted')""",
                (self.store.clock() + 2, error, ident),
            )

    def _terminal(self, ident, status, error=None):
        with self.store.connect() as db:
            db.execute(
                "UPDATE voice_forward_outbox SET status=?,error=? WHERE id=?",
                (status, error, ident),
            )

    def _save_receipt(self, envelope, receipt):
        ident = envelope.event_id
        expected = (ident, envelope.principal, digest(envelope))
        if (
            not isinstance(receipt, dict)
            or tuple(receipt.get(k) for k in ("event_id", "principal", "request_digest"))
            != expected
        ):
            raise Conflict("cloud voice receipt identity mismatch")
        if receipt.get("accepted") is not True:
            if receipt.get("accepted") is not False or receipt.get("status") not in {
                "not_found",
                "expired",
            }:
                raise Conflict("invalid voice admission receipt")
            if isinstance(envelope, ForwardedText) and receipt["status"] == "expired":
                raise Conflict("ordinary text has no implicit admission expiry")
            return False
        if receipt.get("status") not in {"queued", "running", "done", "failed", "superseded"}:
            raise Conflict("invalid cloud voice processing state")
        if (
            type(receipt.get("accepted_at")) not in {int, float}
            or not math.isfinite(receipt["accepted_at"])
            or not isinstance(receipt.get("conversation_id"), str)
            or not receipt["conversation_id"].startswith("cloud:main:")
        ):
            raise Conflict("missing cloud acceptance identity")
        reply = receipt.get("reply")
        if reply is not None:
            if (
                not isinstance(reply, dict)
                or reply.get("event_id") != ident
                or reply.get("principal") != envelope.principal
            ):
                raise Conflict("cloud voice reply identity mismatch")
            if reply.get("session_key") not in {None, receipt["conversation_id"]}:
                raise Conflict("cloud voice reply conversation mismatch")
        status = (
            "failed"
            if receipt["status"] == "failed"
            else "reply_ready"
            if reply is not None
            else "accepted"
        )
        with self.store.connect() as db:
            db.execute(
                """UPDATE voice_forward_outbox SET status=?,accepted_at=?,cloud_status=?,
                conversation_id=?,reply=?,error=NULL,next_try=? WHERE id=?""",
                (
                    status,
                    receipt["accepted_at"],
                    receipt["status"],
                    receipt["conversation_id"],
                    canonical(reply) if reply is not None else None,
                    self.store.clock() + 1,
                    ident,
                ),
            )
        return True

    async def deliver_once(self, *, limit=4):
        """One bounded pass using the existing authenticated node client."""
        if self.host.side != "edge":
            return 0
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("voice delivery limit must be 1..16")
        async with self._delivery_lock:
            with self.store.connect() as db:
                rows = db.execute(
                    """SELECT * FROM voice_forward_outbox
                    WHERE status IN ('pending','accepted') AND next_try<=?
                    ORDER BY next_try,rowid LIMIT ?""",
                    (self.store.clock(), limit),
                ).fetchall()
            for row in rows:
                envelope = TypeAdapter(ForwardedInput).validate_json(row["body"])
                ident = row["id"]
                expired = _admission_expired(envelope, self.store.clock())
                if expired and not row["send_attempted"] and row["accepted_at"] is None:
                    self._terminal(ident, "expired")
                    continue
                query = VoiceReceiptQuery(
                    event_id=ident, principal=envelope.principal, request_digest=digest(envelope)
                )
                route = (
                    "/harness/ingress" if isinstance(envelope, ForwardedText) else "/harness/voice"
                )
                try:
                    response = await self.host.client.post(
                        route + "/receipt", json=query.model_dump(), timeout=2
                    )
                    response.raise_for_status()
                    receipt = response.json()
                    if self._save_receipt(envelope, receipt):
                        continue
                    if row["accepted_at"] is not None:
                        # A cloud database loss must not replay an already accepted command.
                        self._terminal(ident, "failed", "cloud_acceptance_record_missing")
                        continue
                    if receipt["status"] == "expired" or _admission_expired(
                        envelope, self.store.clock()
                    ):
                        self._terminal(ident, "expired")
                        continue
                    with self.store.connect() as db:
                        db.execute(
                            "UPDATE voice_forward_outbox SET send_attempted=1 WHERE id=?", (ident,)
                        )
                    response = await self.host.client.post(
                        route + "/forward", json=envelope.model_dump(mode="json"), timeout=2
                    )
                    response.raise_for_status()
                    receipt = response.json()
                    if not self._save_receipt(envelope, receipt):
                        if receipt["status"] != "expired":
                            raise Conflict("cloud did not return a voice admission")
                        self._terminal(ident, "expired")
                except httpx.HTTPStatusError as exc:
                    if exc.response.status_code in {400, 401, 403, 404, 409, 422}:
                        self._terminal(ident, "failed", f"cloud_http_{exc.response.status_code}")
                    else:
                        self._retry(ident, "cloud_unavailable")
                except Conflict:
                    self._terminal(ident, "failed", "cloud_receipt_mismatch")
                except (httpx.RequestError, ValueError):
                    self._retry(ident, "delivery_unconfirmed")
            return len(rows)
