"""Durable observation lifecycle and media coverage in HarnessStore's SQLite DB.

The host resolves configuration references and checks capacity before apply,
establishes resources after start/resume, and then calls confirm_started. A
duplicate mutation returns CURRENT state with changed=False; it never instructs
the host to repeat a subscription or side effect. This module does no I/O beyond
the existing database and does not own NanoBot history, media files, or tools.
"""

from __future__ import annotations

import json
from contextlib import contextmanager

from pydantic import TypeAdapter

from .contracts import canonical, digest
from .harness_state import HarnessStore
from .observation_contracts import (
    Counter,
    DeferredBatch,
    ObservationBatch,
    ObservationCommand,
    ObservationConfig,
    ObservationGap,
    ObservationRun,
    Reference,
    SeenBatch,
)
from .store import Conflict


def _validated(model, value):
    # Revalidate model instances too (model_copy/update can bypass validation).
    return model.model_validate(value.model_dump() if isinstance(value, model) else value)


class ObservationStore:
    def __init__(self, store: HarnessStore):
        self.store = store
        with store.connect() as db:
            has_refs = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='observation_media_refs'"
            ).fetchone()
            db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS observation_tasks (
                    task_id TEXT PRIMARY KEY, principal TEXT NOT NULL,
                    version INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL, config TEXT NOT NULL, reason TEXT,
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observation_commands (
                    principal TEXT NOT NULL, request_id TEXT NOT NULL,
                    body TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(principal,request_id)
                );
                CREATE INDEX IF NOT EXISTS observation_instruction_lookup
                    ON observation_commands(
                        principal,json_extract(body,'$.config.task_id'),
                        json_extract(body,'$.config.version'),created)
                    WHERE json_extract(body,'$.kind')='apply';
                CREATE INDEX IF NOT EXISTS observation_control_lookup
                    ON observation_commands(principal,json_extract(body,'$.task_id'))
                    WHERE json_extract(body,'$.kind')='control';
                CREATE INDEX IF NOT EXISTS observation_closed_lookup
                    ON audit(task_id,json_extract(payload,'$.generation'),seq)
                    WHERE kind='observation.resources.closed';
                CREATE TABLE IF NOT EXISTS observation_command_effects (
                    principal TEXT NOT NULL,request_id TEXT NOT NULL,task_id TEXT NOT NULL,
                    version INTEGER NOT NULL,generation INTEGER NOT NULL,
                    created_run INTEGER NOT NULL,created_task INTEGER NOT NULL,changed INTEGER NOT NULL,
                    PRIMARY KEY(principal,request_id)
                );
                CREATE TABLE IF NOT EXISTS observation_media (
                    task_id TEXT NOT NULL REFERENCES observation_tasks(task_id),
                    entry_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    source_ref TEXT NOT NULL, segment_id TEXT NOT NULL,
                    start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL,
                    kind TEXT NOT NULL, body TEXT NOT NULL, received_at REAL NOT NULL,
                    gap_reason TEXT, seen TEXT, seen_at REAL,
                    PRIMARY KEY(task_id,entry_id), UNIQUE(task_id,sequence)
                );
                CREATE INDEX IF NOT EXISTS observation_segments
                    ON observation_media(task_id,segment_id,sequence);
                CREATE TABLE IF NOT EXISTS observation_media_refs(
                    media_ref TEXT NOT NULL, task_id TEXT NOT NULL, entry_id TEXT NOT NULL,
                    PRIMARY KEY(media_ref,task_id,entry_id));
                CREATE INDEX IF NOT EXISTS observation_refs_entry
                    ON observation_media_refs(task_id,entry_id);
                CREATE TRIGGER IF NOT EXISTS observation_media_refs_insert
                AFTER INSERT ON observation_media WHEN new.kind='batch' BEGIN
                    INSERT OR IGNORE INTO observation_media_refs
                    SELECT json_extract(new.body,'$.media_ref'),new.task_id,new.entry_id
                    WHERE json_extract(new.body,'$.media_ref') IS NOT NULL;
                    INSERT OR IGNORE INTO observation_media_refs
                    SELECT value,new.task_id,new.entry_id
                    FROM json_each(new.body,'$.media_parts');
                END;
                CREATE TRIGGER IF NOT EXISTS observation_media_refs_delete
                AFTER DELETE ON observation_media BEGIN
                    DELETE FROM observation_media_refs
                    WHERE task_id=old.task_id AND entry_id=old.entry_id;
                END;
                CREATE TABLE IF NOT EXISTS observation_backfill_retries (
                    task_id TEXT NOT NULL,batch_id TEXT NOT NULL,next_try REAL NOT NULL,error TEXT,
                    PRIMARY KEY(task_id,batch_id));
                CREATE TABLE IF NOT EXISTS observation_backfill_results (
                    id TEXT PRIMARY KEY,task_id TEXT NOT NULL,batch_id TEXT NOT NULL,body TEXT NOT NULL,
                    delivered_by TEXT,UNIQUE(task_id,batch_id));
            """)
            # Project the latest accepted control once for older databases.
            # Keepalive reads current state, never replays command history.
            columns = {row["name"] for row in db.execute("PRAGMA table_info(observation_tasks)")}
            if "last_control" not in columns:
                db.execute("ALTER TABLE observation_tasks ADD COLUMN last_control TEXT")
                db.execute("""UPDATE observation_tasks SET last_control=(
                    SELECT json_object('action',json_extract(c.body,'$.action'),
                                       'reason',json_extract(c.body,'$.reason'))
                    FROM observation_commands c
                    WHERE c.principal=observation_tasks.principal
                    AND json_extract(c.body,'$.task_id')=observation_tasks.task_id
                    AND json_extract(c.body,'$.kind')='control'
                    ORDER BY c.rowid DESC LIMIT 1)""")
            if not has_refs:
                db.execute("""INSERT OR IGNORE INTO observation_media_refs
                    SELECT json_extract(body,'$.media_ref'),task_id,entry_id
                    FROM observation_media WHERE kind='batch'
                    AND json_extract(body,'$.media_ref') IS NOT NULL""")
                db.execute("""INSERT OR IGNORE INTO observation_media_refs
                    SELECT j.value,m.task_id,m.entry_id
                    FROM observation_media m,json_each(m.body,'$.media_parts') j
                    WHERE m.kind='batch'""")

    @contextmanager
    def _write(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _task(db, principal, task_id):
        row = db.execute(
            "SELECT * FROM observation_tasks WHERE principal=? AND task_id=?", (principal, task_id)
        ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return row

    def _receipt(self, db, principal, request_id, body):
        old = db.execute(
            "SELECT body FROM observation_commands WHERE principal=? AND request_id=?",
            (principal, request_id),
        ).fetchone()
        if old:
            if old["body"] != canonical(body):
                raise Conflict("request_id reused with different observation command")
            return True
        db.execute(
            "INSERT INTO observation_commands VALUES (?,?,?,?)",
            (principal, request_id, canonical(body), self.store.clock()),
        )
        return False

    def _guard(self, row, run, *, running=False):
        if row["version"] != run.version:
            raise Conflict("stale observation version")
        if row["generation"] != run.generation:
            raise Conflict("stale observation generation")
        if running:
            if row["status"] != "running":
                raise Conflict("observation is not running")
            self._not_expired(json.loads(row["config"]))

    def _not_expired(self, config):
        if config["expires_at"] is not None and self.store.clock() >= config["expires_at"]:
            raise Conflict("observation deadline reached")

    @staticmethod
    def _entry(row):
        return {
            **dict(row),
            "body": json.loads(row["body"]),
            "seen": json.loads(row["seen"]) if row["seen"] else None,
        }

    def _state(self, db, principal, task_id):
        row = self._task(db, principal, task_id)
        config = json.loads(row["config"])
        counts = db.execute(
            """SELECT COUNT(*) AS entry_count,
            COALESCE(SUM(kind='batch'),0) AS batch_count,
            COALESCE(SUM(seen IS NOT NULL),0) AS seen_count,
            COALESCE(SUM(json_extract(seen,'$.mode')='backfill'),0) AS backfill_count,
            COALESCE(SUM(kind='batch' AND seen IS NULL),0) AS unseen_count,
            COALESCE(SUM(gap_reason IS NOT NULL),0) AS gap_count,
            COALESCE(SUM(gap_reason IS NOT NULL AND seen IS NULL),0) AS unresolved_gap_count,
            COALESCE(SUM(CASE WHEN kind='batch' AND seen IS NULL
                THEN end_ms-start_ms ELSE 0 END),0) AS unseen_media_ms,
            COALESCE(SUM(CASE WHEN kind='gap' THEN end_ms-start_ms ELSE 0 END),0) AS missing_media_ms
            FROM observation_media WHERE task_id=?""",
            (task_id,),
        ).fetchone()
        cursors = {}
        for name, condition, order in (
            ("latest_ingested", "1", "DESC"),
            ("latest_seen", "seen IS NOT NULL", "DESC"),
            ("oldest_unseen", "kind='batch' AND seen IS NULL", "ASC"),
        ):
            item = db.execute(
                f"""SELECT entry_id,kind,sequence,source_ref,segment_id,start_ms,end_ms
                FROM observation_media WHERE task_id=? AND {condition}
                ORDER BY sequence {order} LIMIT 1""",
                (task_id,),
            ).fetchone()
            cursors[name] = dict(item) if item else None
        return {
            **dict(row),
            "config": config,
            "deadline_reached": config["expires_at"] is not None
            and self.store.clock() >= config["expires_at"],
            "ledger": {**dict(counts), **cursors},
        }

    def _result(self, db, principal, task_id, *, duplicate=False, changed=False):
        return {
            "duplicate": duplicate,
            "changed": changed,
            "task": self._state(db, principal, task_id),
        }

    def get(self, principal: str, task_id: str):
        """Current durable state and coverage; latest_seen is not complete coverage."""
        with self.store.connect() as db:
            db.execute("BEGIN")
            return self._state(db, principal, task_id)

    def unseen_in_run(self, run: ObservationRun):
        """Pending duration; explicit unavailable input stays in the coverage ledger."""
        run = _validated(ObservationRun, run)
        with self.store.connect() as db:
            self._guard(self._task(db, run.principal, run.task_id), run, running=True)
            return db.execute(
                """SELECT COALESCE(SUM(end_ms-start_ms),0) FROM observation_media
                WHERE task_id=? AND kind='batch' AND seen IS NULL
                AND COALESCE(gap_reason,'')!='media_unavailable'
                AND COALESCE(json_extract(body,'$.generation'),?)>=?""",
                (run.task_id, run.generation, run.generation),
            ).fetchone()[0]

    def run_is_current(self, run: ObservationRun):
        """Cheap cancellation fence: never scan the growing media ledger per frame."""
        run = _validated(ObservationRun, run)
        with self.store.connect() as db:
            row = db.execute(
                """SELECT status,version,generation,json_extract(config,'$.expires_at') AS expires
                FROM observation_tasks WHERE principal=? AND task_id=?""",
                (run.principal, run.task_id),
            ).fetchone()
        return bool(
            row
            and row["status"] == "running"
            and row["version"] == run.version
            and row["generation"] == run.generation
            and (row["expires"] is None or self.store.clock() < row["expires"])
        )

    @staticmethod
    def _command_effect(
        db,
        principal,
        request_id,
        task_id,
        version,
        generation,
        *,
        created_run=False,
        created_task=False,
        changed=False,
    ):
        # Same transaction as the command and state mutation: a lost response
        # cannot erase proof of which run this command actually created.
        db.execute(
            "INSERT INTO observation_command_effects VALUES (?,?,?,?,?,?,?,?)",
            (
                principal,
                request_id,
                task_id,
                version,
                generation,
                int(created_run),
                int(created_task),
                int(changed),
            ),
        )

    def apply(self, config: ObservationConfig, *, request_id: str, expected_version: int = 0):
        """Create at version 1, or replace at a paused/accepted version boundary.

        expected_version=0 means create. Reapplying identical current config is
        harmless. A stopped task is terminal even when a higher version arrives.
        Project and stable session identity cannot change within a task.
        """
        config = _validated(ObservationConfig, config)
        request_id = TypeAdapter(Reference).validate_python(request_id)
        expected_version = TypeAdapter(Counter).validate_python(expected_version)
        body = {
            "kind": "apply",
            "config": config.model_dump(mode="json"),
            "expected_version": expected_version,
        }
        with self._write() as db:
            duplicate = self._receipt(db, config.principal, request_id, body)
            if duplicate:
                return self._result(db, config.principal, config.task_id, duplicate=True)
            old = db.execute(
                "SELECT * FROM observation_tasks WHERE task_id=?", (config.task_id,)
            ).fetchone()
            wire = canonical(config)
            owner = db.execute(
                """SELECT task_id FROM observation_tasks
                WHERE json_extract(config,'$.session_ref')=? AND task_id!=?""",
                (config.session_ref, config.task_id),
            ).fetchone()
            if owner:
                raise Conflict("observation session is already bound to another task")
            if old:
                if old["principal"] != config.principal:
                    raise Conflict("observation task belongs to another principal")
                if old["config"] == wire:
                    if expected_version not in {config.version - 1, config.version}:
                        raise Conflict("incorrect expected_version for identical configuration")
                    self._command_effect(
                        db,
                        config.principal,
                        request_id,
                        config.task_id,
                        old["version"],
                        old["generation"],
                    )
                    return self._result(db, config.principal, config.task_id, duplicate=True)
                if old["version"] != expected_version or config.version != expected_version + 1:
                    raise Conflict("observation version must advance by one from expected_version")
                if old["status"] not in {"accepted", "paused"}:
                    raise Conflict("configuration changes require an accepted or paused task")
                previous = json.loads(old["config"])
                if any(
                    previous[key] != getattr(config, key) for key in ("project_id", "session_ref")
                ):
                    raise Conflict("project and session bindings are stable for the task")
                db.execute(
                    "UPDATE observation_tasks SET version=?,config=?,updated=? WHERE task_id=?",
                    (config.version, wire, self.store.clock(), config.task_id),
                )
            else:
                if config.version != 1 or expected_version != 0:
                    raise Conflict("new observation requires version 1 and expected_version 0")
                now = self.store.clock()
                db.execute(
                    """INSERT INTO observation_tasks
                    (task_id,principal,version,status,config,created,updated)
                    VALUES (?,?,1,'accepted',?,?,?)""",
                    (config.task_id, config.principal, wire, now, now),
                )
            self._command_effect(
                db,
                config.principal,
                request_id,
                config.task_id,
                config.version,
                old["generation"] if old else 0,
                created_task=old is None,
                changed=True,
            )
            return self._result(db, config.principal, config.task_id, changed=True)

    def control(self, command: ObservationCommand):
        command = _validated(ObservationCommand, command)
        with self._write() as db:
            row = self._task(db, command.principal, command.task_id)
            if self._receipt(
                db,
                command.principal,
                command.request_id,
                {"kind": "control", **command.model_dump(mode="json")},
            ):
                return self._result(db, command.principal, command.task_id, duplicate=True)
            self._guard(row, command)
            action, status, generation = command.action, row["status"], row["generation"]
            target = status
            if action == "stop":
                target = "stopped"
            elif status == "stopped":
                raise Conflict("stopped observation cannot be restarted")
            elif action in {"start", "resume"}:
                self._not_expired(json.loads(row["config"]))
                if status not in {"starting", "running"}:
                    expected = "accepted" if action == "start" else "paused"
                    if status != expected:
                        raise Conflict(f"{action} requires {expected} state")
                    target, generation = "starting", generation + 1
            elif action == "confirm_started":
                self._not_expired(json.loads(row["config"]))
                if status not in {"starting", "running"}:
                    raise Conflict("confirm_started requires starting state")
                target = "running"
            elif action == "pause":
                target = "paused"
            elif action in {"recover", "fail"}:
                if status not in {"starting", "running"}:
                    raise Conflict(f"{action} requires starting or running state")
                target = "paused"
            changed = target != status or generation != row["generation"]
            # Even an owner pause of an already fault-paused task changes the
            # current recovery intent. Receipt replay and rejected controls do
            # not overwrite it; physical lifecycle effects still use changed.
            db.execute(
                "UPDATE observation_tasks SET last_control=? WHERE task_id=?",
                (canonical({"action": action, "reason": command.reason}), command.task_id),
            )
            if changed:
                db.execute(
                    """UPDATE observation_tasks SET status=?,generation=?,reason=?,updated=?
                    WHERE task_id=?""",
                    (target, generation, command.reason, self.store.clock(), command.task_id),
                )
            self._command_effect(
                db,
                command.principal,
                command.request_id,
                command.task_id,
                row["version"],
                generation,
                created_run=action in {"start", "resume"} and generation > row["generation"],
                changed=changed,
            )
            return self._result(
                db, command.principal, command.task_id, duplicate=not changed, changed=changed
            )

    def append_batch(self, batch: ObservationBatch):
        return self._append(_validated(ObservationBatch, batch), "batch")

    def record_gap(self, gap: ObservationGap):
        """Append an explicit unavailable interval; never mark it seen."""
        return self._append(_validated(ObservationGap, gap), "gap")

    def _append(self, item, kind):
        ident = item.batch_id if kind == "batch" else item.gap_id
        with self._write() as db:
            row = self._task(db, item.principal, item.task_id)
            old = db.execute(
                "SELECT * FROM observation_media WHERE task_id=? AND entry_id=?",
                (item.task_id, ident),
            ).fetchone()
            if old:
                old_type = ObservationBatch if kind == "batch" else ObservationGap
                if old["kind"] != kind or canonical(
                    old_type.model_validate_json(old["body"])
                ) != canonical(item):
                    raise Conflict("media identity reused with different content")
                return self._result(db, item.principal, item.task_id, duplicate=True)
            self._guard(row, item, running=True)
            if item.source_ref != json.loads(row["config"])["source_ref"]:
                raise Conflict("media source does not match task binding")
            last = db.execute(
                "SELECT * FROM observation_media WHERE task_id=? ORDER BY sequence DESC LIMIT 1",
                (item.task_id,),
            ).fetchone()
            if item.sequence != (last["sequence"] + 1 if last else 0):
                raise Conflict("media sequence must be contiguous and increasing")
            if last and last["segment_id"] == item.segment_id:
                if item.source_ref != last["source_ref"] or item.start_ms != last["end_ms"]:
                    raise Conflict(
                        "overlap or undeclared gap; append an explicit gap or new segment"
                    )
            elif db.execute(
                "SELECT 1 FROM observation_media WHERE task_id=? AND segment_id=? LIMIT 1",
                (item.task_id, item.segment_id),
            ).fetchone():
                raise Conflict("cannot reopen an earlier continuous segment")
            if kind == "batch":
                first = db.execute(
                    """SELECT start_ms FROM observation_media WHERE task_id=? AND segment_id=? ORDER BY sequence LIMIT 1""",
                    (item.task_id, item.segment_id),
                ).fetchone()
                prior = db.execute(
                    """SELECT body FROM observation_media WHERE task_id=? AND segment_id=? AND kind='batch' ORDER BY sequence LIMIT 1""",
                    (item.task_id, item.segment_id),
                ).fetchone()
                if (first and item.segment_start_ms > first["start_ms"]) or (
                    prior
                    and json.loads(prior["body"]).get("segment_start_ms", 0)
                    != item.segment_start_ms
                ):
                    raise Conflict("continuous segment boundary changed")
            db.execute(
                """INSERT INTO observation_media
                (task_id,entry_id,sequence,source_ref,segment_id,start_ms,end_ms,kind,body,received_at,gap_reason)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    item.task_id,
                    ident,
                    item.sequence,
                    item.source_ref,
                    item.segment_id,
                    item.start_ms,
                    item.end_ms,
                    kind,
                    canonical(item),
                    self.store.clock(),
                    item.reason if kind == "gap" else None,
                ),
            )
            return self._result(db, item.principal, item.task_id, changed=True)

    def _batch(self, db, task_id, batch_id):
        row = db.execute(
            "SELECT * FROM observation_media WHERE task_id=? AND entry_id=? AND kind='batch'",
            (task_id, batch_id),
        ).fetchone()
        if row is None:
            raise KeyError(batch_id)
        return row

    def defer_batch(self, deferred: DeferredBatch):
        deferred = _validated(DeferredBatch, deferred)
        with self._write() as db:
            row = self._task(db, deferred.principal, deferred.task_id)
            if self._receipt(
                db,
                deferred.principal,
                deferred.request_id,
                {"kind": "defer", **deferred.model_dump(mode="json")},
            ):
                return self._result(db, deferred.principal, deferred.task_id, duplicate=True)
            self._guard(row, deferred, running=True)
            batch = self._batch(db, deferred.task_id, deferred.batch_id)
            if batch["seen"] is not None:
                raise Conflict("cannot defer an already seen batch")
            if batch["gap_reason"] is not None:
                if batch["gap_reason"] != deferred.reason:
                    raise Conflict("batch already deferred with another reason")
                return self._result(db, deferred.principal, deferred.task_id, duplicate=True)
            db.execute(
                "UPDATE observation_media SET gap_reason=? WHERE task_id=? AND entry_id=?",
                (deferred.reason, deferred.task_id, deferred.batch_id),
            )
            return self._result(db, deferred.principal, deferred.task_id, changed=True)

    def mark_seen(self, seen: SeenBatch, *, backfill_result_ids=()):
        seen = _validated(SeenBatch, seen)
        with self._write() as db:
            row = self._task(db, seen.principal, seen.task_id)
            wire = {"kind": "seen", **seen.model_dump(mode="json")}
            if backfill_result_ids:
                wire["backfill_result_ids"] = list(backfill_result_ids)
            if self._receipt(db, seen.principal, seen.request_id, wire):
                return self._result(db, seen.principal, seen.task_id, duplicate=True)
            self._guard(row, seen, running=True)
            batch = self._batch(db, seen.task_id, seen.batch_id)
            if batch["seen"] is not None:
                raise Conflict("batch already has a consumption receipt")
            config = json.loads(row["config"])
            original = json.loads(batch["body"])
            if seen.mode == "live":
                if seen.session_ref != config["session_ref"]:
                    raise Conflict("live consumption requires the bound session")
                if (
                    original["version"] != seen.version
                    or original["source_ref"] != config["source_ref"]
                ):
                    raise Conflict("old configuration media requires isolated backfill")
                if batch["gap_reason"] is not None:
                    raise Conflict("deferred media requires isolated backfill")
                earlier = db.execute(
                    """SELECT 1 FROM observation_media WHERE task_id=?
                    AND sequence<? AND kind='batch' AND seen IS NULL AND gap_reason IS NULL LIMIT 1""",
                    (seen.task_id, batch["sequence"]),
                ).fetchone()
            else:
                if seen.session_ref == config["session_ref"]:
                    raise Conflict("backfill must use an isolated session")
                earlier = db.execute(
                    """SELECT 1 FROM observation_media WHERE task_id=?
                    AND sequence<? AND kind='batch' AND seen IS NULL LIMIT 1""",
                    (seen.task_id, batch["sequence"]),
                ).fetchone()
            if earlier:
                raise Conflict("earlier unseen batches must be consumed or explicitly deferred")
            db.execute(
                "UPDATE observation_media SET seen=?,seen_at=? WHERE task_id=? AND entry_id=?",
                (canonical(seen), self.store.clock(), seen.task_id, seen.batch_id),
            )
            if seen.mode == "backfill":
                if backfill_result_ids:
                    raise Conflict("historical invocation cannot acknowledge live notifications")
                marks = []
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='observation_decisions'"
                ).fetchone():
                    marks = [
                        dict(r)
                        for r in db.execute(
                            """SELECT id,decision,start_ms,end_ms,event_id,reason FROM observation_decisions
                        WHERE task_id=? AND batch_id=? ORDER BY start_ms""",
                            (seen.task_id, seen.batch_id),
                        )
                    ]
                ident = "backfill-" + digest([seen.task_id, seen.batch_id])[:32]
                result = {
                    "id": ident,
                    "kind": "historical_observation_result",
                    "batch_id": seen.batch_id,
                    "source_ref": original["source_ref"],
                    "segment_id": original["segment_id"],
                    "range_ms": [original["start_ms"], original["end_ms"]],
                    "instruction_version": original["version"],
                    "receipt": seen.model_dump(mode="json"),
                    "decisions": marks,
                    "simulated": original["simulated"],
                    "current_camera_authority": False,
                }
                db.execute(
                    "INSERT INTO observation_backfill_results VALUES (?,?,?,?,NULL)",
                    (ident, seen.task_id, seen.batch_id, canonical(result)),
                )
            else:
                for ident in backfill_result_ids:
                    result = db.execute(
                        "SELECT * FROM observation_backfill_results WHERE id=? AND task_id=?",
                        (ident, seen.task_id),
                    ).fetchone()
                    if result is None or result["delivered_by"] not in (None, seen.batch_id):
                        raise Conflict("backfill notification was not pending for this task")
                    db.execute(
                        "UPDATE observation_backfill_results SET delivered_by=? WHERE id=?",
                        (seen.batch_id, ident),
                    )
            return self._result(db, seen.principal, seen.task_id, changed=True)

    def instruction_config(self, principal, task_id, version):
        with self.store.connect() as db:
            self._task(db, principal, task_id)
            row = db.execute(
                """SELECT body FROM observation_commands WHERE principal=?
                AND json_extract(body,'$.kind')='apply' AND json_extract(body,'$.config.task_id')=?
                AND json_extract(body,'$.config.version')=? ORDER BY created LIMIT 1""",
                (principal, task_id, version),
            ).fetchone()
            if row is None:
                raise Conflict("historical configuration is unavailable")
            return ObservationConfig.model_validate(json.loads(row["body"])["config"])

    def has_live_pending(self, run):
        with self.store.connect() as db:
            task = self._task(db, run.principal, run.task_id)
            self._guard(task, run, running=True)
            return bool(
                db.execute(
                    """SELECT 1 FROM observation_media WHERE task_id=? AND kind='batch'
                AND seen IS NULL AND gap_reason IS NULL LIMIT 1""",
                    (run.task_id,),
                ).fetchone()
            )

    def next_backfill(self, run, *, source_exhausted=False):
        with self.store.connect() as db:
            task = self._task(db, run.principal, run.task_id)
            self._guard(task, run, running=True)
            # Never skip an older unresolved input to make a retry look caught up.
            row = db.execute(
                """SELECT m.*,COALESCE(r.next_try,0) AS next_try,r.error AS retry_reason FROM observation_media m
                LEFT JOIN observation_backfill_retries r ON r.task_id=m.task_id AND r.batch_id=m.entry_id
                WHERE m.task_id=? AND m.kind='batch' AND m.seen IS NULL AND COALESCE(m.gap_reason,'') NOT IN ('unanalysed_expired','retention_expired','media_unavailable') ORDER BY m.sequence LIMIT 1""",
                (run.task_id,),
            ).fetchone()
            if row is None or row["gap_reason"] is None:
                return None
            # Once a finite input is sealed there is no future fresh frame to
            # prioritize. Only lift preemption backoff, never model-error backoff.
            preemption_ended = source_exhausted and row["retry_reason"] == "fresh_input"
            if row["next_try"] > self.store.clock() and not preemption_ended:
                return None
            return json.loads(row["body"])

    def retry_backfill(self, run, batch_id, error, seconds):
        with self._write() as db:
            task = self._task(db, run.principal, run.task_id)
            self._guard(task, run, running=True)
            self._batch(db, run.task_id, batch_id)
            db.execute(
                """INSERT INTO observation_backfill_retries VALUES (?,?,?,?)
                ON CONFLICT(task_id,batch_id) DO UPDATE SET next_try=excluded.next_try,error=excluded.error""",
                (run.task_id, batch_id, self.store.clock() + seconds, error),
            )

    def entries(self, principal: str, task_id: str, *, after_sequence: int = -1, limit: int = 100):
        """Bounded ledger page, including original inputs, gaps and seen evidence."""
        if (
            type(after_sequence) is not int
            or after_sequence < -1
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise ValueError("invalid ledger page bounds")
        with self.store.connect() as db:
            self._task(db, principal, task_id)
            return [
                self._entry(row)
                for row in db.execute(
                    """SELECT * FROM observation_media
                WHERE task_id=? AND sequence>? ORDER BY sequence LIMIT ?""",
                    (task_id, after_sequence, limit),
                )
            ]

    def next_live(
        self,
        run: ObservationRun,
        *,
        latest_after_seconds: float,
        preserve_order=False,
        after_sequence=-1,
    ):
        """Choose one durable input reference, exposing any skipped intervals.

        The single Gateway consumer owns the in-flight call. Fresh pending input
        stays FIFO. When the oldest pending window is stale, older references
        become explicit backfill debt; this is a fault/degradation, never a
        throughput pass. No video bytes are discarded by this operation.
        """
        import math

        run = _validated(ObservationRun, run)
        if (
            type(after_sequence) is not int
            or after_sequence < -1
            or (after_sequence != -1 and not preserve_order)
        ):
            raise ValueError("staged cursor requires ordered selection")
        if type(preserve_order) is not bool:
            raise ValueError("ordered replay policy must be explicit boolean")
        if (
            type(latest_after_seconds) not in {int, float}
            or not math.isfinite(latest_after_seconds)
            or latest_after_seconds <= 0
        ):
            raise ValueError("invalid live observation age limit")
        with self._write() as db:
            task = self._task(db, run.principal, run.task_id)
            self._guard(task, run, running=True)
            db.execute(
                """UPDATE observation_media SET gap_reason='interrupted_or_reconfigured_run'
                WHERE task_id=? AND kind='batch' AND seen IS NULL AND gap_reason IS NULL
                AND (json_extract(body,'$.generation')!=? OR json_extract(body,'$.version')!=?)""",
                (run.task_id, run.generation, run.version),
            )
            query = """SELECT * FROM observation_media WHERE task_id=? AND kind='batch'
                AND seen IS NULL AND gap_reason IS NULL AND sequence>? ORDER BY sequence """
            first = db.execute(query + "ASC LIMIT 1", (run.task_id, after_sequence)).fetchone()
            if first is None:
                return None
            candidate = first
            if (
                not preserve_order
                and self.store.clock() - json.loads(first["body"])["captured_at"]
                > latest_after_seconds
            ):
                candidate = db.execute(
                    query + "DESC LIMIT 1", (run.task_id, after_sequence)
                ).fetchone()
                db.execute(
                    """UPDATE observation_media SET gap_reason='live_backlog_deferred'
                    WHERE task_id=? AND kind='batch' AND seen IS NULL AND gap_reason IS NULL AND sequence<?""",
                    (run.task_id, candidate["sequence"]),
                )
            cursor = db.execute(
                """SELECT COALESCE(MAX(sequence),-1) FROM observation_media
                WHERE task_id=? AND seen IS NOT NULL AND json_extract(seen,'$.mode')='live' """,
                (run.task_id,),
            ).fetchone()[0]
            gaps = []
            for row in db.execute(
                """SELECT sequence,source_ref,segment_id,start_ms,end_ms,gap_reason
                FROM observation_media WHERE task_id=? AND sequence>? AND sequence<?
                AND gap_reason IS NOT NULL AND seen IS NULL ORDER BY sequence""",
                (run.task_id, cursor, candidate["sequence"]),
            ):
                item = dict(row)
                if (
                    gaps
                    and all(
                        gaps[-1][key] == item[key]
                        for key in ("source_ref", "segment_id", "gap_reason")
                    )
                    and gaps[-1]["end_ms"] == item["start_ms"]
                ):
                    gaps[-1]["end_ms"] = item["end_ms"]
                    gaps[-1]["through_sequence"] = item["sequence"]
                else:
                    gaps.append({**item, "through_sequence": item["sequence"]})
            results = [
                json.loads(row[0])
                for row in db.execute(
                    """SELECT r.body FROM observation_backfill_results r
                JOIN observation_media m ON m.task_id=r.task_id AND m.entry_id=r.batch_id
                WHERE r.task_id=? AND r.delivered_by IS NULL ORDER BY m.sequence LIMIT 1""",
                    (run.task_id,),
                )
            ]
            return {
                "batch": json.loads(candidate["body"]),
                "gaps": gaps,
                "backfill_results": results,
            }
