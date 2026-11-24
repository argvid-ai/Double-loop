"""Durable application tasks and events; NanoBot owns its own conversation history."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Conflict(ValueError):
    pass


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, root: Path, *, filename: str = "cloud.sqlite3"):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / filename
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, prompt TEXT NOT NULL, context TEXT NOT NULL,
                    status TEXT NOT NULL, answer TEXT, outcome TEXT, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    kind TEXT NOT NULL, payload TEXT NOT NULL, due REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued', error TEXT,
                    created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS edge_tasks (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
                    request_key TEXT NOT NULL, request TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT,
                    UNIQUE(task_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL REFERENCES events(id),
                    task_id TEXT NOT NULL, started REAL NOT NULL,
                    finished REAL, status TEXT NOT NULL, result TEXT
                );
            """)

    @contextmanager
    def connect(self):
        from .sqlite_writes import WriteConnection

        db = sqlite3.connect(self.path, timeout=10, factory=WriteConnection).bind(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_task(self, prompt: str, context: dict, due: float | None = None):
        task_id = uuid.uuid4().hex
        now = time.time()
        with self.connect() as db:
            db.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                (task_id, prompt, encode(context), "queued", None, None, now),
            )
            db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,'queued',NULL,?)",
                (
                    uuid.uuid4().hex,
                    task_id,
                    "timer" if due is not None else "user",
                    "{}",
                    due if due is not None else now,
                    now,
                ),
            )
        return self.task(task_id)

    def task(self, task_id: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise KeyError(task_id)
            task = dict(row)
            task["context"] = json.loads(task["context"])
            task["outcome"] = json.loads(task["outcome"]) if task["outcome"] else None
            task["edge_tasks"] = self._edges(db, task_id)
            task["events"] = [
                dict(r)
                for r in db.execute(
                    "SELECT id,kind,due,status,error FROM events WHERE task_id=? ORDER BY created",
                    (task_id,),
                )
            ]
            task["runs"] = [
                dict(r)
                for r in db.execute("SELECT * FROM runs WHERE task_id=? ORDER BY id", (task_id,))
            ]
            for run in task["runs"]:
                if run["result"]:
                    run["result"] = json.loads(run["result"])
            return task

    def tasks(self):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,prompt,status,created FROM tasks ORDER BY created DESC"
                )
            ]

    @staticmethod
    def _edges(db, task_id):
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM edge_tasks WHERE task_id=? ORDER BY rowid", (task_id,)
            )
        ]
        for row in rows:
            row["request"] = json.loads(row["request"])
            row["result"] = json.loads(row["result"]) if row["result"] else None
        return rows

    def context(self, task_id):
        task = self.task(task_id)
        return {"goal": task["prompt"], "facts": task["context"], "edge_tasks": task["edge_tasks"]}

    def submit_edge(self, task_id, request_key, request):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            old = db.execute(
                "SELECT * FROM edge_tasks WHERE task_id=? AND request_key=?", (task_id, request_key)
            ).fetchone()
            if old:
                if old["request"] != encode(request):
                    raise Conflict("request_key already names a different request")
                return {
                    "task_id": old["id"],
                    "status": old["status"],
                    "simulated": True,
                    "result": json.loads(old["result"]) if old["result"] else None,
                }
            if task["status"] != "running":
                raise Conflict("edge tasks may only be submitted by an active cloud run")
            edge_id = uuid.uuid4().hex
            db.execute(
                "INSERT INTO edge_tasks VALUES (?,?,?,?,?,NULL)",
                (edge_id, task_id, request_key, encode(request), "accepted"),
            )
        return {"task_id": edge_id, "status": "accepted", "simulated": True, "result": None}

    def complete_edge(self, edge_id: str, event_id: str, result: dict):
        """The mock result and its wake event commit in one transaction."""
        if result.get("simulated") is not True:
            raise ValueError("Mock completion requires simulated=true")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            edge = db.execute("SELECT * FROM edge_tasks WHERE id=?", (edge_id,)).fetchone()
            if edge is None:
                raise KeyError(edge_id)
            payload = encode({"edge_task_id": edge_id, "result": result})
            previous = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if previous:
                if previous["kind"] != "edge.completed" or previous["payload"] != payload:
                    raise Conflict("event_id already names a different event")
                return {"event_id": event_id, "task_id": edge["task_id"], "duplicate": True}
            if edge["status"] == "completed":
                if edge["result"] != encode(result):
                    raise Conflict("edge task already completed with a different result")
                return {"event_id": None, "task_id": edge["task_id"], "duplicate": True}
            db.execute(
                "UPDATE edge_tasks SET status='completed',result=? WHERE id=?",
                (encode(result), edge_id),
            )
            now = time.time()
            db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,'queued',NULL,?)",
                (event_id, edge["task_id"], "edge.completed", payload, now, now),
            )
        return {"event_id": event_id, "task_id": edge["task_id"], "duplicate": False}

    def recover(self):
        """Called only by the exclusive worker at startup; interrupted runs are visible."""
        with self.connect() as db:
            db.execute(
                "UPDATE runs SET status='interrupted',finished=? WHERE status='running'",
                (time.time(),),
            )
            db.execute("UPDATE events SET status='queued' WHERE status='running'")
            db.execute("UPDATE tasks SET status='queued',outcome=NULL WHERE status='running'")

    def publish_result(self, task_id, status, summary):
        if status not in {"completed", "unsupported"}:
            raise ValueError("result status must be completed or unsupported")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            if task["status"] != "running":
                raise Conflict("only an active cloud run may publish a result")
            pending = db.execute(
                "SELECT COUNT(*) FROM edge_tasks WHERE task_id=? AND status='accepted'", (task_id,)
            ).fetchone()[0]
            if pending:
                raise Conflict("remote work is still pending; wait for completion")
            result = {"status": status, "summary": summary, "edge_mode": "mock"}
            db.execute("UPDATE tasks SET outcome=? WHERE id=?", (encode(result), task_id))
        return result

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE events SET status='superseded' WHERE status='queued' "
                "AND task_id IN (SELECT id FROM tasks WHERE status IN ('completed','unsupported'))"
            )
            row = db.execute(
                "SELECT * FROM events WHERE status='queued' AND due<=? "
                "ORDER BY due,created LIMIT 1",
                (time.time(),),
            ).fetchone()
            if row is None:
                return None
            event = dict(row)
            db.execute("UPDATE events SET status='running' WHERE id=?", (event["id"],))
            db.execute(
                "UPDATE tasks SET status='running',outcome=NULL WHERE id=?", (event["task_id"],)
            )
            cur = db.execute(
                "INSERT INTO runs (event_id,task_id,started,status) VALUES (?,?,?,'running')",
                (event["id"], event["task_id"], time.time()),
            )
            event["run_id"] = cur.lastrowid
            event["payload"] = json.loads(event["payload"])
            return event

    def finish(self, event, *, answer: str, trace: dict, error: str | None = None):
        with self.connect() as db:
            pending = db.execute(
                "SELECT COUNT(*) FROM edge_tasks WHERE task_id=? AND status='accepted'",
                (event["task_id"],),
            ).fetchone()[0]
            outcome = db.execute(
                "SELECT outcome FROM tasks WHERE id=?", (event["task_id"],)
            ).fetchone()[0]
            final_status = json.loads(outcome)["status"] if outcome else "needs_input"
            status = "failed" if error else ("waiting" if pending else final_status)
            db.execute(
                "UPDATE tasks SET status=?,answer=? WHERE id=?", (status, answer, event["task_id"])
            )
            if error:
                db.execute("UPDATE tasks SET outcome=NULL WHERE id=?", (event["task_id"],))
            elif status in {"completed", "unsupported"}:
                db.execute(
                    "UPDATE events SET status='superseded' WHERE task_id=? "
                    "AND status='queued' AND kind='edge.completed'",
                    (event["task_id"],),
                )
            db.execute(
                "UPDATE events SET status=?,error=? WHERE id=?",
                ("failed" if error else "done", error, event["id"]),
            )
            db.execute(
                "UPDATE runs SET status=?,finished=?,result=? WHERE id=?",
                ("failed" if error else "completed", time.time(), encode(trace), event["run_id"]),
            )

    def retry_event(self, event_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT e.status,t.status AS task_status FROM events e "
                "JOIN tasks t ON t.id=e.task_id WHERE e.id=?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            if row["status"] != "failed":
                raise Conflict("only a failed event can be retried explicitly")
            if row["task_status"] in {"completed", "unsupported"}:
                raise Conflict("a finished task cannot be reopened by retrying an old event")
            db.execute("UPDATE events SET status='queued',error=NULL WHERE id=?", (event_id,))
