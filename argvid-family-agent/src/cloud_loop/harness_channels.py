"""Durable channel routing into the existing main conversation and notifications."""

from __future__ import annotations
import asyncio
import json
import re
from pathlib import Path
from pydantic import Field, SecretStr, field_validator
from .contracts import Contract, canonical, digest
from .harness_contracts import UserInput
from .store import Conflict


class FeishuSettings(Contract):
    enabled: bool = False
    app_id: str = ""
    app_secret: SecretStr = SecretStr("")
    allowed_user_ids: list[str] = Field(default_factory=list, max_length=20)
    test_chat_id: str = ""
    notify_main: bool = False

    @field_validator("allowed_user_ids")
    @classmethod
    def users(cls, values):
        if any(not re.fullmatch(r"ou_[A-Za-z0-9_-]+", v) for v in values):
            raise ValueError("only explicit Feishu user open IDs are supported")
        return values


class FeishuBridge:
    def __init__(self, host, path, transport_factory=None):
        self.host, self.store, self.path = host, host.store, Path(path).resolve()
        self.config = FeishuSettings.model_validate_json(self.path.read_text())
        self.transport_factory = transport_factory
        self.transport = None
        self.wake = asyncio.Event()
        self.tasks = []
        self.error = None
        previous_app = self.store.meta("feishu_app_id")
        self.identity_changed = bool(previous_app and previous_app != self.config.app_id)
        if not previous_app and self.config.app_id:
            self.store.set_meta("feishu_app_id", self.config.app_id)
        self.notice_since = self.store.meta("feishu_notice_since", self.store.clock())
        self.store.set_meta("feishu_notice_since", self.notice_since)
        with self.store.connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS feishu_routes(event_id TEXT PRIMARY KEY,principal TEXT NOT NULL,sender TEXT NOT NULL,chat TEXT NOT NULL,message_id TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS feishu_deliveries(event_id TEXT PRIMARY KEY,chat TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_try REAL NOT NULL DEFAULT 0,receipt TEXT,error TEXT,updated REAL NOT NULL);
              CREATE TABLE IF NOT EXISTS feishu_pending(sender TEXT PRIMARY KEY,seen REAL NOT NULL);
            """)

    def pending(self, sender):
        with self.store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO feishu_pending VALUES (?,?)", (sender, self.store.clock())
            )

    def status(self):
        with self.store.connect() as db:
            pending = [
                dict(r)
                for r in db.execute("SELECT * FROM feishu_pending ORDER BY seen DESC LIMIT 10")
                if r["sender"] not in self.config.allowed_user_ids
            ]
            deliveries = [
                {**dict(r), "receipt": json.loads(r["receipt"]) if r["receipt"] else None}
                for r in db.execute(
                    "SELECT event_id,status,attempts,receipt,error,updated FROM feishu_deliveries ORDER BY updated DESC LIMIT 10"
                )
            ]
        return {
            "configured": bool(self.config.app_id and self.config.app_secret.get_secret_value()),
            "enabled": self.config.enabled,
            "connected": bool(self.transport and self.transport.connected),
            "app_id": self.config.app_id,
            "allowed_user_ids": self.config.allowed_user_ids,
            "pending_users": pending,
            "deliveries": deliveries,
            "notify_main": self.config.notify_main,
            "error": self.error,
            "scope": "authorized_private_messages",
            "media_upload": True,
        }

    def authorize(self, sender):
        if self.identity_changed:
            raise Conflict("app identity changed; explicit rebinding is required")
        with self.store.connect() as db:
            if not db.execute("SELECT 1 FROM feishu_pending WHERE sender=?", (sender,)).fetchone():
                raise Conflict("user must first contact this bot")
        allowed = list(dict.fromkeys([*self.config.allowed_user_ids, sender]))
        data = json.loads(self.path.read_text())
        data["allowed_user_ids"] = allowed
        updated = FeishuSettings.model_validate(data)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        temporary.chmod(0o600)
        temporary.replace(self.path)
        self.config = updated
        if self.transport:
            self.transport.channel.config.allow_from = allowed
        return self.status()

    def ingest(self, message):
        if self.identity_changed:
            raise Conflict("app identity changed; explicit rebinding is required")
        if message.sender_id not in self.config.allowed_user_ids:
            self.pending(message.sender_id)
            return {"accepted": False, "reason": "sender_not_authorized"}
        if message.metadata.get("chat_type") not in {None, "p2p"}:
            raise Conflict("this installation accepts private messages only")
        if not message.content.strip():
            raise Conflict("empty channel message")
        message_id = message.metadata.get("message_id")
        if not message_id:
            raise Conflict("platform message identity is required")
        ident = "feishu-" + digest([self.config.app_id, message_id])[:40]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM feishu_routes WHERE event_id=?", (ident,)).fetchone()
            if prior:
                if (prior["sender"], prior["chat"]) != (message.sender_id, message.chat_id):
                    raise Conflict("message identity differs")
                original = db.execute(
                    "SELECT body FROM harness_events WHERE id=?", (ident,)
                ).fetchone()
                original_text = json.loads(original["body"])["text"] if original else None
                legacy_prefix = (
                    original_text is not None
                    and len(original_text) == 8000
                    and message.content.startswith(original_text)
                )
                if original_text != message.content and not legacy_prefix:
                    raise Conflict("platform message content changed")
                return {"accepted": True, "duplicate": True, "event_id": ident}
            result = self.host.submit_user(
                UserInput(event_id=ident, text=message.content),
                "local-user",
                channel="feishu",
                _db=db,
            )
            db.execute(
                "INSERT INTO feishu_routes VALUES (?,?,?,?,?)",
                (ident, "local-user", message.sender_id, message.chat_id, message_id),
            )
        self.store.audit(
            None,
            "channel.input",
            {
                "event_id": ident,
                "channel": "feishu",
                "message_id": message_id,
                "sender": message.sender_id,
                "simulated": bool(message.metadata.get("simulated", False)),
            },
        )
        self.wake.set()
        return result

    def _destination(self, db, event):
        direct = db.execute(
            "SELECT chat FROM feishu_routes WHERE event_id=?", (event["id"],)
        ).fetchone()
        if direct:
            return direct["chat"]
        body = json.loads(event["body"])
        origin = body.get("origin_event_id")
        if origin:
            route = db.execute(
                "SELECT chat FROM feishu_routes WHERE event_id=?", (origin,)
            ).fetchone()
            if route:
                return route["chat"]
            forwarded = db.execute(
                "SELECT body FROM harness_events WHERE id=? AND principal=?",
                (origin, event["principal"]),
            ).fetchone()
            if forwarded:
                source_id = json.loads(forwarded["body"]).get("source_event_id")
                route = db.execute(
                    "SELECT chat FROM feishu_routes WHERE event_id=?", (source_id,)
                ).fetchone()
                if route:
                    return route["chat"]
        if event["task_id"]:
            route = db.execute(
                "SELECT r.chat FROM feishu_routes r JOIN task_routes t ON t.source_event=r.event_id WHERE t.task_id=?",
                (event["task_id"],),
            ).fetchone()
            if route:
                return route["chat"]
        if self.config.notify_main and self.config.test_chat_id in self.config.allowed_user_ids:
            return self.config.test_chat_id
        return None

    def enqueue_results(self):
        with self.store.connect() as db:
            # Archiving is reversible, but already queued internal messages must
            # not leak through the external retry path after owner cleanup.
            db.execute(
                """UPDATE feishu_deliveries
                   SET status='suppressed',error='owner_archived_internal_message',updated=?
                   WHERE status IN ('pending','retry') AND event_id IN
                     (SELECT a.event_id FROM conversation_event_archives a
                      JOIN harness_events e ON e.id=a.event_id
                      AND e.principal=a.principal AND e.session_key=a.conversation_id)""",
                (self.store.clock(),),
            )
            events = db.execute(
                """SELECT e.* FROM harness_events e
                   WHERE principal='local-user' AND status IN ('done','failed','cancelled')
                   AND ((channel='feishu' AND kind='user') OR kind='task.notification'
                        OR (kind='report.notification'
                            AND json_extract(reply,'$.delivery_managed')=1))
                   AND id NOT IN (SELECT event_id FROM feishu_deliveries)
                   AND NOT EXISTS (SELECT 1 FROM conversation_event_archives a
                       WHERE a.event_id=e.id AND a.principal=e.principal
                       AND a.conversation_id=e.session_key)
                   ORDER BY created"""
            ).fetchall()
            for event in events:
                destination = self._destination(db, event)
                if not destination:
                    continue
                reply = json.loads(event["reply"] or "{}")
                if reply.get("quiet"):
                    continue
                text = reply.get("text") or (
                    "这次处理未完成，请在 Muse 动态查看错误记录。"
                    if event["status"] == "failed"
                    else ""
                )
                reviewed = reply.get("reviewed_result") or {}
                media = []
                if reviewed.get("verified") and reviewed.get("review_status") == "approved":
                    asset = db.execute(
                        "SELECT a.* FROM assets a JOIN harness_jobs j ON j.id=a.task_id JOIN business_tasks b ON b.id=j.task_id WHERE a.id=? AND b.principal=? AND b.id=? AND j.version=b.version AND a.kind='deliverable'",
                        (reviewed["asset_id"], event["principal"], event["task_id"]),
                    ).fetchone()
                    if not asset:
                        raise Conflict("approved channel media missing")
                    path = Path(asset["path"]).resolve()
                    if not path.is_relative_to(self.store.root.resolve()) or not path.is_file():
                        raise Conflict("media path unavailable")
                    media = [str(path)]
                text = re.sub(r"\[([^\]]+)\]\(cloud-asset://[^)]+\)", r"\1", text)
                if not text and not media:
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO feishu_deliveries(event_id,chat,body,status,updated) VALUES (?,?,?,?,?)",
                    (
                        event["id"],
                        destination,
                        canonical({"text": text, "media": media}),
                        "pending",
                        self.store.clock(),
                    ),
                )

    async def deliver_once(self):
        self.enqueue_results()
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM feishu_deliveries WHERE status IN ('pending','retry') AND next_try<=? ORDER BY updated LIMIT 1",
                (self.store.clock(),),
            ).fetchone()
        if row is None:
            return False
        try:
            payload = json.loads(row["body"])
            receipt = await self.transport.send(
                row["event_id"], row["chat"], payload["text"], payload["media"]
            )
            if not receipt.get("message_ids"):
                raise RuntimeError("delivery has no platform receipt")
            status, error = "delivered", None
        except Exception as exc:
            receipt = None
            status = "failed" if row["attempts"] >= 2 else "retry"
            error = (
                str(exc).replace(self.config.app_secret.get_secret_value(), "[redacted]")
                if self.config.app_secret.get_secret_value()
                else type(exc).__name__
            )
        with self.store.connect() as db:
            db.execute(
                "UPDATE feishu_deliveries SET status=?,attempts=attempts+1,next_try=?,receipt=?,error=?,updated=? WHERE event_id=?",
                (
                    status,
                    self.store.clock() + 30,
                    canonical(receipt) if receipt else None,
                    error,
                    self.store.clock(),
                    row["event_id"],
                ),
            )
        return True

    async def _incoming(self):
        while not self.host.stop.is_set():
            message = await self.transport.receive()
            try:
                self.ingest(message)
            except Exception as exc:
                self.error = type(exc).__name__

    async def _outgoing(self):
        self.wake.set()
        while not self.host.stop.is_set():
            with self.store.connect() as db:
                retry = db.execute(
                    "SELECT MIN(next_try) FROM feishu_deliveries WHERE status='retry'"
                ).fetchone()[0]
            timeout = max(1, retry - self.store.clock()) if retry else None
            try:
                await asyncio.wait_for(self.wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            try:
                while await self.deliver_once():
                    pass
            except Exception as exc:
                self.error = type(exc).__name__

    async def start(self):
        if self.identity_changed:
            self.error = "app_identity_changed_requires_rebinding"
            return
        if not self.config.enabled:
            return
        if not self.config.app_id or not self.config.app_secret.get_secret_value():
            self.error = "missing_credentials"
            return
        if self.transport_factory is None:
            from .feishu_transport import FeishuTransport

            self.transport_factory = FeishuTransport
        self.transport = self.transport_factory(
            self.config, self.store.root / "channels/feishu", self.pending
        )
        await self.transport.start()
        self.tasks = [asyncio.create_task(self._incoming()), asyncio.create_task(self._outgoing())]

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.transport:
            await self.transport.close()
