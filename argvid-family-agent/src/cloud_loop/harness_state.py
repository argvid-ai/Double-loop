"""Durable business records and event scheduling; never a model/tool loop."""

from __future__ import annotations

import json
import re
import time
import uuid
from contextlib import nullcontext

from .contracts import ID, canonical, digest
from .dual_store import DualStore
from .store import Conflict


class HarnessStore(DualStore):
    def __init__(self, root, side, *, clock=time.time):
        super().__init__(root, side)
        self.clock = clock
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS business_tasks (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, kind TEXT NOT NULL,
                    goal TEXT NOT NULL, version INTEGER NOT NULL, status TEXT NOT NULL,
                    constraints TEXT NOT NULL, result TEXT, created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS harness_events (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, channel TEXT NOT NULL,
                    kind TEXT NOT NULL, task_id TEXT, body TEXT NOT NULL, read_only INTEGER NOT NULL,
                    status TEXT NOT NULL, session_key TEXT, reply TEXT, error TEXT,
                    created REAL NOT NULL, finished REAL
                );
                CREATE TABLE IF NOT EXISTS short_sessions (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, interaction_id TEXT NOT NULL,
                    turns INTEGER NOT NULL, last_used REAL NOT NULL, closed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS record_history (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, task_id TEXT,
                    kind TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS user_memory (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, key TEXT NOT NULL,
                    text TEXT NOT NULL, version INTEGER NOT NULL, source TEXT NOT NULL,
                    updated REAL NOT NULL, UNIQUE(principal,key)
                );
                CREATE TABLE IF NOT EXISTS harness_jobs (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, version INTEGER NOT NULL,
                    kind TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
                    created REAL NOT NULL, UNIQUE(task_id,version,kind)
                );
                CREATE TABLE IF NOT EXISTS task_waits (
                    task_id TEXT PRIMARY KEY, version INTEGER NOT NULL, operation TEXT NOT NULL, reason TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS harness_job_plans (
                    job_id TEXT PRIMARY KEY, plan TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS harness_timers (
                    task_id TEXT PRIMARY KEY, interval_seconds INTEGER NOT NULL,
                    next_due REAL NOT NULL, enabled INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS peer_mail (
                    id TEXT PRIMARY KEY, direction TEXT NOT NULL, body TEXT NOT NULL,
                    status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_try REAL NOT NULL DEFAULT 0, result TEXT
                );
                CREATE TABLE IF NOT EXISTS task_routes (
                    task_id TEXT PRIMARY KEY, source_event TEXT NOT NULL,
                    principal TEXT NOT NULL, channel TEXT NOT NULL, peer_request TEXT
                );
                CREATE TABLE IF NOT EXISTS peer_task_bindings (
                    request_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
                    principal TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transferred_assets (
                    request_id TEXT NOT NULL, asset_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, task_version INTEGER NOT NULL,
                    principal TEXT NOT NULL, sha256 TEXT NOT NULL,
                    PRIMARY KEY(request_id,asset_id)
                );
                CREATE TABLE IF NOT EXISTS run_task_versions (
                    event_id TEXT NOT NULL, task_id TEXT NOT NULL, version INTEGER NOT NULL,
                    PRIMARY KEY(event_id,task_id)
                );
                CREATE TABLE IF NOT EXISTS memory_retirements (
                    memory_id TEXT PRIMARY KEY, superseded_by TEXT NOT NULL,
                    source_event TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS record_mutations (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, request TEXT NOT NULL,
                    receipt TEXT NOT NULL, created REAL NOT NULL
                );
            """)

        from .harness_sessions import ConversationDirectory

        self.conversations = ConversationDirectory(self)
        from .harness_context_mail import ContextMail

        self.context_mail = ContextMail(self)
        from .harness_scheduler import MatterScheduler

        self.scheduler = MatterScheduler(self)
        from .harness_delivery import ResultDelivery

        self.delivery = ResultDelivery(self)

    @staticmethod
    def _identity(value):
        if not isinstance(value, str) or not re.fullmatch(ID, value):
            raise ValueError("invalid identity")

    @staticmethod
    def _task(db, ident, principal):
        row = db.execute(
            "SELECT * FROM business_tasks WHERE id=? AND principal=?", (ident, principal)
        ).fetchone()
        if row is None:
            raise KeyError(ident)
        schedule = db.execute(
            "SELECT * FROM wake_rules WHERE task_id=? AND deleted=0", (ident,)
        ).fetchone()
        route = db.execute(
            "SELECT source_event,channel,peer_request FROM task_routes WHERE task_id=?", (ident,)
        ).fetchone()
        parent = json.loads(route["peer_request"]) if route and route["peer_request"] else None
        origin = (
            {
                "source_event": route["source_event"],
                "channel": route["channel"],
                "parent_side": parent["sender"] if parent else None,
                "parent_task_id": parent["task_id"] if parent else None,
                "parent_version": parent["task_version"] if parent else None,
                "request_id": parent["id"] if parent else None,
            }
            if route
            else None
        )
        wait = db.execute(
            "SELECT version,operation,reason FROM task_waits WHERE task_id=? AND version=?",
            (ident, row["version"]),
        ).fetchone()
        return {
            **dict(row),
            "origin": origin,
            "wait": dict(wait) if wait else None,
            "constraints": json.loads(row["constraints"]),
            "schedule": {**dict(schedule), "spec": json.loads(schedule["spec"])}
            if schedule
            else None,
            "result": json.loads(row["result"]) if row["result"] else None,
        }

    def business_task(self, ident, principal):
        with self.connect() as db:
            return self._task(db, ident, principal)

    @staticmethod
    def _history(db, ident, principal, task_id, kind, text, source, now):
        db.execute(
            "INSERT INTO record_history VALUES (?,?,?,?,?,?,?)",
            (ident, principal, task_id, kind, text, source, now),
        )

    def submit(
        self, ident, principal, channel, kind, body, *, task_id=None, read_only=False, _db=None
    ):
        self._identity(ident)
        self._identity(principal)
        if channel not in {"web", "voice", "peer", "system", "feishu"}:
            raise ValueError("invalid reply channel")
        with nullcontext(_db) if _db is not None else self.connect() as db:
            if _db is None:
                db.execute("BEGIN IMMEDIATE")
            if task_id:
                self._task(db, task_id, principal)
            if body.get("conversation_id"):
                selected = self.conversations.get(principal, body["conversation_id"], db=db)
                if (
                    task_id
                    and selected["kind"] == "work"
                    and selected["task_id"] not in {None, task_id}
                ):
                    raise Conflict("input task does not belong to selected conversation")
            old = db.execute("SELECT * FROM harness_events WHERE id=?", (ident,)).fetchone()
            wire = canonical(body)
            if old:
                if (
                    old["principal"],
                    old["channel"],
                    old["kind"],
                    old["task_id"],
                    old["body"],
                    old["read_only"],
                ) != (principal, channel, kind, task_id, wire, int(read_only)):
                    raise Conflict("event identity conflict")
                return {"event_id": ident, "duplicate": True, "status": old["status"]}
            now = self.clock()
            db.execute(
                """INSERT INTO harness_events
                (id,principal,channel,kind,task_id,body,read_only,status,created)
                VALUES (?,?,?,?,?,?,?,'queued',?)""",
                (ident, principal, channel, kind, task_id, wire, int(read_only), now),
            )
            self._history(
                db,
                "input:" + ident,
                principal,
                task_id,
                kind,
                body.get("text", canonical(body)),
                ident,
                now,
            )
            self._audit(
                db,
                task_id,
                "harness.input",
                {
                    "event_id": ident,
                    "principal": principal,
                    "channel": channel,
                    "kind": kind,
                    "read_only": read_only,
                    "body": body,
                },
            )
        return {"event_id": ident, "duplicate": False, "status": "queued"}

    def event(self, ident, principal, *, _db=None):
        with nullcontext(_db) if _db is not None else self.connect() as db:
            row = db.execute(
                "SELECT * FROM harness_events WHERE id=? AND principal=?", (ident, principal)
            ).fetchone()
            if row is None:
                raise KeyError(ident)
            return {
                **dict(row),
                "body": json.loads(row["body"]),
                "reply": json.loads(row["reply"]) if row["reply"] else None,
            }

    def events_for(self, principal, limit=50):
        with self.connect() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM harness_events WHERE principal=? ORDER BY created DESC LIMIT ?",
                    (principal, min(max(limit, 1), 100)),
                )
            ]
        return [self.event(ident, principal) for ident in ids]

    def claim_event(self, settings, lane=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("""SELECT * FROM harness_events WHERE status='queued'
                ORDER BY CASE WHEN kind IN ('user','voice') THEN 0 ELSE 1 END,created,rowid""").fetchall()
            for row in rows:
                if lane == "interactive" and row["kind"] not in {"user", "voice"}:
                    continue
                if lane == "background" and row["kind"] in {"user", "voice"}:
                    continue
                if (
                    row["task_id"]
                    and not row["read_only"]
                    and row["kind"] not in {"user", "voice", "peer.task", "work.message"}
                ):
                    task = self._task(db, row["task_id"], row["principal"])
                    body = json.loads(row["body"])
                    reason = (
                        "task_not_active"
                        if task["status"] in {"paused", "cancelled", "completed", "expired"}
                        else "stale_task_version"
                        if body.get("task_version") is not None
                        and body["task_version"] != task["version"]
                        else None
                    )
                    if (
                        reason is None
                        and row["kind"] == "task.step_completed"
                        and db.execute(
                            "SELECT 1 FROM harness_jobs WHERE task_id=? AND version=? AND kind='compose' AND status='completed'",
                            (task["id"], task["version"]),
                        ).fetchone()
                    ):
                        reason = "pipeline_already_complete"
                    if reason:
                        reply = {
                            "text": "这次后台唤醒已因任务状态或版本变化跳过。",
                            "reason": reason,
                            "model_invoked": False,
                        }
                        db.execute(
                            "UPDATE harness_events SET status='superseded',reply=?,finished=? WHERE id=?",
                            (canonical(reply), self.clock(), row["id"]),
                        )
                        db.execute(
                            "UPDATE wake_occurrences SET status='skipped',reason=? WHERE event_id=?",
                            (reason, row["id"]),
                        )
                        self._audit(
                            db, row["task_id"], "harness.skipped", {"event_id": row["id"], **reply}
                        )
                        continue
                conversation = self.conversations.route(row, db=db)
                key = conversation["id"] if conversation else None
                if (
                    key
                    and db.execute(
                        "SELECT 1 FROM harness_events WHERE status='running' AND session_key=?",
                        (key,),
                    ).fetchone()
                ):
                    continue
                if row["task_id"] and not row["read_only"]:
                    task = self._task(db, row["task_id"], row["principal"])
                    db.execute(
                        "INSERT OR IGNORE INTO run_task_versions VALUES (?,?,?)",
                        (row["id"], task["id"], task["version"]),
                    )
                if key:
                    db.execute(
                        "UPDATE harness_conversations SET status='running',turns=turns+1,updated=? WHERE id=?",
                        (self.clock(), key),
                    )
                db.execute(
                    "UPDATE harness_events SET status='running',session_key=? WHERE id=?",
                    (key, row["id"]),
                )
                self._audit(
                    db,
                    row["task_id"],
                    "conversation.routed",
                    {
                        "event_id": row["id"],
                        "conversation_id": key,
                        "alias": conversation["alias"] if conversation else None,
                        "trigger": row["kind"],
                        "reason": "program_receipt"
                        if key is None
                        else "fixed_main"
                        if conversation["kind"] == "main"
                        else "associated_work",
                    },
                )
                return self.event(row["id"], row["principal"], _db=db)
        return None

    def finish_event(self, event, reply, *, error=None, _db=None):
        with nullcontext(_db) if _db is not None else self.connect() as db:
            if _db is None:
                db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM harness_events WHERE id=?", (event["id"],)).fetchone()
            if (
                old is None
                or old["principal"] != event["principal"]
                or old["session_key"] != event["session_key"]
            ):
                raise Conflict("run no longer owns event")
            if old["status"] == "superseded":
                result = {
                    "text": "这条较早的操作请求已被后续控制指令中止。",
                    "reply_kind": "receipt",
                    "event_id": event["id"],
                    "principal": event["principal"],
                    "channel": old["channel"],
                    "session_key": event["session_key"],
                    "source": "superseded_input",
                }
                db.execute(
                    "UPDATE harness_events SET reply=?,finished=? WHERE id=?",
                    (canonical(result), self.clock(), event["id"]),
                )
                self._audit(db, old["task_id"], "harness.superseded", result)
                if event["session_key"]:
                    db.execute(
                        "UPDATE harness_conversations SET status='idle',updated=? WHERE id=?",
                        (self.clock(), event["session_key"]),
                    )
                return result
            if old["status"] != "running":
                raise Conflict("event already finished")
            now = self.clock()
            result = {
                **reply,
                "event_id": event["id"],
                "principal": event["principal"],
                "channel": old["channel"],
                "session_key": event["session_key"],
            }
            db.execute(
                "UPDATE harness_events SET status=?,reply=?,error=?,finished=? WHERE id=?",
                ("failed" if error else "done", canonical(result), error, now, event["id"]),
            )
            occurrence_status, occurrence_reason = "done", None
            if error:
                occurrence_status, occurrence_reason = "failed", error
            elif reply.get("period_skipped"):
                occurrence_status, occurrence_reason = "skipped", reply["period_skip_reason"]
            db.execute(
                "UPDATE wake_occurrences SET status=?,reason=? WHERE event_id=?",
                (occurrence_status, occurrence_reason, event["id"]),
            )
            self._history(
                db,
                "reply:" + event["id"],
                event["principal"],
                old["task_id"],
                "reply",
                reply.get("text", ""),
                event["id"],
                now,
            )
            self._audit(db, old["task_id"], "harness.reply", result)
            if event["session_key"]:
                db.execute(
                    "UPDATE harness_conversations SET status='idle',updated=? WHERE id=?",
                    (now, event["session_key"]),
                )
        return result

    def project_origin_notification(self, event, reply, *, db):
        """Project a work result to its original main window without waking a model."""
        from .harness_delivery_policy import routine_review

        self.context_mail.project(event, reply, db=db)
        if (
            not self.delivery.work_result(event, reply, db)
            or
            event["kind"]
            not in {
                "job.result",
                "media.review",
                "autonomy",
                "work.message",
                "storage.review",
                "peer.result",
                "peer.failed",
                "timer",
            }
            or reply.get("pending_receipt")
            or reply.get("quiet")
            or routine_review(event, reply)
            or (reply.get("published_reports") and not reply.get("error"))
        ):
            return
        # A committed publication owns delivery across process restarts too.
        if (
            not reply.get("error")
            and db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='matter_reports'"
            ).fetchone()
        ):
            published = db.execute(
                "SELECT 1 FROM matter_reports WHERE source_event=? AND principal=? LIMIT 1",
                (event["id"], event["principal"]),
            ).fetchone()
            if published:
                return
        origin_id = (
            event["id"]
            if event["kind"] in {"autonomy", "work.message", "storage.review"}
            else event["body"].get("origin_event_id")
        )
        if not origin_id and event.get("task_id"):
            route = db.execute(
                "SELECT source_event FROM task_routes WHERE task_id=?", (event["task_id"],)
            ).fetchone()
            origin_id = route["source_event"] if route else None
        if not origin_id:
            return
        origin = db.execute(
            "SELECT * FROM harness_events WHERE id=? AND principal=?",
            (origin_id, event["principal"]),
        ).fetchone()
        # A periodic edit belongs to the user's original goal. Follow its timer
        # origin, rather than leaving the completed film only in a work session.
        visited = set()
        while (
            origin
            and origin["kind"] in {"timer", "media.review", "task.step_completed"}
            and origin["id"] not in visited
        ):
            visited.add(origin["id"])
            trigger = json.loads(origin["body"])
            if (
                origin["kind"] == "timer"
                and trigger.get("final_occurrence")
                and event["kind"] in {"job.result", "media.review"}
                and not reply.get("error")
            ):
                result = reply.get("reviewed_result") or event["body"].get("result") or {}
                if result.get("verified") is True:
                    db.execute(
                        "UPDATE business_tasks SET status='completed',result=?,updated=? WHERE id=? AND principal=? AND version=? AND status NOT IN ('paused','cancelled','expired')",
                        (
                            canonical(result),
                            self.clock(),
                            origin["task_id"],
                            event["principal"],
                            trigger.get("task_version"),
                        ),
                    )
            route = db.execute(
                "SELECT source_event FROM task_routes WHERE task_id=? AND principal=?",
                (origin["task_id"], event["principal"]),
            ).fetchone()
            if not route:
                return
            origin_id = route["source_event"]
            origin = db.execute(
                "SELECT * FROM harness_events WHERE id=? AND principal=?",
                (origin_id, event["principal"]),
            ).fetchone()
        if not origin or origin["kind"] not in {
            "user",
            "voice",
            "autonomy",
            "work.message",
            "storage.review",
        }:
            return
        target = (
            json.loads(origin["body"]).get("origin_conversation_id")
            if origin["kind"] in {"autonomy", "work.message", "storage.review"}
            else origin["session_key"]
        )
        # Updating a goal inside its work conversation makes that real input the
        # route origin. Preserve delivery to its recorded parent main window;
        # do not infer a new user's main from an unlinked conversation.
        target = target or origin["session_key"]
        if not target:
            return
        conversation = self.conversations.get(event["principal"], target, db=db)
        ancestors = set()
        while conversation["kind"] != "main":
            if conversation["id"] in ancestors or not conversation.get("parent_id"):
                return
            ancestors.add(conversation["id"])
            conversation = self.conversations.get(
                event["principal"], conversation["parent_id"], db=db
            )
        if conversation["kind"] != "main" or event["session_key"] == conversation["id"]:
            return
        ident = "notice-" + digest({"event": event["id"], "conversation": conversation["id"]})[:32]
        body = {
            "text": "关联事项的新结果。",
            "origin_event_id": origin_id,
            "execution_event_id": event["id"],
            "conversation_id": conversation["id"],
            "task_id": event["task_id"],
        }
        projected = {
            **reply,
            "event_id": ident,
            "execution_event_id": event["id"],
            "session_key": conversation["id"],
            "model_invoked": False,
            "source": "linked_work_result",
            "audio_generated": False,
            "simulated": origin["channel"] == "voice"
            or (
                origin["kind"] == "storage.review"
                and json.loads(origin["body"]).get("simulated", False)
            ),
        }
        self.delivery.offer(event["principal"], event["task_id"], {
            "id": ident, "target": conversation["id"], "channel": origin["channel"],
            "kind": "task.notification", "body": body, "reply": projected,
        }, db=db, attention=self.delivery.answers_user(event))
        self._audit(
            db,
            event["task_id"],
            "notification.projected",
            {
                "event_id": ident,
                "execution_event_id": event["id"],
                "origin_event_id": origin_id,
                "conversation_id": conversation["id"],
                "model_invoked": False,
            },
        )

    def recover_harness(self):
        with self.connect() as db:
            db.execute(
                "UPDATE harness_events SET status='queued',session_key=NULL WHERE status='running'"
            )
            db.execute("UPDATE harness_conversations SET status='idle' WHERE status='running'")
            db.execute("UPDATE harness_jobs SET status='queued' WHERE status='running'")
        self.expire_peer_tasks()

    def expire_peer_tasks(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                """SELECT DISTINCT b.id FROM business_tasks b JOIN peer_task_bindings p ON p.task_id=b.id
                WHERE p.expires<=? AND b.status NOT IN ('completed','cancelled','expired')""",
                (self.clock(),),
            ).fetchall()
            for row in rows:
                db.execute(
                    "UPDATE business_tasks SET status='expired',version=version+1,updated=? WHERE id=?",
                    (self.clock(), row["id"]),
                )
                db.execute(
                    "UPDATE harness_jobs SET status='superseded' WHERE task_id=? AND status IN ('queued','running')",
                    (row["id"],),
                )
                db.execute(
                    "UPDATE harness_events SET status='superseded' WHERE task_id=? AND status='queued' AND read_only=0",
                    (row["id"],),
                )
                self._audit(db, row["id"], "business.expired", {"source": "peer_deadline"})
        return len(rows)

    def close_sessions(self, principal):
        with self.connect() as db:
            db.execute("UPDATE short_sessions SET closed=1 WHERE principal=?", (principal,))

    def event_versions(self, event_id):
        with self.connect() as db:
            return {
                r["task_id"]: r["version"]
                for r in db.execute(
                    "SELECT task_id,version FROM run_task_versions WHERE event_id=?", (event_id,)
                )
            }

    def pin_versions(self, event_id, tasks):
        with self.connect() as db:
            db.executemany(
                "INSERT OR IGNORE INTO run_task_versions VALUES (?,?,?)",
                [(event_id, t["id"], t["version"]) for t in tasks],
            )
        return self.event_versions(event_id)

    def save_business_task(self, principal, args, source_event, *, _db=None):
        from .harness_authority import local_work

        event = self.event(source_event, principal, _db=_db)
        if not local_work(event) and not (event["kind"] == "peer.task" and not event["read_only"]):
            raise Conflict("this event cannot create or replace goals")
        data = args.model_dump()
        now = self.clock()
        with nullcontext(_db) if _db is not None else self.connect() as db:
            if _db is None:
                db.execute("BEGIN IMMEDIATE")
            if event["kind"] == "timer":
                parent = self._task(db, event["task_id"], principal)
                if parent["status"] in {"paused", "cancelled", "expired", "completed"}:
                    raise Conflict("scheduled parent no longer permits new work")
            if (
                event["kind"] == "timer"
                and not data.get("creation_key")
                and data["task_id"] is None
            ):
                rule = db.execute(
                    "SELECT * FROM wake_rules WHERE task_id=?", (event["task_id"],)
                ).fetchone()
                parent = self._task(db, event["task_id"], principal)
                if (
                    not rule
                    or json.loads(rule["spec"]).get("output_kind") != "video"
                    or rule["revision"] != event["body"].get("rule_revision")
                    or parent["version"] != event["body"].get("task_version")
                    or parent["kind"] != "goal"
                    or parent["status"] in {"paused", "cancelled", "expired", "completed"}
                    or event["status"] != "running"
                    or data["task_id"] is not None
                    or data["kind"] != "edit"
                    or data["requires_capture"]
                    or not data["no_motion"]
                ):
                    raise Conflict("timer may only create its authorized current video edit child")
            ident = data["task_id"]
            if ident:
                old = self._task(db, ident, principal)
                bound = db.execute(
                    "SELECT peer_request FROM task_routes WHERE task_id=?", (ident,)
                ).fetchone()
                if bound and bound["peer_request"]:
                    raise Conflict(
                        "delegated work specification belongs to its parent request; update the parent owner or use pause/cancel"
                    )
                if data["expected_version"] != old["version"]:
                    raise Conflict("stale task version")
                if old["status"] in {"cancelled", "completed", "expired"}:
                    raise Conflict("terminal task cannot be rewritten")
                version = old["version"] + 1
                status = "paused" if old["status"] == "paused" else "ready"
            else:
                creation = {"principal": principal, "event": source_event}
                if data.get("creation_key") is not None:
                    creation["key"] = data["creation_key"]
                ident = "task-" + digest(creation)[:32]
                previous = db.execute(
                    "SELECT principal FROM business_tasks WHERE id=?", (ident,)
                ).fetchone()
                if previous:
                    old = self._task(db, ident, principal)
                    old_duration = old["constraints"].get(
                        "duration_ms",
                        6000
                        if old["kind"] == "capture"
                        else 12000
                        if old["kind"] == "compose"
                        else None,
                    )
                    if (
                        old["kind"],
                        old["goal"],
                        old["constraints"]["no_motion"],
                        old_duration,
                    ) != (data["kind"], data["goal"], data["no_motion"], data["duration_ms"]):
                        raise Conflict(
                            "input already created a different task; choose another creation_key for an independent task, or task_id/expected_version to update"
                        )
                    if old["constraints"].get("requires_capture", False) != data.get(
                        "requires_capture", False
                    ):
                        raise Conflict("capture requirement changed; use versioned update")
                    if old["constraints"].get("media_recipe") != data.get("media_recipe"):
                        raise Conflict("task recipe changed; use versioned update")
                    return old
                version, status = 1, "paused" if data.get("start_paused") else "ready"
            constraints = {
                "no_motion": data["no_motion"],
                "flash_allowed": False,
                "requires_user_presence": True,
                "duration_ms": data["duration_ms"],
                "requires_capture": data.get("requires_capture", False),
                **({"media_recipe": data["media_recipe"]} if data.get("media_recipe") else {}),
            }
            if self.side == "edge" and data["kind"] == "capture" and status != "paused":
                from .contracts import Scene

                scene = Scene.model_validate_json(
                    db.execute("SELECT value FROM metadata WHERE key='scene'").fetchone()[0]
                )
                if not scene.allowed or (
                    constraints["no_motion"] and scene.viewpoint != "front-mid"
                ):
                    status = "waiting"
            remote = event["body"].get("peer_message") if event["kind"] == "peer.task" else None
            if remote:
                from datetime import datetime

                if datetime.fromisoformat(remote["expires_at"]).timestamp() <= self.clock():
                    raise Conflict("remote task expired before creation")
                stopped = db.execute(
                    """SELECT 1 FROM peer_mail WHERE direction='in'
                    AND json_extract(body,'$.kind')='control' AND json_extract(body,'$.correlation_id')=?""",
                    (remote["id"],),
                ).fetchone()
                if stopped:
                    raise Conflict("remote task was stopped before creation")
                parent = remote["payload"]["task"]
                expected_kind = remote["payload"].get("operation") or (
                    "capture" if self.side == "edge" else "compose"
                )
                if (
                    remote["payload"].get("media_recipe")
                    and data.get("media_recipe") != remote["payload"]["media_recipe"]
                ):
                    raise Conflict("delegated media recipe cannot be changed")
                if (
                    ident != "task-" + digest({"principal": principal, "event": source_event})[:32]
                    or data["kind"] != expected_kind
                ):
                    raise Conflict("remote task may only create its own scoped work item")
                if (
                    expected_kind == "compose"
                    and parent["constraints"].get("requires_capture")
                    and not constraints["requires_capture"]
                ):
                    raise Conflict("delegated composition cannot discard required capture")
                if parent["constraints"].get("no_motion") and not constraints["no_motion"]:
                    raise Conflict("remote task cannot relax parent motion constraint")
            db.execute(
                """INSERT INTO business_tasks VALUES (?,?,?,?,?,?,?,NULL,?,?)
                ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,goal=excluded.goal,version=excluded.version,
                status=excluded.status,constraints=excluded.constraints,result=NULL,updated=excluded.updated""",
                (
                    ident,
                    principal,
                    data["kind"],
                    data["goal"],
                    version,
                    status,
                    canonical(constraints),
                    now,
                    now,
                ),
            )
            db.execute(
                "UPDATE harness_jobs SET status='superseded' WHERE task_id=? AND version<>? AND status IN ('queued','running')",
                (ident, version),
            )
            db.execute(
                "INSERT OR REPLACE INTO task_routes VALUES (?,?,?,?,?)",
                (
                    ident,
                    source_event,
                    principal,
                    event["channel"],
                    canonical(remote) if remote else None,
                ),
            )
            if remote:
                from datetime import datetime

                db.execute(
                    "INSERT OR IGNORE INTO peer_task_bindings VALUES (?,?,?,?)",
                    (
                        remote["id"],
                        ident,
                        principal,
                        datetime.fromisoformat(remote["expires_at"]).timestamp(),
                    ),
                )
            db.execute("DELETE FROM task_waits WHERE task_id=?", (ident,))
            if status == "waiting" and data["kind"] == "capture":
                db.execute(
                    "INSERT INTO task_waits VALUES (?,?,?,?)",
                    (ident, version, "capture", "fresh_scene_permission"),
                )
            self.conversations.bind_task(principal, ident, source_event, db=db)
            self._history(
                db,
                f"task:{ident}:{version}",
                principal,
                ident,
                "task.saved",
                data["goal"],
                source_event,
                now,
            )
            db.execute(
                "INSERT OR REPLACE INTO run_task_versions VALUES (?,?,?)",
                (source_event, ident, version),
            )
            self._audit(
                db,
                ident,
                "business.saved",
                {
                    "source_event": source_event,
                    "version": version,
                    "constraints": constraints,
                    "creation_key": data.get("creation_key"),
                },
            )
            return self._task(db, ident, principal)

    def control_business_task(
        self, principal, ident, action, expected_version, *, source_event, _db=None
    ):
        if action not in {"pause", "resume", "cancel"}:
            raise ValueError("invalid control")
        with nullcontext(_db) if _db is not None else self.connect() as db:
            if _db is None:
                db.execute("BEGIN IMMEDIATE")
            old = self._task(db, ident, principal)
            # A repeated direct control must remain idempotent after a restart.
            receipt_id = f"control:{source_event}"
            prior = db.execute(
                "SELECT text FROM record_history WHERE id=? AND principal=?",
                (receipt_id, principal),
            ).fetchone()
            request = canonical(
                {"task_id": ident, "action": action, "expected_version": expected_version}
            )
            if prior:
                if prior[0] != request:
                    raise Conflict("control identity conflict")
                return {**old, "duplicate": True}
            if old["version"] != expected_version:
                raise Conflict("stale task version")
            if old["status"] in {"cancelled", "completed", "expired"}:
                raise Conflict("terminal task cannot be controlled")
            if action == "resume" and old["status"] != "paused":
                raise Conflict("only a paused task can resume")
            db.execute("DELETE FROM task_waits WHERE task_id=?", (ident,))
            status = {"pause": "paused", "resume": "ready", "cancel": "cancelled"}[action]
            if action == "resume" and old["kind"] == "capture":
                from .contracts import Scene

                scene = Scene.model_validate_json(
                    db.execute("SELECT value FROM metadata WHERE key='scene'").fetchone()[0]
                )
                if not scene.allowed or (
                    old["constraints"]["no_motion"] and scene.viewpoint != "front-mid"
                ):
                    status = "waiting"
                    db.execute(
                        "INSERT OR REPLACE INTO task_waits VALUES (?,?,?,?)",
                        (ident, old["version"] + 1, "capture", "fresh_scene_permission"),
                    )
            db.execute(
                "UPDATE business_tasks SET version=version+1,status=?,updated=? WHERE id=?",
                (status, self.clock(), ident),
            )
            db.execute(
                "UPDATE harness_jobs SET status='superseded' WHERE task_id=? AND status IN ('queued','running')",
                (ident,),
            )
            if action == "cancel":
                db.execute("UPDATE wake_rules SET enabled=0 WHERE task_id=?", (ident,))
            self._history(
                db, receipt_id, principal, ident, "control", request, source_event, self.clock()
            )
            self._audit(
                db,
                ident,
                "business.control",
                {"action": action, "source_event": source_event, "version": old["version"] + 1},
            )
            return self._task(db, ident, principal)

    def remember(
        self,
        principal,
        key,
        text,
        source_event,
        *,
        memory_id=None,
        expected_version=None,
        supersedes=(),
        _db=None,
    ):
        event = self.event(source_event, principal, _db=_db)
        from .harness_authority import local_work

        if not local_work(event) and not (
            event["kind"] == "peer.record_mutation" and not event["read_only"]
        ):
            raise Conflict("memory writes require a user interaction")
        with nullcontext(_db) if _db is not None else self.connect() as db:
            if _db is None:
                db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM user_memory WHERE principal=? AND key=?", (principal, key)
            ).fetchone()
            if memory_id is not None:
                if old is None or old["id"] != memory_id:
                    raise Conflict("memory identity/key mismatch")
                if old["version"] != expected_version:
                    raise Conflict("stale memory version")
            elif old and old["text"] != text:
                raise Conflict(
                    "read existing memory and provide its identity/version before changing it"
                )
            if (
                old
                and db.execute(
                    "SELECT 1 FROM memory_retirements WHERE memory_id=?", (old["id"],)
                ).fetchone()
            ):
                raise Conflict("memory was superseded; update the current record")
            if old and old["text"] == text and not supersedes:
                return dict(old)
            ident = old["id"] if old else "memory-" + uuid.uuid4().hex
            version = old["version"] + 1 if old else 1
            now = self.clock()
            db.execute(
                """INSERT INTO user_memory VALUES (?,?,?,?,?,?,?) ON CONFLICT(principal,key)
                DO UPDATE SET text=excluded.text,version=excluded.version,source=excluded.source,updated=excluded.updated""",
                (ident, principal, key, text, version, source_event, now),
            )
            for retired in set(supersedes):
                if retired == ident:
                    raise Conflict("memory cannot supersede itself")
                candidate = db.execute(
                    "SELECT id FROM user_memory WHERE id=? AND principal=?", (retired, principal)
                ).fetchone()
                if not candidate:
                    raise Conflict("superseded memory is outside current principal")
                db.execute(
                    "INSERT OR REPLACE INTO memory_retirements VALUES (?,?,?,?)",
                    (retired, ident, source_event, now),
                )
            self._history(
                db,
                f"{ident}:{version}",
                principal,
                None,
                "memory.changed",
                key + ": " + text,
                source_event,
                now,
            )
            return dict(db.execute("SELECT * FROM user_memory WHERE id=?", (ident,)).fetchone())

    def search(
        self,
        principal,
        query="",
        collection="all",
        limit=5,
        *,
        exclude_source=None,
        include_generated=False,
        match_mode="any",
    ):
        if collection not in {"all", "tasks", "history", "memory"} or not 1 <= limit <= 10:
            raise ValueError("invalid search scope")
        if match_mode not in {"any", "all"}:
            raise ValueError("invalid search match mode")
        # Whitespace separates keywords. Treating "拍摄任务 暂停" as one
        # literal substring misses a record saying "拍摄任务暂停是因为…".
        terms = query.split()[:8] or [""]
        patterns = [
            "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for term in terms
        ]
        records = []
        with self.connect() as db:
            tables = [
                (
                    "tasks",
                    "business_tasks",
                    "(id || ' ' || kind || ' ' || goal || ' ' || status)",
                    "updated",
                ),
                ("memory", "user_memory", "(key || ' ' || text)", "updated"),
                ("history", "record_history", "text", "created"),
            ]
            for name, table, column, order in tables:
                if collection not in {"all", name}:
                    continue
                where = (" AND " if match_mode == "all" else " OR ").join(
                    f"{column} LIKE ? ESCAPE '\\'" for _ in patterns
                )
                exclusion = " AND source<>?" if name == "history" and exclude_source else ""
                generated_filter = (
                    " AND kind NOT IN ('reply','report','report.notification','task.notification')"
                    if name == "history" and not include_generated
                    else ""
                )
                if name == "memory":
                    generated_filter = " AND NOT EXISTS (SELECT 1 FROM memory_retirements r WHERE r.memory_id=user_memory.id)"
                ordering = "CASE WHEN kind='reply' THEN 1 ELSE 0 END," if name == "history" else ""
                rank = "+".join(
                    f"CASE WHEN {column} LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END" for _ in patterns
                )
                params = [
                    *patterns,
                    principal,
                    *patterns,
                    *([exclude_source] if exclusion else []),
                    limit,
                ]
                rows = db.execute(
                    f"SELECT *,({rank}) AS _rank,{column} AS _search_text FROM {table} WHERE principal=? AND ({where}){exclusion}{generated_filter} ORDER BY _rank DESC,{ordering}{order} DESC LIMIT ?",
                    params,
                ).fetchall()
                for row in rows:
                    item = self._task(db, row["id"], principal) if name == "tasks" else dict(row)
                    for key in ("constraints", "result"):
                        if key in item and isinstance(item[key], str):
                            item[key] = json.loads(item[key])
                    item["authority"] = (
                        "current_business_state"
                        if name == "tasks"
                        else "user_memory_not_execution_evidence"
                        if name == "memory"
                        else "generated_reply_not_execution_evidence"
                        if item.get("kind")
                        in {"reply", "report", "report.notification", "task.notification"}
                        else "historical_record_not_current_state"
                    )
                    item.pop("_rank", None)
                    item.pop("_search_text", None)
                    matched = [
                        t for t in terms if t and t.casefold() in row["_search_text"].casefold()
                    ]
                    records.append(
                        {
                            "collection": name,
                            **item,
                            "matched_terms": matched,
                            "all_terms_matched": len(matched) == len([t for t in terms if t]),
                            "_rank": row["_rank"],
                            "_time": row[order],
                        }
                    )
        records.sort(
            key=lambda r: (
                -r["_rank"],
                -{"tasks": 3, "memory": 2, "history": 1}[r["collection"]],
                -r["_time"],
            )
        )
        records = records[:limit]
        for r in records:
            r.pop("_rank")
            r.pop("_time")
        return {
            "records": records,
            "query": query,
            "known": bool(records),
            "retrieved_at": self.clock(),
            "source_side": self.side,
            "match": match_mode + "_keywords_ranked",
            "terms": terms,
        }

    def start_job(self, principal, ident, expected_version, operation=None, plan=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._task(db, ident, principal)
            if task["version"] != expected_version or task["status"] in {
                "paused",
                "cancelled",
                "expired",
            }:
                raise Conflict("task no longer permits a new job")
            if db.execute(
                "SELECT 1 FROM peer_task_bindings WHERE task_id=? AND expires<=?",
                (ident, self.clock()),
            ).fetchone():
                raise Conflict("delegated task expired")
            operation = operation or task["kind"]
            capture_step = (
                operation == "capture"
                and task["kind"] == "compose"
                and task["constraints"].get("requires_capture")
            )
            if (
                operation not in {"capture", "compose"}
                or (
                    task["kind"] not in {"goal", "edit"}
                    and operation != task["kind"]
                    and not capture_step
                )
                or (task["kind"] == "edit" and operation != "compose")
            ):
                raise Conflict("task does not authorize this operation")
            old = db.execute(
                "SELECT * FROM harness_jobs WHERE task_id=? AND version=? AND kind=?",
                (ident, expected_version, operation),
            ).fetchone()
            if old:
                saved = db.execute(
                    "SELECT plan FROM harness_job_plans WHERE job_id=?", (old["id"],)
                ).fetchone()
                if plan is not None and (not saved or saved["plan"] != canonical(plan)):
                    raise Conflict(
                        "job inputs already frozen; update task version before changing materialization"
                    )
                job_id = old["id"]
            else:
                if task["status"] == "completed":
                    raise Conflict("completed task cannot start a new operation")
                if db.execute(
                    "SELECT 1 FROM harness_jobs WHERE task_id=? AND version=? AND status IN ('queued','running')",
                    (ident, expected_version),
                ).fetchone():
                    raise Conflict("another operation is still running; await its receipt")
                job_id = f"job-{ident}-{expected_version}-{operation}"
                db.execute(
                    "INSERT INTO harness_jobs VALUES (?,?,?,?,'queued',NULL,?)",
                    (job_id, ident, expected_version, operation, self.clock()),
                )
                if plan is not None:
                    db.execute(
                        "INSERT INTO harness_job_plans VALUES (?,?)", (job_id, canonical(plan))
                    )
                old = db.execute("SELECT * FROM harness_jobs WHERE id=?", (job_id,)).fetchone()
            if old["status"] == "waiting":
                db.execute(
                    "UPDATE harness_jobs SET status='queued',result=NULL WHERE id=?", (job_id,)
                )
                old = db.execute("SELECT * FROM harness_jobs WHERE id=?", (job_id,)).fetchone()
            if old["status"] == "failed":
                raise Conflict("failed job requires an explicit updated task")
            if old["status"] == "completed":
                return dict(old)
            db.execute("DELETE FROM task_waits WHERE task_id=?", (ident,))
            db.execute(
                "UPDATE business_tasks SET status='running',updated=? WHERE id=?",
                (self.clock(), ident),
            )
            return dict(old)

    def defer_capture_job(self, job):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._task(db, job["task_id"], job["principal"])
            if task["version"] != job["version"] or task["status"] in {
                "paused",
                "cancelled",
                "expired",
                "completed",
            }:
                db.execute("UPDATE harness_jobs SET status='superseded' WHERE id=?", (job["id"],))
                return False
            db.execute(
                "UPDATE harness_jobs SET status='waiting',result=? WHERE id=?",
                (
                    canonical(
                        {"status": "waiting", "reason": "fresh_scene_permission", "simulated": True}
                    ),
                    job["id"],
                ),
            )
            db.execute(
                "UPDATE business_tasks SET status='waiting',updated=? WHERE id=?",
                (self.clock(), task["id"]),
            )
            db.execute(
                "INSERT OR REPLACE INTO task_waits VALUES (?,?,?,?)",
                (task["id"], task["version"], "capture", "fresh_scene_permission"),
            )
            self._audit(
                db,
                task["id"],
                "job.deferred",
                {
                    "job_id": job["id"],
                    "reason": "fresh_scene_permission",
                    "task_version": task["version"],
                },
            )
            return True

    def guard_job(self, job):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM business_tasks WHERE id=?", (job["task_id"],)
            ).fetchone()
            if (
                row is None
                or row["version"] != job["version"]
                or row["status"] in {"paused", "cancelled", "completed", "expired"}
            ):
                raise Conflict("job was superseded or stopped")
            if db.execute(
                "SELECT 1 FROM peer_task_bindings WHERE task_id=? AND expires<=?",
                (job["task_id"], self.clock()),
            ).fetchone():
                raise Conflict("delegated task expired")
            return self._task(db, row["id"], row["principal"])

    def claim_job(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT * FROM harness_jobs WHERE status='queued' ORDER BY created"
            ).fetchall()
            for row in rows:
                task = db.execute(
                    "SELECT * FROM business_tasks WHERE id=?", (row["task_id"],)
                ).fetchone()
                if (
                    task is None
                    or task["version"] != row["version"]
                    or task["status"] in {"paused", "cancelled", "completed", "expired"}
                ):
                    db.execute(
                        "UPDATE harness_jobs SET status='superseded' WHERE id=?", (row["id"],)
                    )
                    continue
                db.execute("UPDATE harness_jobs SET status='running' WHERE id=?", (row["id"],))
                return {**dict(row), "principal": task["principal"]}
        return None

    def finish_job(self, job, result, *, error=None, _db=None):
        with nullcontext(_db) if _db is not None else self.connect() as db:
            if _db is None:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM business_tasks WHERE id=?", (job["task_id"],)
            ).fetchone()
            old = db.execute("SELECT * FROM harness_jobs WHERE id=?", (job["id"],)).fetchone()
            if old is None:
                raise KeyError(job["id"])
            if old["status"] == "completed":
                if error or old["result"] != canonical(result):
                    raise Conflict("committed job result changed")
                return "completed"
            stale = (
                row is None
                or row["version"] != job["version"]
                or row["status"] in {"paused", "cancelled", "completed", "expired"}
            )
            status = "superseded" if stale else "failed" if error else "completed"
            db.execute(
                "UPDATE harness_jobs SET status=?,result=? WHERE id=?",
                (status, canonical(result), job["id"]),
            )
            if not stale:
                review_needed = bool(result.get("review_required") and job["kind"] == "compose")
                continuing = row["kind"] == "goal" or (
                    row["kind"] == "compose" and job["kind"] == "capture"
                )
                final_status = "ready" if review_needed or (continuing and not error) else status
                db.execute(
                    "UPDATE business_tasks SET status=?,result=?,updated=? WHERE id=?",
                    (final_status, canonical(result), self.clock(), job["task_id"]),
                )
                route = db.execute(
                    "SELECT * FROM task_routes WHERE task_id=?", (row["id"],)
                ).fetchone()
                self.submit(
                    "result-" + job["id"],
                    row["principal"],
                    route["channel"] if route else "system",
                    "media.review" if review_needed else "job.result",
                    {
                        "text": "制作作业已落库；先读取当前任务，成功时复看成片并review_rendered_media批准，失败或不满意时有限修正。未批准前不得称为已交付。"
                        if review_needed
                        else "工具作业已落库，请读取最新任务并通知原请求者。",
                        "result": result,
                        "job_id": job["id"],
                        "task_version": job["version"],
                        "peer_message": json.loads(route["peer_request"])
                        if route and route["peer_request"]
                        else None,
                        "origin_event_id": route["source_event"] if route else None,
                    },
                    task_id=row["id"],
                    read_only=not review_needed,
                    _db=db,
                )
                if review_needed:
                    db.execute(
                        "INSERT OR REPLACE INTO run_task_versions VALUES (?,?,?)",
                        ("result-" + job["id"], job["task_id"], job["version"]),
                    )
                if row["kind"] in {"goal", "compose"} and job["kind"] == "capture" and not error:
                    self.submit(
                        "step-" + job["id"],
                        row["principal"],
                        "system",
                        "task.step_completed",
                        {
                            "text": "本事项采集步骤已完成。读取本任务和核验素材，继续剩余已授权步骤或等待原规则。",
                            "job_id": job["id"],
                            "task_version": job["version"],
                            "origin_event_id": route["source_event"] if route else None,
                        },
                        task_id=row["id"],
                        _db=db,
                    )
            self._audit(
                db,
                job["task_id"],
                "job.finished",
                {"job_id": job["id"], "status": status, "result": result},
            )
            return status

    def set_timer(self, principal, ident, interval, enabled):
        from .harness_contracts import RuleArgs

        existing = next((r for r in self.scheduler.list(principal) if r["task_id"] == ident), None)
        result = self.scheduler.set(
            principal,
            RuleArgs(
                task_id=ident,
                schedule="interval",
                interval_seconds=interval,
                enabled=enabled,
                expected_revision=existing["revision"] if existing else None,
                delivery_side=self.side,
            ),
            source_event="legacy-set_timer",
        )
        return {**result, "interval_seconds": interval}

    def due_timers(self, *, now=None):
        return self.scheduler.due(now=now)
