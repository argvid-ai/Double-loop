"""One durable scheduler: native NanoBot calendar calculation, transactional task wakeups.

NanoBot CronService normally persists a separate JSON store and calls a callback.
Here a callback and task version must commit together. Reuse its schedule type and
next-run calculation, but keep the only clock owner in the host's SQLite poller.
No second native service or model polling loop is started.
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from nanobot.cron.service import _compute_next_run
from nanobot.cron.types import CronSchedule

from .contracts import canonical, digest
from .store import Conflict


class MatterScheduler:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS wake_rules (
                    task_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    spec TEXT NOT NULL, next_due REAL, enabled INTEGER NOT NULL,
                    deleted INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS wake_occurrences (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, rule_revision INTEGER NOT NULL,
                    due REAL NOT NULL, status TEXT NOT NULL, reason TEXT,
                    event_id TEXT, task_version INTEGER, created REAL NOT NULL
                );
            """)
            # Only imported once; the old table stops being a scheduling owner.
            for row in db.execute("SELECT * FROM harness_timers").fetchall():
                spec = {
                    "schedule": "interval",
                    "interval_seconds": row["interval_seconds"],
                    "timezone": "Asia/Shanghai",
                    "delivery_side": store.side,
                    "delivery_channel": "web",
                }
                db.execute(
                    "INSERT OR IGNORE INTO wake_rules VALUES (?,1,?,?,?,0,?)",
                    (
                        row["task_id"],
                        canonical(spec),
                        row["next_due"],
                        row["enabled"],
                        store.clock(),
                    ),
                )
            db.execute("DELETE FROM harness_timers")

    @staticmethod
    def next_due(spec, after):
        if spec["schedule"] == "daily":
            hour, minute = map(int, spec["local_time"].split(":"))
            schedule = CronSchedule(kind="cron", expr=f"{minute} {hour} * * *", tz=spec["timezone"])
        elif spec["schedule"] == "interval":
            schedule = CronSchedule(kind="every", every_ms=spec["interval_seconds"] * 1000)
        else:
            schedule = CronSchedule(
                kind="at", at_ms=int(datetime.fromisoformat(spec["run_at"]).timestamp() * 1000)
            )
        value = _compute_next_run(schedule, int(after * 1000))
        if value is None and spec["schedule"] != "once":
            raise ValueError("invalid schedule")
        due = value / 1000 if value is not None else None
        if spec.get("end_at"):
            end = datetime.fromisoformat(spec["end_at"]).timestamp()
            if after >= end:
                return None
            due = min(due, end) if due is not None else end
        return due

    @staticmethod
    def decode(row):
        if row is None:
            return None
        return {
            **dict(row),
            "spec": json.loads(row["spec"]),
            "enabled": bool(row["enabled"]),
            "deleted": bool(row["deleted"]),
        }

    def list(self, principal, *, task_id=None, include_deleted=False):
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT r.* FROM wake_rules r JOIN business_tasks b ON b.id=r.task_id
                WHERE b.principal=? AND (r.deleted=0 OR ?) AND (? IS NULL OR r.task_id=?)
                ORDER BY r.updated DESC""",
                (principal, include_deleted, task_id, task_id),
            ).fetchall()
            result = []
            for row in rows:
                last = db.execute(
                    "SELECT * FROM wake_occurrences WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
                    (row["task_id"],),
                ).fetchone()
                result.append({**self.decode(row), "last_occurrence": dict(last) if last else None})
            return result

    def set(self, principal, args, *, source_event):
        data = args.model_dump(mode="json")
        spec = {
            k: v for k, v in data.items() if k not in {"task_id", "enabled", "expected_revision"}
        }
        now = self.store.clock()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.store._task(db, args.task_id, principal)
            if task["status"] in {"completed", "cancelled", "expired"}:
                raise Conflict("terminal task cannot be scheduled")
            if task["kind"] not in {"goal", "report"} and args.schedule != "once":
                raise Conflict(
                    "recurring rule requires kind=goal or report. Update this task with save_task(task_id, expected_version, kind=goal); do not cancel and recreate it."
                )
            if args.output_kind == "video" and task["kind"] != "goal":
                raise Conflict("periodic video delivery requires a goal matter")
            old = db.execute("SELECT * FROM wake_rules WHERE task_id=?", (args.task_id,)).fetchone()
            if (
                old
                and args.next_wake_in_seconds is None
                and not old["deleted"]
                and old["spec"] == canonical(spec)
                and bool(old["enabled"]) == args.enabled
            ):
                return self.decode(old)
            if old and args.expected_revision != old["revision"]:
                raise Conflict("stale rule revision")
            if not old and args.expected_revision is not None:
                raise Conflict("rule does not exist")
            next_due = None if args.start_on_observation else self.next_due(spec, now)
            if args.next_wake_in_seconds is not None:
                next_due = now + args.next_wake_in_seconds
                if spec.get("end_at"):
                    next_due = min(next_due, datetime.fromisoformat(spec["end_at"]).timestamp())
            if args.enabled and next_due is None and not args.start_on_observation:
                raise Conflict("run_at must be in the future")
            revision = old["revision"] + 1 if old else 1
            db.execute(
                "INSERT OR REPLACE INTO wake_rules VALUES (?,?,?,?,?,0,?)",
                (args.task_id, revision, canonical(spec), next_due, int(args.enabled), now),
            )
            self._supersede_queued(db, args.task_id, "rule_replaced")
            self.store._audit(
                db,
                args.task_id,
                "rule.saved",
                {
                    "source_event": source_event,
                    "revision": revision,
                    "spec": spec,
                    "enabled": args.enabled,
                    "next_due": next_due,
                },
            )
            return self.decode(
                db.execute("SELECT * FROM wake_rules WHERE task_id=?", (args.task_id,)).fetchone()
            )

    def control(self, principal, args, *, source_event):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.store._task(db, args.task_id, principal)
            row = db.execute("SELECT * FROM wake_rules WHERE task_id=?", (args.task_id,)).fetchone()
            if not row or row["deleted"]:
                raise KeyError(args.task_id)
            if row["revision"] != args.expected_revision:
                raise Conflict("stale rule revision")
            enabled = args.action == "enable"
            if enabled and task["status"] in {"completed", "cancelled", "expired"}:
                raise Conflict("task ended")
            due = row["next_due"]
            if enabled:
                spec = json.loads(row["spec"])
                due = (
                    None
                    if spec.get("start_on_observation")
                    else self.next_due(spec, self.store.clock())
                )
                if due is None and not spec.get("start_on_observation"):
                    raise Conflict("rule has no future occurrence")
            db.execute(
                "UPDATE wake_rules SET enabled=?,deleted=?,revision=revision+1,next_due=?,updated=? WHERE task_id=?",
                (int(enabled), int(args.action == "delete"), due, self.store.clock(), args.task_id),
            )
            self._supersede_queued(db, args.task_id, "rule_" + args.action)
            self.store._audit(
                db,
                args.task_id,
                "rule.control",
                {
                    "source_event": source_event,
                    "action": args.action,
                    "revision": row["revision"] + 1,
                },
            )
            return self.decode(
                db.execute("SELECT * FROM wake_rules WHERE task_id=?", (args.task_id,)).fetchone()
            )

    def observe_ready(self, principal, observation_task_id):
        """Arm an explicitly waiting rule after the bound source has real input."""
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT r.* FROM wake_rules r JOIN business_tasks b ON b.id=r.task_id
                WHERE b.principal=? AND b.status NOT IN ('paused','cancelled','completed','expired')
                AND r.enabled=1 AND r.deleted=0 AND r.next_due IS NULL
                AND json_extract(r.spec,'$.start_on_observation')=1
                AND json_extract(r.spec,'$.observation_task_id')=?""",
                (principal, observation_task_id),
            ).fetchall()
            for row in rows:
                due = self.next_due(json.loads(row["spec"]), self.store.clock())
                db.execute(
                    "UPDATE wake_rules SET next_due=?,enabled=?,updated=? WHERE task_id=?",
                    (due, int(due is not None), self.store.clock(), row["task_id"]),
                )
                self.store._audit(
                    db,
                    row["task_id"],
                    "rule.observation_ready",
                    {"observation_task_id": observation_task_id, "next_due": due},
                )
        return len(rows)

    def _supersede_queued(self, db, task_id, reason):
        rows = db.execute(
            "SELECT id,event_id FROM wake_occurrences WHERE task_id=? AND status='queued'",
            (task_id,),
        ).fetchall()
        for row in rows:
            updated = db.execute(
                "UPDATE harness_events SET status='superseded' WHERE id=? AND status='queued'",
                (row["event_id"],),
            ).rowcount
            if updated:
                db.execute(
                    "UPDATE wake_occurrences SET status='skipped',reason=? WHERE id=?",
                    (reason, row["id"]),
                )

    def due(self, *, now=None):
        now = self.store.clock() if now is None else now
        if getattr(self, "autonomy", None) is not None:
            self.autonomy.due(now)
        delivered = []
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT r.*,b.principal,b.kind AS task_kind,b.status AS task_status FROM wake_rules r
                JOIN business_tasks b ON b.id=r.task_id
                WHERE r.enabled=1 AND r.deleted=0 AND r.next_due<=? ORDER BY r.next_due""",
                (now,),
            ).fetchall()
            for row in rows:
                spec = json.loads(row["spec"])
                ident = (
                    "timer-"
                    + digest(
                        {
                            "task": row["task_id"],
                            "revision": row["revision"],
                            "due": row["next_due"],
                        }
                    )[:32]
                )
                active = db.execute(
                    "SELECT 1 FROM harness_events WHERE task_id=? AND status IN ('queued','running') AND read_only=0",
                    (row["task_id"],),
                ).fetchone()
                reason = (
                    row["task_status"]
                    if row["task_status"]
                    in {"paused", "cancelled", "completed", "expired", "running", "waiting_peer"}
                    else "previous_run_active"
                    if active
                    else None
                )
                end_at = (
                    datetime.fromisoformat(spec["end_at"]).timestamp()
                    if spec.get("end_at")
                    else None
                )
                final = end_at is not None and row["next_due"] >= end_at
                if final and reason in {"previous_run_active", "running", "waiting_peer"}:
                    continue  # Do not silently lose the last delivery while prior work drains.
                version = None
                if reason is None:
                    if row["task_kind"] != "goal":
                        db.execute(
                            "UPDATE business_tasks SET version=version+1,status='ready',result=NULL,updated=? WHERE id=?",
                            (now, row["task_id"]),
                        )
                    version = db.execute(
                        "SELECT version FROM business_tasks WHERE id=?", (row["task_id"],)
                    ).fetchone()[0]
                    local = datetime.fromtimestamp(row["next_due"], ZoneInfo(spec["timezone"]))
                    delivered.append(
                        self.store.submit(
                            ident,
                            row["principal"],
                            "system",
                            "timer",
                            {
                                "text": (
                                    "本任务约定的本期视频交付到期。在当前任务对话内读取制作轮次、实际素材与交付要求，完成选材、制作及复看，不另建剪辑子任务。不改变共享录像。"
                                    if spec.get("output_kind") == "video"
                                    else "本任务已保存的查看安排到期。继续当前任务要求；这是时间提醒，不是新的任务或授权。"
                                    if spec.get("output_kind") == "review"
                                    else "事项规则到期；读取本任务与本期相关记录后继续。"
                                ),
                                "due": row["next_due"],
                                "occurrence_id": ident,
                                "rule_revision": row["revision"],
                                "final_occurrence": final,
                                "output_kind": spec.get("output_kind", "text"),
                                "observation_task_id": spec.get("observation_task_id"),
                                "period_seconds": spec.get("interval_seconds"),
                                "task_version": version,
                                "scheduled_local": local.isoformat(),
                                "delivery_side": spec["delivery_side"],
                                "delivery_channel": spec["delivery_channel"],
                            },
                            task_id=row["task_id"],
                            _db=db,
                        )
                    )
                db.execute(
                    "INSERT OR IGNORE INTO wake_occurrences VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        ident,
                        row["task_id"],
                        row["revision"],
                        row["next_due"],
                        "skipped" if reason else "queued",
                        reason,
                        ident if reason is None else None,
                        version,
                        self.store.clock(),
                    ),
                )
                # Missed dates are coalesced into one bounded wake, never an
                # unbounded backlog. A paused/busy occurrence is recorded, skipped.
                if spec["schedule"] == "interval":
                    interval = spec["interval_seconds"]
                    steps = int((now - row["next_due"]) // interval) + 1
                    due = row["next_due"] + steps * interval
                    if end_at is not None:
                        due = None if final else min(due, end_at)
                else:
                    due = self.next_due(spec, now)
                db.execute(
                    "UPDATE wake_rules SET next_due=?,enabled=? WHERE task_id=?",
                    (due, int(due is not None), row["task_id"]),
                )
                self.store._audit(
                    db,
                    row["task_id"],
                    "rule.occurrence",
                    {
                        "occurrence_id": ident,
                        "event_id": ident if not reason else None,
                        "reason": reason,
                        "next_due": due,
                        "scheduled_due": row["next_due"],
                    },
                )
        return delivered
