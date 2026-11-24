"""Per-film execution rounds inside one task; never another model conversation."""

from .store import Conflict


class MediaOutputs:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS media_outputs (
                    task_id TEXT NOT NULL, output_key TEXT NOT NULL, brief TEXT NOT NULL,
                    start_version INTEGER NOT NULL, version INTEGER NOT NULL,
                    source_event TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(task_id,output_key), UNIQUE(task_id,version))
            """)

    def begin(self, event, args):
        from .harness_authority import require_work_target

        if event["read_only"]:
            raise Conflict("read-only input cannot begin a film")
        require_work_target(self.store, event, args.task_id)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = self.store.event(event["id"], event["principal"], _db=db)
            if saved["status"] != "running":
                raise Conflict("input was superseded")
            task = self.store._task(db, args.task_id, event["principal"])
            if task["kind"] not in {"goal", "edit"}:
                raise Conflict("this task is not a media work")
            if task["status"] in {"paused", "cancelled", "completed", "expired"}:
                raise Conflict("task no longer permits a film")
            old = db.execute(
                "SELECT * FROM media_outputs WHERE task_id=? AND output_key=?",
                (task["id"], args.output_key),
            ).fetchone()
            if old:
                if old["brief"] != args.brief:
                    raise Conflict("output key already has a different brief")
                return self.receipt(old)
            if task["version"] != args.expected_version:
                raise Conflict("stale task version")
            pinned = event.get("_task_versions", {}).get(task["id"])
            if pinned is not None and pinned != task["version"]:
                raise Conflict("task was updated after the model decision")
            job = db.execute(
                "SELECT * FROM harness_jobs WHERE task_id=? AND version=? AND kind='compose'",
                (task["id"], task["version"]),
            ).fetchone()
            current = db.execute(
                "SELECT 1 FROM media_outputs WHERE task_id=? AND version=?",
                (task["id"], task["version"]),
            ).fetchone()
            receipt = task.get("result") or {}
            finished = bool(
                job and job["status"] == "completed" and receipt.get("verified")
                and (not receipt.get("review_required")
                     or receipt.get("review_status") == "approved")
            )
            if (job or current or receipt.get("review_required")) and not finished:
                raise Conflict("current film is unfinished; continue or revise it before starting another")
            if db.execute(
                "SELECT 1 FROM harness_jobs WHERE task_id=? AND status IN ('queued','running','waiting')",
                (task["id"],),
            ).fetchone():
                raise Conflict("current operation is unfinished")
            version = task["version"] + int(finished)
            if finished:
                db.execute(
                    "UPDATE business_tasks SET version=?,status='ready',result=NULL,updated=? WHERE id=?",
                    (version, self.store.clock(), task["id"]),
                )
            db.execute(
                "INSERT INTO media_outputs VALUES (?,?,?,?,?,?,?)",
                (task["id"], args.output_key, args.brief, version, version,
                 event["id"], self.store.clock()),
            )
            db.execute("INSERT OR REPLACE INTO run_task_versions VALUES (?,?,?)",
                       (event["id"], task["id"], version))
            row = db.execute("SELECT * FROM media_outputs WHERE task_id=? AND output_key=?",
                             (task["id"], args.output_key)).fetchone()
            result = self.receipt(row)
            self.store._audit(db, task["id"], "media.output.begun", result)
        event.setdefault("_task_versions", {})[task["id"]] = version
        return result

    @staticmethod
    def receipt(row):
        return {"task_id": row["task_id"], "task_version": row["version"],
                "output_key": row["output_key"], "brief": row["brief"],
                "source_event": row["source_event"]}

    def context(self, task):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM media_outputs WHERE task_id=? ORDER BY start_version DESC LIMIT 20",
                (task["id"],),
            ).fetchall()
        return [self.receipt(row) for row in rows]

    def finish_deadline(self, event, result, *, db):
        """A finite task ends after its last result, not by stopping shared capture."""
        if result.get("error") or result.get("pending_receipt") or not event.get("task_id"):
            return
        task = self.store._task(db, event["task_id"], event["principal"])
        if task["kind"] != "goal" or task["status"] != "ready":
            return
        origin = event
        if event["kind"] != "timer":
            row = db.execute(
                "SELECT source_event FROM media_outputs WHERE task_id=? AND version=?",
                (task["id"], task["version"]),
            ).fetchone()
            origin = self.store.event(row["source_event"], event["principal"], _db=db) if row else event
            if not origin["body"].get("final_occurrence"):
                final = db.execute(
                    "SELECT e.id FROM harness_events e JOIN wake_rules r ON r.task_id=e.task_id "
                    "WHERE e.task_id=? AND e.kind='timer' AND e.status IN ('done','running') "
                    "AND json_extract(e.body,'$.final_occurrence')=1 "
                    "AND json_extract(e.body,'$.rule_revision')=r.revision "
                    "AND json_extract(e.body,'$.task_version')<=? ORDER BY e.created DESC LIMIT 1",
                    (task["id"], task["version"]),
                ).fetchone()
                if final:
                    origin = self.store.event(final["id"], event["principal"], _db=db)
        if origin["kind"] != "timer" or not origin["body"].get("final_occurrence"):
            return
        rule = db.execute("SELECT revision FROM wake_rules WHERE task_id=?", (task["id"],)).fetchone()
        if not rule or rule["revision"] != origin["body"].get("rule_revision"):
            return  # A user's replacement schedule supersedes an old final wake.
        if origin["body"].get("output_kind") == "video":
            if not (task.get("result") or {}).get("review_status") == "approved":
                return
        elif (task.get("result") or {}).get("review_required"):
            if task["result"].get("review_status") != "approved":
                return
        if db.execute(
            "SELECT 1 FROM harness_jobs WHERE task_id=? AND status IN ('queued','running','waiting')",
            (task["id"],),
        ).fetchone():
            return
        db.execute("UPDATE business_tasks SET status='completed',updated=? WHERE id=?",
                   (self.store.clock(), task["id"]))
        self.store._audit(db, task["id"], "task.deadline.completed",
                          {"event_id": event["id"], "recording_controlled": False})
