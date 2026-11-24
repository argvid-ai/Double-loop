"""Durable result handoff, separate from model wakes and user notifications."""

import json

from .contracts import canonical, digest
from .media_audit import without_image_bytes


class ContextMail:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversation_context_mail (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, source_event TEXT NOT NULL,
                    body TEXT NOT NULL, created REAL NOT NULL, delivered REAL,
                    UNIQUE(conversation_id,source_event)
                );
                CREATE INDEX IF NOT EXISTS context_mail_pending
                    ON conversation_context_mail(principal,conversation_id,delivered);
                CREATE TABLE IF NOT EXISTS conversation_event_archives (
                    event_id TEXT PRIMARY KEY, principal TEXT NOT NULL,
                    conversation_id TEXT NOT NULL, source_event TEXT NOT NULL,
                    reason TEXT NOT NULL, archived_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS conversation_archive_sources
                    ON conversation_event_archives(principal,conversation_id,source_event);
            """)

    def put(self, principal, target, source_event, body, *, db):
        self.store.conversations.get(principal, target, db=db)
        ident = "context-" + digest({"target": target, "source": source_event})[:32]
        db.execute(
            "INSERT OR IGNORE INTO conversation_context_mail "
            "(id,principal,conversation_id,source_event,body,created) VALUES (?,?,?,?,?,?)",
            (
                ident,
                principal,
                target,
                source_event,
                canonical(without_image_bytes(body)),
                self.store.clock(),
            ),
        )
        return ident

    def project(self, event, reply, *, db):
        if (
            reply.get("quiet")
            or reply.get("pending_receipt")
            or event["kind"].endswith("notification")
        ):
            return
        if not reply.get("text") and not reply.get("error") and not reply.get("reviewed_result"):
            return
        principal = event["principal"]
        session = event.get("session_key")
        if not session and event.get("task_id"):
            row = db.execute(
                "SELECT id FROM harness_conversations WHERE task_id=? AND principal=?",
                (event["task_id"], principal),
            ).fetchone()
            session = row["id"] if row else None
        if not session:
            return
        conversation = self.store.conversations.get(principal, session, db=db)
        if conversation["kind"] == "main":
            return
        targets, visited = [], {session}
        target = conversation.get("parent_id") or event["body"].get("origin_conversation_id")
        while target and target not in visited:
            visited.add(target)
            parent = self.store.conversations.get(principal, target, db=db)
            targets.append(target)
            target = parent.get("parent_id")
        task = self.store._task(db, event["task_id"], principal) if event.get("task_id") else None
        body = {
            "origin": "work_result",
            "source_event": event["id"],
            "source_kind": event["kind"],
            "source_conversation": session,
            "source_created": event.get("created"),
            "source_finished": event.get("finished"),
            "task_id": event.get("task_id"),
            "task_version": task["version"] if task else None,
            "task_status_at_delivery": task["status"] if task else None,
            "recorded_at": self.store.clock(),
            "result": {
                k: reply[k]
                for k in (
                    "text",
                    "error",
                    "source",
                    "model_invoked",
                    "simulated",
                    "review_activity",
                    "published_reports",
                )
                if k in reply
            },
            "authority": "result_and_evidence_only_not_new_authorization",
        }
        reviewed = reply.get("reviewed_result")
        if reviewed:
            body["result"]["reviewed_result"] = {
                k: reviewed[k]
                for k in (
                    "asset_id",
                    "asset_side",
                    "sha256",
                    "verified",
                    "review_status",
                    "visual_review",
                    "simulated",
                    "stage",
                )
                if k in reviewed
            }
        for target in targets:
            if self.store.conversations.get(
                principal, target, db=db
            )["kind"] == "main":
                continue  # ResultDelivery admits both the visible result and its model context.
            self.put(principal, target, event["id"], body, db=db)

    def pending(self, principal, conversation):
        with self.store.connect() as db:
            self.store.conversations.get(principal, conversation, db=db)
            rows = db.execute(
                "SELECT id,body FROM conversation_context_mail WHERE principal=? "
                "AND conversation_id=? AND delivered IS NULL AND NOT EXISTS ("
                "SELECT 1 FROM conversation_event_archives a WHERE "
                "a.principal=conversation_context_mail.principal AND "
                "a.conversation_id=conversation_context_mail.conversation_id AND "
                "a.source_event=conversation_context_mail.source_event) ORDER BY created,id",
                (principal, conversation),
            ).fetchall()
            items = []
            for row in rows:
                body = json.loads(row["body"])
                task_id = body.get("task_id")
                current = (
                    db.execute(
                        "SELECT status,version FROM business_tasks WHERE id=? AND principal=?",
                        (task_id, principal),
                    ).fetchone()
                    if task_id
                    else None
                )
                items.append(
                    {
                        "id": row["id"],
                        "body": body,
                        "current_task_state": dict(current) if current else None,
                    }
                )
            return items

    def acknowledge(self, principal, conversation, ids):
        with self.store.connect() as db:
            for ident in ids:
                db.execute(
                    "UPDATE conversation_context_mail SET delivered=COALESCE(delivered,?) "
                    "WHERE id=? AND principal=? AND conversation_id=?",
                    (self.store.clock(), ident, principal, conversation),
                )

    def archived_ids(self, principal, conversation):
        """Owner-selected old projections; raw mail stays available for audit."""
        with self.store.connect() as db:
            self.store.conversations.get(principal, conversation, db=db)
            return [r[0] for r in db.execute(
                """SELECT DISTINCT m.id FROM conversation_context_mail m
                JOIN conversation_event_archives a ON a.principal=m.principal
                AND a.conversation_id=m.conversation_id AND a.source_event=m.source_event
                WHERE m.principal=? AND m.conversation_id=?""", (principal, conversation))]
