"""Stable conversation identities; NanoBot still owns their message history."""

from __future__ import annotations

import json
from contextlib import nullcontext

from .contracts import canonical, digest
from .store import Conflict


class ConversationDirectory:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS harness_conversations (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, alias TEXT NOT NULL,
                    kind TEXT NOT NULL, task_id TEXT, parent_id TEXT,
                    status TEXT NOT NULL DEFAULT 'idle', turns INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL,
                    UNIQUE(principal,alias)
                );
                CREATE TABLE IF NOT EXISTS harness_conversation_bindings (
                    conversation_id TEXT PRIMARY KEY, binding TEXT NOT NULL, updated REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS conversation_task
                    ON harness_conversations(principal,task_id) WHERE task_id IS NOT NULL;
            """)

    def ensure(self, principal, alias, kind, *, task_id=None, parent_id=None, db=None):
        with nullcontext(db) if db is not None else self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM harness_conversations WHERE principal=? AND alias=?",
                (principal, alias),
            ).fetchone()
            if row:
                if row["kind"] != kind or (task_id and row["task_id"] not in {None, task_id}):
                    raise Conflict("conversation alias belongs to different work")
                return dict(row)
            ident = (
                f"{self.store.side}:{kind}:" + digest({"principal": principal, "alias": alias})[:24]
            )
            now = self.store.clock()
            conn.execute(
                "INSERT INTO harness_conversations(id,principal,alias,kind,task_id,parent_id,created,updated) VALUES (?,?,?,?,?,?,?,?)",
                (ident, principal, alias, kind, task_id, parent_id, now, now),
            )
            self.store._audit(
                conn,
                task_id,
                "conversation.created",
                {"conversation_id": ident, "alias": alias, "kind": kind, "principal": principal},
            )
            return dict(
                conn.execute("SELECT * FROM harness_conversations WHERE id=?", (ident,)).fetchone()
            )

    def main(self, principal, *, db=None):
        return self.ensure(principal, self.store.side + "/main", "main", db=db)

    def get(self, principal, ident, *, db=None):
        with nullcontext(db) if db is not None else self.store.connect() as conn:
            row = conn.execute(
                "SELECT * FROM harness_conversations WHERE id=? AND principal=?", (ident, principal)
            ).fetchone()
            if row is None:
                raise KeyError(ident)
            binding = conn.execute(
                "SELECT binding FROM harness_conversation_bindings WHERE conversation_id=?",
                (ident,),
            ).fetchone()
            return {**dict(row), "binding": json.loads(binding["binding"]) if binding else None}

    def for_task(self, principal, task_id, *, parent_id=None, db=None):
        with nullcontext(db) if db is not None else self.store.connect() as conn:
            self.store._task(conn, task_id, principal)
            row = conn.execute(
                "SELECT * FROM harness_conversations WHERE principal=? AND task_id=?",
                (principal, task_id),
            ).fetchone()
            if row:
                return dict(row)
            return self.ensure(
                principal, "task:" + task_id, "work", task_id=task_id, parent_id=parent_id, db=conn
            )

    def bind_task(self, principal, task_id, source_event, *, db):
        """A received/direct work request becomes the task's existing conversation."""
        event = db.execute(
            "SELECT session_key,kind FROM harness_events WHERE id=? AND principal=?",
            (source_event, principal),
        ).fetchone()
        parent = event["session_key"] if event else None
        session = db.execute(
            "SELECT * FROM harness_conversations WHERE id=? AND principal=?", (parent, principal)
        ).fetchone()
        if session and session["kind"] == "work" and session["task_id"] in {None, task_id}:
            db.execute(
                "UPDATE harness_conversations SET task_id=?,updated=? WHERE id=?",
                (task_id, self.store.clock(), session["id"]),
            )
            return self.get(principal, session["id"], db=db)
        return self.for_task(principal, task_id, parent_id=parent, db=db)

    def route(self, event, *, db):
        kind = event["kind"]
        body = json.loads(event["body"])
        principal = event["principal"]
        if kind in {"job.result", "media.transcript"}:
            return None
        selected = body.get("conversation_id")
        if selected:
            session = self.get(principal, selected, db=db)
            if (
                event["task_id"]
                and session["kind"] == "work"
                and session["task_id"] not in {None, event["task_id"]}
            ):
                raise Conflict("input task does not belong to selected conversation")
            return session
        if kind in {"user", "voice"}:
            return self.main(principal, db=db)
        if event["task_id"]:
            return self.for_task(principal, event["task_id"], db=db)
        origin_id = body.get("origin_event_id")
        if origin_id:
            origin = db.execute(
                "SELECT session_key FROM harness_events WHERE id=? AND principal=?",
                (origin_id, principal),
            ).fetchone()
            if origin and origin["session_key"]:
                return self.get(principal, origin["session_key"], db=db)
        return self.ensure(principal, "request:" + event["id"], "work", db=db)

    def bind_runtime(self, principal, ident, binding):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.get(principal, ident, db=db)
            previous = db.execute(
                "SELECT binding FROM harness_conversation_bindings WHERE conversation_id=?",
                (ident,),
            ).fetchone()
            wire = canonical(binding)
            if previous and previous["binding"] == wire:
                return
            db.execute(
                "INSERT OR REPLACE INTO harness_conversation_bindings VALUES (?,?,?)",
                (ident, wire, self.store.clock()),
            )
            self.store._audit(
                db,
                None,
                "conversation.binding.changed",
                {
                    "conversation_id": ident,
                    "principal": principal,
                    "previous": json.loads(previous["binding"]) if previous else None,
                    "current": binding,
                    "source": "loaded_profile",
                },
            )

    def list(self, principal, limit=100):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT id FROM harness_conversations WHERE principal=? ORDER BY kind,updated DESC LIMIT ?",
                (principal, min(max(limit, 1), 100)),
            ).fetchall()
            return [self.get(principal, r["id"], db=db) for r in rows]
