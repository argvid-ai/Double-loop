"""One bounded background writer for already classified observation events.

No inference or cloud delivery happens here. Event selection remains owned by
ObservationDecisions; file proof and quotas remain owned by ObservationArchive.
"""

from __future__ import annotations

import asyncio
import fcntl
import math
import time
from concurrent.futures import ThreadPoolExecutor

from .observation_export import MoreCapturedMediaRequired
from .store import Conflict

_owners = {}


class ObservationRetentionWorker:
    def __init__(
        self,
        decisions,
        archive,
        *,
        max_output_bytes,
        on_failure=None,
        retry_seconds=2,
        close_timeout=3,
        cleanup=None,
    ):
        if decisions.store is not archive.store:
            raise ValueError("retention components must share the node store")
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise ValueError("invalid per-file storage reservation")
        for value in (retry_seconds, close_timeout):
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError("invalid retention time bound")
        self.decisions, self.archive, self.store = decisions, archive, archive.store
        self.max_output_bytes, self.on_failure = max_output_bytes, on_failure
        self.cleanup = cleanup
        self.retry_seconds, self.close_timeout = retry_seconds, close_timeout
        self.key = str(self.store.path.resolve())
        if self.key in _owners:
            raise Conflict("another retention worker owns this node")
        self.handle = (self.store.root / "observation-retention.lock").open("a")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.handle.close()
            raise Conflict("another retention worker owns this node") from None
        self.executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="observation-retention"
        )
        self.future = None
        self.active_task_id = None
        self.lock = asyncio.Lock()
        self.tick_active = False
        self.closing = False
        self.released = False
        try:
            with self.store.connect() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS observation_retention_work (
                    event_id TEXT PRIMARY KEY,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
                    next_try REAL NOT NULL DEFAULT 0,error TEXT,updated REAL NOT NULL)""")
        except BaseException:
            self.executor.shutdown(wait=False)
            self.handle.close()
            raise
        _owners[self.key] = self

    def _select(self):
        with self.store.connect() as db:
            row = db.execute(
                """SELECT e.*,t.principal FROM observation_retention_events e
                JOIN observation_tasks t ON t.task_id=e.task_id
                LEFT JOIN observation_retention_work w ON w.event_id=e.id
                WHERE (e.status IN ('open','exporting') OR
                       (e.status='saved' AND COALESCE(w.status,'pending')!='saved'))
                AND e.merged_into IS NULL AND COALESCE(w.next_try,0)<=?
                ORDER BY COALESCE(w.next_try,0),e.created,e.id LIMIT 1""",
                (self.store.clock(),),
            ).fetchone()
            return dict(row) if row else None

    def _record(self, event, status, error=None):
        now = self.store.clock()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT status,error,attempts FROM observation_retention_work WHERE event_id=?",
                (event["id"],),
            ).fetchone()
            attempts = (old["attempts"] if old else 0) + 1
            delay = (
                min(60, self.retry_seconds * 2 ** min(attempts - 1, 5))
                if status == "blocked"
                else self.retry_seconds
            )
            db.execute(
                """INSERT INTO observation_retention_work VALUES (?,?,?,?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET status=excluded.status,attempts=excluded.attempts,
                next_try=excluded.next_try,error=excluded.error,updated=excluded.updated""",
                (event["id"], status, attempts, now + delay, error, now),
            )
            if not old or (old["status"], old["error"]) != (status, error):
                self.store._audit(
                    db,
                    event["task_id"],
                    "observation.retention.progress",
                    {
                        "event_id": event["id"],
                        "status": status,
                        "error_type": error,
                        "file_saved": status == "saved",
                        "cloud_model_invoked": False,
                        "cloud_upload": False,
                    },
                )
        return {
            "event_id": event["id"],
            "task_id": event["task_id"],
            "version": event["version"],
            "principal": event["principal"],
            "status": status,
            "error_type": error,
            "file_saved": status == "saved",
        }

    def _process(self, event):
        try:
            plan = self.decisions.event_plan(event["principal"], event["id"])
            event = {**plan["event"], "principal": event["principal"]}
            if plan["event"]["status"] == "open" and not plan["ready"]:
                return self._record(event, "waiting_for_context")
            job = self.decisions.reserve_event(
                event["principal"],
                event["id"],
                self.archive,
                max_output_bytes=self.max_output_bytes,
            )
            self.archive.materialize(event["principal"], job["id"])
            receipt = self.decisions.export_result(event["principal"], event["id"], self.archive)
            if not receipt["file_saved"]:
                raise Conflict("export did not produce a verified file")
            event = {**receipt["event"], "principal": event["principal"]}
            return self._record(event, "saved")
        except MoreCapturedMediaRequired:
            return self._record(event, "waiting_for_codec_boundary", "MoreCapturedMediaRequired")
        except Conflict as exc:
            # A model decision can extend an open event between this worker's
            # initial check and the transactional reservation. That is a wait,
            # not a storage failure which should pause observation.
            latest = self.decisions.event_plan(event["principal"], event["id"])
            if latest["event"]["status"] == "open" and not latest["ready"]:
                return self._record(event, "waiting_for_context")
            return self._record(event, "blocked", type(exc).__name__)
        except Exception as exc:
            return self._record(event, "blocked", type(exc).__name__)

    async def tick(self):
        async with self.lock:
            if self.closing:
                return False
            # Cancellation of an async caller must not create a second writer
            # while its prior executor thread is still materializing a file.
            if self.future is not None and not self.future.done():
                return False
            event = self._select()
            if event is None and self.cleanup is None:
                return False
            self.tick_active = True
            self.active_task_id = event["task_id"] if event else None
            self.future = (
                self.executor.submit(self._process, event)
                if event
                else self.executor.submit(self.cleanup.run_once)
            )
            try:
                result = await asyncio.shield(asyncio.wrap_future(self.future))
                if (
                    isinstance(result, dict)
                    and result["status"] == "blocked"
                    and self.on_failure is not None
                    and not self.closing
                ):
                    await self.on_failure(result)
                return result
            finally:
                self.tick_active = False

    def status(self, task_id):
        with self.store.connect() as db:
            counts = {
                row[0]: row[1]
                for row in db.execute(
                    """SELECT COALESCE(w.status,'pending'),COUNT(*)
                FROM observation_retention_events e LEFT JOIN observation_retention_work w ON w.event_id=e.id
                WHERE e.task_id=? AND e.merged_into IS NULL GROUP BY COALESCE(w.status,'pending')""",
                    (task_id,),
                )
            }
            errors = [
                dict(row)
                for row in db.execute(
                    """SELECT w.event_id,w.status,w.error FROM observation_retention_work w
                JOIN observation_retention_events e ON e.id=w.event_id WHERE e.task_id=? AND w.error IS NOT NULL
                ORDER BY w.updated DESC LIMIT 5""",
                    (task_id,),
                )
            ]
        return {
            "counts": counts,
            "errors": errors,
            "writer_active": self.active_task_id == task_id
            and self.future is not None
            and not self.future.done(),
        }

    async def close(self):
        self.closing = True
        deadline = time.monotonic() + self.close_timeout
        while (
            self.tick_active or (self.future is not None and not self.future.done())
        ) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        pending = self.tick_active or (self.future is not None and not self.future.done())
        if not pending and not self.released:
            self.executor.shutdown(wait=False, cancel_futures=False)
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
            self.released = True
            if _owners.get(self.key) is self:
                del _owners[self.key]
        return {
            "confirmed": not pending,
            "lock_released": self.released,
            "pending_callbacks": int(pending),
        }
