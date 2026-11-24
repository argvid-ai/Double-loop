"""Dual-mode state extends the existing durable task/event store, one DB per side."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .contracts import Scene, TaskInput, canonical
from .store import Conflict, Store

TERMINAL = {"completed", "unsupported", "cancelled", "expired"}


class DualStore(Store):
    def __init__(self, root: Path, side: str):
        self.side = side
        if side not in {"cloud", "edge"}:
            raise ValueError("invalid side")
        super().__init__(root, filename=f"{side}.sqlite3")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS remote (
                    id TEXT PRIMARY KEY, cloud_task TEXT NOT NULL, request TEXT NOT NULL,
                    binding TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
                    last_sent REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,
                    body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, task_id TEXT, path TEXT NOT NULL,
                    info TEXT NOT NULL, kind TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, kind TEXT NOT NULL,
                    result TEXT NOT NULL, created REAL NOT NULL
                );
            """)
            if side == "edge":
                db.execute(
                    "INSERT OR IGNORE INTO metadata VALUES ('scene',?)", (canonical(Scene()),)
                )

    def meta(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, canonical(value)))

    @staticmethod
    def _audit(db, task_id, kind, payload):
        db.execute(
            "INSERT INTO audit(task_id,kind,payload,created) VALUES (?,?,?,?)",
            (task_id, kind, canonical(payload), time.time()),
        )

    def audit(self, task_id, kind, payload):
        with self.connect() as db:
            self._audit(db, task_id, kind, payload)

    def timeline(self, task_id=None, after=0):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM audit WHERE seq>? AND (? IS NULL OR task_id=?) ORDER BY seq LIMIT 500",
                (after, task_id, task_id),
            ).fetchall()
        return [
            {**dict(r), "payload": self.audit_payload(r["payload"]), "side": self.side}
            for r in rows
        ]

    def audit_payload(self, payload):
        from .audit_archive import restore_payload

        return restore_payload(self.root, payload)

    def new_task(self, data: TaskInput, profile_digest: str, max_seconds: int):
        now = time.time()
        due = data.run_at.timestamp() if data.run_at else now
        if due < now - 1:
            raise ValueError("run_at is in the past")
        deadline = min(
            due + max_seconds, data.deadline.timestamp() if data.deadline else float("inf")
        )
        if deadline <= due:
            raise ValueError("deadline must follow the scheduled start")
        context = {
            "user_context": data.context,
            "mode": data.completion_mode,
            "profile_digest": profile_digest,
            "expires": deadline,
            "simulated": True,
        }
        task = super().create_task(data.prompt, context, due if data.run_at else None)
        self.audit(
            task["id"], "task.created", {"prompt": data.prompt, "mode": data.completion_mode}
        )
        return task

    def task(self, task_id):
        task = super().task(task_id)
        with self.connect() as db:
            task["assets"] = [
                {"id": r["id"], "kind": r["kind"], "info": json.loads(r["info"])}
                for r in db.execute("SELECT * FROM assets WHERE task_id=?", (task_id,))
            ]
        task["side"] = self.side
        return task

    def assert_active(self, task_id):
        task = self.task(task_id)
        if (
            task["status"] in TERMINAL
            or task["context"].get("paused")
            or task["context"].get("expires", float("inf")) <= time.time()
        ):
            raise Conflict("task cancelled, finished or expired")
        return task

    def wake(self, task_id, kind, payload, event_id=None):
        event_id = event_id or uuid.uuid4().hex
        with self.connect() as db:
            row = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not row or row[0] in TERMINAL:
                return False
            db.execute(
                "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,'queued',NULL,?)",
                (event_id, task_id, kind, canonical(payload), time.time(), time.time()),
            )
        return True

    def expire(self):
        for row in self.tasks():
            if row["status"] not in TERMINAL:
                task = self.task(row["id"])
                if task["context"].get("expires", float("inf")) <= time.time():
                    self.cancel(task["id"], "expired")

    def claim(self):
        self.expire()
        with self.connect() as db:
            db.execute(
                "UPDATE events SET status='superseded' WHERE status='queued' AND task_id IN "
                "(SELECT id FROM tasks WHERE status IN ('cancelled','expired'))"
            )
            db.execute(
                "UPDATE events SET status='superseded' WHERE status='queued' AND task_id IN "
                "(SELECT id FROM tasks WHERE json_extract(context,'$.paused')=1)"
            )
        return super().claim()

    def cancel(self, task_id, status="cancelled"):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if old is None:
                raise KeyError(task_id)
            if old[0] in TERMINAL:
                return
            db.execute("UPDATE tasks SET status=?,outcome=NULL WHERE id=?", (status, task_id))
            db.execute(
                "UPDATE events SET status='superseded' WHERE task_id=? AND status='queued'",
                (task_id,),
            )
            db.execute(
                "UPDATE outbox SET status='superseded' WHERE task_id=? AND status='pending'",
                (task_id,),
            )
            db.execute(
                "UPDATE remote SET status=?,last_sent=0 WHERE cloud_task=? AND status NOT IN ('completed','failed')",
                (status, task_id),
            )
            db.execute(
                "UPDATE edge_tasks SET status=? WHERE task_id=? AND status IN ('sent','accepted')",
                (status, task_id),
            )
            self._audit(db, task_id, f"task.{status}", {})

    def set_outcome(self, task_id, status, summary):
        self.assert_active(task_id)
        if status not in {"completed", "unsupported", "waiting"}:
            raise ValueError("invalid outcome")
        with self.connect() as db:
            db.execute(
                "UPDATE tasks SET outcome=? WHERE id=?",
                (
                    canonical(
                        {
                            "status": status,
                            "summary": summary,
                            "simulated": True,
                            "capture_mode": "replay",
                        }
                    ),
                    task_id,
                ),
            )

    def finish(self, event, *, answer, trace, error=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT status,outcome,context FROM tasks WHERE id=?", (event["task_id"],)
            ).fetchone()
            if row["status"] in TERMINAL:
                status = row["status"]
            elif json.loads(row["context"]).get("paused"):
                status = "waiting"
                db.execute(
                    "UPDATE tasks SET status='waiting',outcome=NULL WHERE id=?", (event["task_id"],)
                )
            else:
                pending = db.execute(
                    "SELECT COUNT(*) FROM edge_tasks WHERE task_id=? AND status IN ('sent','accepted')",
                    (event["task_id"],),
                ).fetchone()[0]
                outcome = json.loads(row["outcome"]) if row["outcome"] else {}
                status = (
                    "failed"
                    if error
                    else ("waiting" if pending else outcome.get("status", "needs_input"))
                )
                db.execute(
                    "UPDATE tasks SET status=?,answer=?,outcome=? WHERE id=?",
                    (status, answer, None if error else row["outcome"], event["task_id"]),
                )
            db.execute(
                "UPDATE events SET status=?,error=? WHERE id=?",
                ("failed" if error else "done", error, event["id"]),
            )
            db.execute(
                "UPDATE runs SET status=?,finished=?,result=? WHERE id=?",
                (
                    "failed" if error else "completed",
                    time.time(),
                    canonical(trace),
                    event["run_id"],
                ),
            )
            self._audit(
                db,
                event["task_id"],
                "agent.finished",
                {
                    "status": status,
                    "error": error,
                    "model": trace.get("model"),
                    "usage": trace.get("usage"),
                    "answer": answer,
                },
            )

    def scene(self):
        return Scene.model_validate(self.meta("scene", Scene().model_dump(mode="json")))

    def change_scene(self, stage):
        # Compatibility entry for the original demo. It is now just a Mock
        # producer feeding the same ingress used by future sensor adapters.
        from .inputs import InputIngress, mock_stage

        ingress = InputIngress(self)
        with self.connect() as db:
            sequence = db.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM input_heads").fetchone()[
                0
            ]
        ingress.ingest_many(
            mock_stage(
                stage, sequence, viewpoint=self.scene().viewpoint, provider_prefix="mock-local"
            )
        )
        return self.scene()

    def put_asset(self, asset_id, task_id, path, info, kind):
        resolved = Path(path).resolve()
        if not resolved.is_relative_to(self.root) or not resolved.is_file():
            raise ValueError("asset must be an existing local file")
        with self.connect() as db:
            old = db.execute("SELECT info FROM assets WHERE id=?", (asset_id,)).fetchone()
            if old and old[0] != canonical(info):
                raise Conflict("asset id already names different content")
            db.execute(
                "INSERT OR IGNORE INTO assets VALUES (?,?,?,?,?)",
                (asset_id, task_id, str(resolved), canonical(info), kind),
            )

    def asset(self, asset_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if row is None:
            raise KeyError(asset_id)
        return {**dict(row), "info": json.loads(row["info"])}

    def assets(self, task_id):
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,kind,info FROM assets WHERE task_id=? OR task_id IS NULL", (task_id,)
            ).fetchall()
        return [{**dict(r), "info": json.loads(r["info"])} for r in rows]

    def action(self, action_id):
        with self.connect() as db:
            row = db.execute("SELECT result FROM actions WHERE id=?", (action_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def record_action(self, action_id, task_id, kind, result):
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO actions VALUES (?,?,?,?,?)",
                (action_id, task_id, kind, canonical(result), time.time()),
            )
            self._audit(db, task_id, f"tool.{kind}", result)

    def queue_output(self, task_id, kind, body):
        ident = f"{kind}:{task_id}"
        with self.connect() as db:
            old = db.execute("SELECT body FROM outbox WHERE id=?", (ident,)).fetchone()
            if old and old[0] != canonical(body):
                raise Conflict("outbox identity conflict")
            db.execute(
                "INSERT OR IGNORE INTO outbox(id,task_id,kind,body) VALUES (?,?,?,?)",
                (ident, task_id, kind, canonical(body)),
            )
        return ident

    def pending_outputs(self):
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM outbox WHERE status='pending' AND next_try<=? ORDER BY rowid",
                (time.time(),),
            ).fetchall()
        return [{**dict(r), "body": json.loads(r["body"])} for r in rows]

    def output_sent(self, ident):
        with self.connect() as db:
            db.execute("UPDATE outbox SET status='sent' WHERE id=? AND status='pending'", (ident,))

    def output_failed(self, ident, max_attempts=5):
        with self.connect() as db:
            row = db.execute("SELECT attempts,status FROM outbox WHERE id=?", (ident,)).fetchone()
            if not row or row["status"] != "pending":
                return
            attempts = row["attempts"] + 1
            status = "failed" if attempts >= max_attempts else "pending"
            db.execute(
                "UPDATE outbox SET attempts=?,status=?,next_try=? WHERE id=?",
                (attempts, status, time.time() + min(60, 2**attempts), ident),
            )

    def retry_output(self, ident):
        with self.connect() as db:
            row = db.execute(
                "SELECT o.status,t.status AS task_status FROM outbox o JOIN tasks t ON t.id=o.task_id WHERE o.id=?",
                (ident,),
            ).fetchone()
            if (
                not row
                or row["status"] != "failed"
                or row["task_status"] in {"cancelled", "expired"}
            ):
                raise Conflict("only a failed, non-cancelled delivery may be retried")
            db.execute(
                "UPDATE outbox SET status='pending',attempts=0,next_try=0 WHERE id=?", (ident,)
            )

    def active_tasks(self):
        return [t for t in self.tasks() if t["status"] not in TERMINAL | {"failed", "needs_input"}]

    def retry_event(self, event_id, profile_digest=None):
        with self.connect() as db:
            row = db.execute(
                "SELECT t.status,t.context FROM events e JOIN tasks t ON t.id=e.task_id WHERE e.id=?",
                (event_id,),
            ).fetchone()
        if row is None:
            raise KeyError(event_id)
        if row["status"] in TERMINAL:
            raise Conflict("a terminal task cannot be retried")
        if profile_digest and json.loads(row["context"])["profile_digest"] != profile_digest:
            raise Conflict("restore the task's pinned profile before retrying")
        return super().retry_event(event_id)
