"""Durable secondary voice results, separate from replies and wake authority.

Cloud receipt means stored on edge, not played aloud. Local display requires its
own acknowledgement and never opens a new voice follow-up window.
"""

import asyncio
import json

import httpx
from pydantic import Field

from .contracts import ID, Contract, canonical, digest
from .store import Conflict


class VoiceNotice(Contract):
    event_id: str = Field(pattern=ID)
    origin_event_id: str = Field(pattern=ID)
    principal: str = Field(pattern=ID)
    conversation_id: str = Field(pattern=ID)
    reply: dict
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class VoiceNoticeReceipt(Contract):
    event_id: str = Field(pattern=ID)
    notice_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class VoiceNotifications:
    def __init__(self, host):
        self.host, self.store = host, host.store
        self.lock = asyncio.Lock()
        self.next_poll = 0
        self.after_sequence = 0
        self.transport_error = None
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS voice_notifications (
                id TEXT PRIMARY KEY,principal TEXT NOT NULL,origin_event_id TEXT NOT NULL,
                body TEXT NOT NULL,created REAL NOT NULL,received_at REAL,displayed_at REAL)""")

    def enqueue(self, db, event, reply):
        if self.host.side != "cloud" or event["channel"] != "voice":
            return False
        origin = db.execute(
            "SELECT * FROM voice_forward_inbox WHERE id=? AND principal=? AND admission='accepted'",
            (event["id"], event["principal"]),
        ).fetchone()
        if origin is None:
            return False  # Only an actual forwarded voice input has an edge return route.
        envelope = json.loads(origin["body"])
        if envelope["adapter"] == "edge-text":
            return False
        notice = VoiceNotice(
            event_id=reply["event_id"],
            origin_event_id=event["id"],
            principal=event["principal"],
            conversation_id=reply["session_key"],
            reply=reply,
            request_digest=digest(envelope),
        )
        wire = canonical(notice)
        old = db.execute(
            "SELECT body FROM voice_notifications WHERE id=?", (notice.event_id,)
        ).fetchone()
        if old and old["body"] != wire:
            raise Conflict("voice notification identity collision")
        db.execute(
            "INSERT OR IGNORE INTO voice_notifications(id,principal,origin_event_id,body,created) VALUES (?,?,?,?,?)",
            (notice.event_id, notice.principal, notice.origin_event_id, wire, self.store.clock()),
        )
        return True

    def pending_cloud(self, principal, limit=8, *, after_sequence=0):
        if self.host.side != "cloud":
            raise Conflict("notification outbox belongs to cloud")
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("invalid notification page size")
        if type(after_sequence) is not int or after_sequence < 0:
            raise ValueError("invalid notification cursor")
        with self.store.connect() as db:
            return [
                {"sequence": r["rowid"], "notice": json.loads(r["body"])}
                for r in db.execute(
                    """SELECT rowid,body FROM voice_notifications
                WHERE principal=? AND received_at IS NULL AND rowid>? ORDER BY rowid LIMIT ?""",
                    (principal, after_sequence, limit),
                )
            ]

    def received_cloud(self, principal, receipt):
        if self.host.side != "cloud":
            raise Conflict("notification transport receipt belongs to cloud")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM voice_notifications WHERE id=? AND principal=?",
                (receipt.event_id, principal),
            ).fetchone()
            if row is None or digest(json.loads(row["body"])) != receipt.notice_digest:
                raise Conflict("notification transport receipt mismatch")
            db.execute(
                "UPDATE voice_notifications SET received_at=COALESCE(received_at,?) WHERE id=?",
                (self.store.clock(), receipt.event_id),
            )
            return {
                **receipt.model_dump(),
                "stored_on_edge": True,
                "display_confirmed": False,
                "audio_generated": False,
            }

    def accept_edge(self, notice):
        if self.host.side != "edge":
            raise Conflict("notification inbox belongs to edge")
        notice = VoiceNotice.model_validate(notice)
        reply = notice.reply
        if (
            reply.get("event_id") != notice.event_id
            or reply.get("principal") != notice.principal
            or reply.get("session_key") != notice.conversation_id
            or reply.get("source") != "observation_lifecycle"
            or not isinstance(reply.get("text"), str)
            or not reply["text"]
            or reply.get("model_invoked") is not False
            or reply.get("audio_generated") is not False
        ):
            raise Conflict("invalid observation voice notification")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            origin = db.execute(
                "SELECT * FROM voice_forward_outbox WHERE id=? AND principal=?",
                (notice.origin_event_id, notice.principal),
            ).fetchone()
            if (
                origin is None
                or digest(json.loads(origin["body"])) != notice.request_digest
                or json.loads(origin["body"])["adapter"] == "edge-text"
            ):
                raise Conflict("voice notification has no matching original input")
            if not notice.conversation_id.startswith("cloud:main:"):
                raise Conflict("voice notification does not belong to cloud main")
            if origin["conversation_id"] not in (None, notice.conversation_id):
                raise Conflict("voice notification conversation mismatch")
            if db.execute(
                "SELECT 1 FROM voice_forward_outbox WHERE id=?", (notice.event_id,)
            ).fetchone():
                raise Conflict("notification identity belongs to an input")
            if (
                db.execute("SELECT 1 FROM voice_ingress WHERE id=?", (notice.event_id,)).fetchone()
                or db.execute(
                    "SELECT 1 FROM harness_events WHERE id=?", (notice.event_id,)
                ).fetchone()
            ):
                raise Conflict("notification identity belongs to local input history")
            old = db.execute(
                "SELECT body FROM voice_notifications WHERE id=?", (notice.event_id,)
            ).fetchone()
            wire = canonical(notice)
            if old and old["body"] != wire:
                raise Conflict("voice notification identity collision")
            db.execute(
                "INSERT OR IGNORE INTO voice_notifications VALUES (?,?,?,?,?,?,NULL)",
                (
                    notice.event_id,
                    notice.principal,
                    notice.origin_event_id,
                    wire,
                    self.store.clock(),
                    self.store.clock(),
                ),
            )
        return VoiceNoticeReceipt(event_id=notice.event_id, notice_digest=digest(notice))

    def local(self, principal, *, origin_event_id=None, limit=16):
        if self.host.side != "edge":
            raise Conflict("notification display belongs to edge")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid notification display limit")
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT * FROM voice_notifications WHERE principal=? AND displayed_at IS NULL
                AND (? IS NULL OR origin_event_id=?) ORDER BY created,rowid LIMIT ?""",
                (principal, origin_event_id, origin_event_id, limit),
            ).fetchall()
            return [self._status(row) for row in rows]

    @staticmethod
    def _status(row):
        body = json.loads(row["body"])
        reply = body["reply"]
        return {
            "event_id": row["id"],
            "origin_event_id": row["origin_event_id"],
            "channel": "voice",
            "status": "delivered" if row["displayed_at"] is not None else "reply_ready",
            "reply": reply,
            "reply_digest": digest(reply),
            "delivered_at": row["displayed_at"],
            "audio_generated": False,
            "opens_followup_window": False,
            "kind": "observation_notification",
        }

    def acknowledge(self, db, principal, delivery):
        row = db.execute(
            "SELECT * FROM voice_notifications WHERE id=? AND principal=?",
            (delivery.event_id, principal),
        ).fetchone()
        if row is None:
            return None
        status = self._status(row)
        if delivery.reply_digest != status["reply_digest"]:
            raise Conflict("notification display digest mismatch")
        db.execute(
            "UPDATE voice_notifications SET displayed_at=COALESCE(displayed_at,?) WHERE id=?",
            (self.store.clock(), delivery.event_id),
        )
        return {
            **status,
            "status": "delivered",
            "delivered_at": row["displayed_at"] or self.store.clock(),
            "duplicate": row["displayed_at"] is not None,
        }

    async def poll_once(self):
        if self.host.side != "edge" or self.store.clock() < self.next_poll:
            return 0
        async with self.lock:
            if self.store.clock() < self.next_poll:
                return 0
            self.next_poll = self.store.clock() + 2
            count = 0
            error = None
            try:
                response = await self.host.client.get(
                    "/harness/voice/notifications/pending",
                    params={"after_sequence": self.after_sequence},
                    timeout=2,
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, list) or len(payload) > 16:
                    raise Conflict("invalid notification page")
                if not payload:
                    self.after_sequence = 0
                for item in payload:
                    if (
                        not isinstance(item, dict)
                        or type(item.get("sequence")) is not int
                        or item["sequence"] <= self.after_sequence
                    ):
                        raise Conflict("invalid notification page order")
                    self.after_sequence = item["sequence"]
                    try:
                        receipt = self.accept_edge(
                            item.get("notice")
                        )  # Durable commit precedes remote receipt.
                        answer = await self.host.client.post(
                            "/harness/voice/notifications/received",
                            json=receipt.model_dump(),
                            timeout=2,
                        )
                        answer.raise_for_status()
                        ack = answer.json()
                        if (
                            not isinstance(ack, dict)
                            or ack.get("event_id") != receipt.event_id
                            or ack.get("notice_digest") != receipt.notice_digest
                            or ack.get("stored_on_edge") is not True
                        ):
                            raise Conflict("notification receipt mismatch")
                        count += 1
                    except (httpx.HTTPError, ValueError, Conflict) as exc:
                        error = type(exc).__name__
            except (httpx.HTTPError, ValueError, Conflict) as exc:
                error = type(exc).__name__
                self.next_poll = self.store.clock() + 5
            if error != self.transport_error:
                self.store.audit(
                    None,
                    "voice.notifications.transport",
                    {"error_type": error, "audio_generated": False},
                )
                self.transport_error = error
            return count
