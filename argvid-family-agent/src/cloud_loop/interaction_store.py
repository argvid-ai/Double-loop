"""Edge-owned conversations and versioned intent, sharing the existing edge DB."""

from __future__ import annotations

import json
import time
import uuid
from contextlib import nullcontext
from pathlib import Path

from .contracts import (
    ID,
    Constraints,
    ControlArguments,
    IntentArguments,
    Scene,
    UserMessage,
    canonical,
)
from .dual_store import DualStore
from .store import Conflict


class ConversationStore(DualStore):
    def __init__(self, root: Path):
        super().__init__(root, "edge")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY, revision INTEGER NOT NULL, intent TEXT NOT NULL,
                    summary TEXT, summary_cursor INTEGER NOT NULL DEFAULT 0,
                    created REAL NOT NULL, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_inputs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    revision INTEGER NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
                    status TEXT NOT NULL, created REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversation_delegations (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    intent_version INTEGER NOT NULL, body TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, created REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                    cancel_sent INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS conversation_effects (
                    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id),
                    intent_version INTEGER NOT NULL, accepted_revision INTEGER NOT NULL,
                    task_id TEXT NOT NULL, arguments TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT, created REAL NOT NULL
                );
            """)

    def create_conversation(self, conversation_id: str | None = None):
        conversation_id = conversation_id or uuid.uuid4().hex
        import re

        if not re.fullmatch(ID, conversation_id):
            raise ValueError("invalid conversation identity")
        now = time.time()
        initial = {
            "version": 0,
            "kind": None,
            "goal": None,
            "subject_id": "pet-cat-mimi",
            "duration_ms": None,
            "orientation": "portrait",
            "flash_allowed": False,
            "requires_user_presence": True,
            "paused": False,
            "status": "idle",
            "capture_task": None,
            "cloud_job": None,
            "status_reason": None,
            "pending_question": None,
        }
        with self.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO conversations VALUES (?,?,?,NULL,0,?,?)",
                (conversation_id, 0, canonical(initial), now, now),
            )
        return self.conversation(conversation_id)

    def conversation(self, cid):
        with self.connect() as db:
            row = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            if row is None:
                raise KeyError(cid)
            value = dict(row)
            value["intent"] = json.loads(value["intent"])
            value["summary"] = json.loads(value["summary"]) if value["summary"] else None
            return value

    @staticmethod
    def _current(db, cid, revision=None):
        row = db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if row is None:
            raise KeyError(cid)
        if revision is not None and row["revision"] != revision:
            raise Conflict("CONVERSATION_SUPERSEDED: a newer user input exists")
        return row, json.loads(row["intent"])

    def assert_turn(self, cid, revision):
        with self.connect() as db:
            row, intent = self._current(db, cid, revision)
            return intent

    def submit_message(self, cid, message: UserMessage, *, _db=None):
        body = canonical(message)
        with nullcontext(_db) if _db is not None else self.connect() as db:
            if _db is None:
                db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM conversation_inputs WHERE id=?", (message.event_id,)
            ).fetchone()
            if old:
                if old["conversation_id"] != cid or old["kind"] != "user" or old["body"] != body:
                    raise Conflict("input event identity conflict")
                return {
                    "duplicate": True,
                    "event_id": message.event_id,
                    "revision": old["revision"],
                }
            row, _ = self._current(db, cid)
            revision = row["revision"] + 1
            db.execute(
                "UPDATE conversations SET revision=?,updated=? WHERE id=?",
                (revision, time.time(), cid),
            )
            db.execute(
                "INSERT INTO conversation_inputs(id,conversation_id,revision,kind,body,status,created) VALUES (?,?,?,?,?,?,?)",
                (message.event_id, cid, revision, "user", body, "queued", time.time()),
            )
            self._audit(
                db,
                cid,
                "conversation.input",
                {"event_id": message.event_id, "revision": revision, "text": message.text},
            )
        return {"duplicate": False, "event_id": message.event_id, "revision": revision}

    def set_intent(self, cid, revision, args: IntentArguments):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _, current = self._current(db, cid, revision)
            comparable = args.model_dump(exclude={"new_attempt"})
            identity = {k: v for k, v in comparable.items() if k != "goal"}
            # Repeating an unchanged goal is normally idempotent. An explicit
            # user retry of a failed cloud job must be able to create a fresh
            # delegation without requiring new_attempt (which requests a new
            # capture). Never rearm a capture failure via this cloud-only path.
            retry_cloud = False
            if (
                not args.new_attempt
                and current["kind"] == "compose"
                and current["cloud_job"]
                and current["capture_task"]
                and all(current.get(k) == v for k, v in identity.items())
            ):
                failed = db.execute(
                    "SELECT status FROM conversation_delegations WHERE id=?",
                    (current["cloud_job"],),
                ).fetchone()
                receipt = db.execute(
                    "SELECT result FROM actions WHERE id=?", ("capture:" + current["capture_task"],)
                ).fetchone()
                if failed and failed["status"] == "failed" and receipt:
                    package = json.loads(receipt["result"])
                    retry_cloud = (
                        package["subject_id"] == args.subject_id
                        and package["asset"]["duration_ms"] == 6000
                    )
            if (
                not args.new_attempt
                and not retry_cloud
                and current["status"] != "cancelled"
                and all(current.get(k) == v for k, v in identity.items())
            ):
                if current.get("pending_question") or current["goal"] != args.goal:
                    current["pending_question"] = None
                    current["goal"] = args.goal
                    db.execute(
                        "UPDATE conversations SET intent=? WHERE id=?", (canonical(current), cid)
                    )
                    self._audit(
                        db,
                        cid,
                        "intent.description.updated",
                        {
                            "intent_version": current["version"],
                            "goal": args.goal,
                            "effect_identity_unchanged": True,
                        },
                    )
                return current
            # Invalidate old effects in the SAME transaction as changing intent.
            reuse = None
            if not args.new_attempt and current["capture_task"]:
                receipt = db.execute(
                    "SELECT result FROM actions WHERE id=?", ("capture:" + current["capture_task"],)
                ).fetchone()
                if receipt:
                    package = json.loads(receipt["result"])
                    if (
                        package["subject_id"] == args.subject_id
                        and package["asset"]["duration_ms"] == 6000
                    ):
                        reuse = current["capture_task"]
            for row in db.execute(
                "SELECT id,context,status FROM tasks WHERE json_extract(context,'$.conversation_id')=?",
                (cid,),
            ).fetchall():
                if row["status"] not in {"completed", "cancelled", "unsupported", "expired"}:
                    db.execute(
                        "UPDATE tasks SET status='cancelled',outcome=NULL WHERE id=?", (row["id"],)
                    )
            db.execute(
                "UPDATE conversation_delegations SET status='superseded' WHERE conversation_id=? AND status IN ('pending','accepted','running')",
                (cid,),
            )
            scene = Scene.model_validate_json(
                db.execute("SELECT value FROM metadata WHERE key='scene'").fetchone()[0]
            )
            waiting = current["paused"] or not scene.allowed or scene.subject_id != args.subject_id
            value = {
                **current,
                **comparable,
                "version": current["version"] + 1,
                "status": "waiting"
                if current["paused"]
                else "asset_ready"
                if reuse
                else "waiting"
                if waiting
                else "ready",
                "capture_task": reuse,
                "cloud_job": None,
                "status_reason": None,
                "pending_question": None,
            }
            db.execute(
                "UPDATE conversations SET intent=?,updated=? WHERE id=?",
                (canonical(value), time.time(), cid),
            )
            self._audit(db, cid, "intent.changed", {"revision": revision, "intent": value})
            if retry_cloud:
                self._audit(
                    db,
                    cid,
                    "cloud.retry.prepared",
                    {
                        "previous_job_id": current["cloud_job"],
                        "intent_version": value["version"],
                        "capture_task": reuse,
                        "revision": revision,
                    },
                )
            if reuse:
                self._audit(
                    db,
                    cid,
                    "capture.reference.reused",
                    {
                        "source_task_id": reuse,
                        "intent_version": value["version"],
                        "reason": "already committed same-subject six-second capture; no new_attempt requested; bytes rechecked before use",
                    },
                )
        return value

    def control(self, cid, action, *, revision=None, event_id=None, source_text=None, _db=None):
        ControlArguments(action=action)
        with nullcontext(_db) if _db is not None else self.connect() as db:
            if _db is None:
                db.execute("BEGIN IMMEDIATE")
            if event_id:
                old = db.execute(
                    "SELECT * FROM conversation_inputs WHERE id=?", (event_id,)
                ).fetchone()
                body = canonical(
                    {"action": action, **({"text": source_text} if source_text is not None else {})}
                )
                if old:
                    if (
                        old["conversation_id"] != cid
                        or old["kind"] != "control"
                        or old["body"] != body
                    ):
                        raise Conflict("control event identity conflict")
                    return {"duplicate": True, "action": action, "revision": old["revision"]}
            row, intent = self._current(db, cid, revision)
            pending = (
                db.execute(
                    """SELECT 1 FROM conversation_inputs WHERE conversation_id=? AND kind='user'
                AND status IN ('queued','running','coalesced','interrupted') LIMIT 1""",
                    (cid,),
                ).fetchone()
                is not None
            )
            pending_only = intent["kind"] is None or intent["status"] in {"completed", "cancelled"}
            if (
                intent["kind"] is None
                and not pending
                and not intent["paused"]
                and not (action == "cancel" and intent["status"] == "cancelled")
            ):
                raise Conflict("no active intent or pending user request to control")
            if (intent["status"] == "completed" and not pending and not intent["paused"]) or (
                intent["status"] == "cancelled" and action != "cancel" and not pending
            ):
                raise Conflict(
                    "terminal intent cannot be resumed or paused; explicitly create a new request"
                )
            new_revision = row["revision"] + (1 if event_id else 0)
            if action == "cancel" and intent["status"] != "cancelled":
                if intent["status"] == "completed":
                    intent["paused"] = False
                else:
                    intent.update(status="cancelled", paused=False, version=intent["version"] + 1)
            elif action == "pause":
                intent.update(
                    paused=True,
                    status=intent["status"]
                    if intent["status"] in {"completed", "cancelled"}
                    else "waiting",
                )
            elif action == "resume":
                intent.update(
                    paused=False,
                    status=intent["status"]
                    if intent["status"] in {"completed", "cancelled"}
                    else "ready"
                    if intent["kind"]
                    else "idle",
                )
            db.execute(
                "UPDATE conversations SET revision=?,intent=?,updated=? WHERE id=?",
                (new_revision, canonical(intent), time.time(), cid),
            )
            for task in db.execute(
                "SELECT id,context,status FROM tasks WHERE json_extract(context,'$.conversation_id')=?",
                (cid,),
            ).fetchall():
                if pending_only:
                    continue
                if task["status"] in {"completed", "cancelled", "unsupported", "expired"}:
                    continue
                context = json.loads(task["context"])
                context["paused"] = action == "pause"
                status = (
                    "cancelled"
                    if action == "cancel"
                    else "waiting"
                    if action == "pause"
                    else "running"
                )
                db.execute(
                    "UPDATE tasks SET context=?,status=?,outcome=NULL WHERE id=?",
                    (canonical(context), status, task["id"]),
                )
                db.execute(
                    "UPDATE events SET status='superseded' WHERE task_id=? AND status='queued'",
                    (task["id"],),
                )
            if action == "cancel":
                if not pending_only:
                    db.execute(
                        "UPDATE conversation_delegations SET status='superseded' WHERE conversation_id=? AND status!='failed'",
                        (cid,),
                    )
                db.execute(
                    """UPDATE conversation_inputs SET status='superseded' WHERE conversation_id=?
                    AND status IN ('queued','running','coalesced','interrupted')
                    AND (revision<? OR kind!='user')""",
                    (cid, new_revision),
                )
            if event_id:
                db.execute(
                    "INSERT INTO conversation_inputs(id,conversation_id,revision,kind,body,status,created) VALUES (?,?,?,?,?,'done',?)",
                    (event_id, cid, new_revision, "control", body, time.time()),
                )
            self._audit(
                db,
                cid,
                "intent.control",
                {
                    "action": action,
                    "revision": new_revision,
                    "intent_version": intent["version"],
                    "event_id": event_id,
                    "source_text": source_text,
                    "model_invoked": event_id is None,
                    "scope": "pending_inputs" if pending_only else "intent_and_pending_inputs",
                },
            )
        return {
            "duplicate": False,
            "action": action,
            "revision": new_revision,
            "intent": intent,
            "scope": "pending_inputs" if pending_only else "intent_and_pending_inputs",
        }

    def capture_task(self, cid, revision, profile_digest, deadline_seconds=600):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _, intent = self._current(db, cid, revision)
            if (
                intent["kind"] not in {"capture", "compose"}
                or intent["paused"]
                or intent["status"] in {"cancelled", "unsupported"}
            ):
                raise Conflict("no active capture authorization")
            if intent["capture_task"]:
                return intent["capture_task"]
            task_id = uuid.uuid4().hex
            constraints = Constraints(subject_id=intent["subject_id"]).model_dump(mode="json")
            context = {
                "owner": "conversation",
                "conversation_id": cid,
                "intent_version": intent["version"],
                "expires": time.time() + deadline_seconds,
                "paused": False,
                "profile_digest": profile_digest,
                "invocation": {"arguments": {"constraints": constraints}},
                "simulated": True,
            }
            # Direct local tool execution, not a second NanoBot loop or a fake cloud invocation.
            db.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                (task_id, intent["goal"], canonical(context), "running", None, None, time.time()),
            )
            intent["capture_task"] = task_id
            db.execute("UPDATE conversations SET intent=? WHERE id=?", (canonical(intent), cid))
            self._audit(
                db,
                cid,
                "capture.task.created",
                {
                    "task_id": task_id,
                    "intent_version": intent["version"],
                    "origin": "edge_conversation",
                },
            )
        return task_id

    def set_status(self, cid, revision, status, summary, pending_question=None):
        if status not in {"waiting", "unsupported", "completed"}:
            raise ValueError("invalid outcome")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _, intent = self._current(db, cid, revision)
            if intent["status"] == "cancelled":
                raise Conflict("cancelled intent cannot be completed or resumed")
            if intent["status"] == "completed" and status != "completed":
                raise Conflict("completed intent cannot regress")
            if status == "completed":
                if intent["paused"]:
                    raise Conflict("paused intent cannot be published completed")
                if intent["kind"] == "capture":
                    task = intent["capture_task"]
                    if (
                        not task
                        or not db.execute(
                            "SELECT 1 FROM actions WHERE id=?", (f"capture:{task}",)
                        ).fetchone()
                    ):
                        raise Conflict("verified capture receipt required")
                elif intent["kind"] == "compose":
                    job = db.execute(
                        "SELECT * FROM conversation_delegations WHERE id=?", (intent["cloud_job"],)
                    ).fetchone()
                    if (
                        not job
                        or job["status"] != "completed"
                        or job["intent_version"] != intent["version"]
                    ):
                        raise Conflict("current verified cloud result required")
                else:
                    raise Conflict("no active task to complete")
            intent["status"] = status
            intent["status_reason"] = summary
            intent["pending_question"] = pending_question if status == "waiting" else None
            db.execute(
                "UPDATE conversations SET intent=?,updated=? WHERE id=?",
                (canonical(intent), time.time(), cid),
            )
            self._audit(
                db,
                cid,
                "intent.outcome",
                {"status": status, "summary": summary, "intent_version": intent["version"]},
            )
        return intent

    def save_summary(self, cid, revision, cursor, text, *, intent_version, provenance=None):
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("invalid bounded summary")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row, intent = self._current(db, cid, revision)
            if intent["version"] != intent_version:
                raise Conflict("intent changed during summarization")
            if cursor <= row["summary_cursor"]:
                raise Conflict("stale summary cursor")
            if not db.execute(
                "SELECT 1 FROM conversation_inputs WHERE conversation_id=? AND seq=?", (cid, cursor)
            ).fetchone():
                raise Conflict("summary references unseen or foreign input")
            db.execute(
                "UPDATE conversations SET summary=?,summary_cursor=? WHERE id=?",
                (
                    canonical(
                        {
                            "text": text,
                            "covered_input_seq": cursor,
                            "intent_version": intent_version,
                            "based_on_revision": revision,
                            "derived": True,
                            "provenance": provenance or {},
                        }
                    ),
                    cursor,
                    cid,
                ),
            )
            self._audit(
                db,
                cid,
                "context.summary.saved",
                {"covered_input_seq": cursor, "revision": revision},
            )

    def conversations(self):
        with self.connect() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM conversations ORDER BY updated DESC")]
        return [self.conversation(cid) for cid in ids]

    def inputs(self, cid):
        self.conversation(cid)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM conversation_inputs WHERE conversation_id=? ORDER BY seq", (cid,)
            ).fetchall()
        return [{**dict(r), "body": json.loads(r["body"])} for r in rows]

    def enqueue_event(self, cid, event_id, kind, data, *, intent_version, _db=None):
        if kind not in {
            "scene.changed",
            "capture.completed",
            "capture.failed",
            "cloud.completed",
            "cloud.failed",
            "restart",
        }:
            raise ValueError("unsupported conversation event")
        with nullcontext(_db) if _db is not None else self.connect() as db:
            if _db is None:
                db.execute("BEGIN IMMEDIATE")
            row, intent = self._current(db, cid)
            resumes_pending = kind == "restart" and bool(data.get("pending_user_ids"))
            if (
                intent["version"] != intent_version
                or (intent["status"] in {"completed", "cancelled"} and not resumes_pending)
                or intent["paused"]
            ):
                self._audit(
                    db, cid, "conversation.event.superseded", {"event_id": event_id, "kind": kind}
                )
                return False
            body = canonical({**data, "_intent_version": intent_version})
            old = db.execute(
                "SELECT conversation_id,kind,body FROM conversation_inputs WHERE id=?", (event_id,)
            ).fetchone()
            if old:
                old_data = json.loads(old["body"])
                if (
                    old["conversation_id"] != cid
                    or old["kind"] != kind
                    or {k: v for k, v in old_data.items() if k != "_intent_version"} != data
                ):
                    raise Conflict("conversation event identity conflict")
                if old_data.get("_intent_version", intent_version) != intent_version:
                    raise Conflict("event intent binding conflict")
                return False
            if kind == "scene.changed":
                db.execute(
                    "UPDATE conversation_inputs SET status='superseded' WHERE conversation_id=? AND kind='scene.changed' AND status='queued'",
                    (cid,),
                )
            db.execute(
                "INSERT INTO conversation_inputs(id,conversation_id,revision,kind,body,status,created) VALUES (?,?,?,?,?,'queued',?)",
                (event_id, cid, row["revision"], kind, body, time.time()),
            )
        return True

    def claim_input(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""UPDATE conversation_inputs SET status=CASE WHEN kind='user' THEN 'coalesced' ELSE 'superseded' END
                          WHERE status='queued' AND revision<(SELECT revision FROM conversations WHERE id=conversation_id)""")
            db.execute("""UPDATE conversation_inputs SET status='superseded' WHERE status='queued' AND kind NOT IN ('user','control')
                AND json_extract(body,'$._intent_version') IS NOT NULL
                AND json_extract(body,'$._intent_version') != (SELECT json_extract(intent,'$.version') FROM conversations WHERE id=conversation_id)""")
            row = db.execute(
                "SELECT * FROM conversation_inputs WHERE status='queued' ORDER BY CASE WHEN kind='user' THEN 0 ELSE 1 END,seq LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            db.execute("UPDATE conversation_inputs SET status='running' WHERE seq=?", (row["seq"],))
        return {**dict(row), "body": json.loads(row["body"])}

    def finish_input(self, event, status, result):
        if status not in {"done", "failed", "interrupted"}:
            raise ValueError("invalid input outcome")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row, intent = self._current(db, event["conversation_id"])
            if row["revision"] != event["revision"]:
                status = "interrupted"
            existing = db.execute(
                "SELECT status FROM conversation_inputs WHERE seq=?", (event["seq"],)
            ).fetchone()
            if existing and existing["status"] == "superseded":
                status = "superseded"
            db.execute(
                "UPDATE conversation_inputs SET status=? WHERE seq=?", (status, event["seq"])
            )
            if status == "done" and not intent["paused"]:
                reserved = set()
                for later in db.execute(
                    "SELECT body FROM conversation_inputs WHERE conversation_id=? AND kind='restart' AND status='queued' AND seq>?",
                    (event["conversation_id"], event["seq"]),
                ):
                    reserved.update(json.loads(later["body"]).get("pending_user_ids", []))
                for pending in db.execute(
                    "SELECT id FROM conversation_inputs WHERE conversation_id=? AND kind='user' AND seq<? AND status IN ('coalesced','interrupted')",
                    (event["conversation_id"], event["seq"]),
                ).fetchall():
                    if pending["id"] not in reserved:
                        db.execute(
                            "UPDATE conversation_inputs SET status='included' WHERE id=?",
                            (pending["id"],),
                        )
                if event["kind"] == "user":
                    self._resume_continuations(db, event["conversation_id"], row, intent)
            self._audit(
                db,
                event["conversation_id"],
                "conversation.turn.finished",
                {
                    "event_id": event["id"],
                    "input_seq": event["seq"],
                    "revision": event["revision"],
                    "status": status,
                    **result,
                },
            )
        return status

    def _resume_continuations(self, db, cid, conversation, intent):
        if (
            intent["status"] in {"completed", "cancelled", "unsupported"}
            or intent["paused"]
            or intent.get("pending_question")
        ):
            return
        candidates = db.execute(
            """SELECT * FROM conversation_inputs WHERE conversation_id=?
            AND kind IN ('scene.changed','capture.completed','cloud.completed') AND status IN ('interrupted','superseded') ORDER BY seq""",
            (cid,),
        ).fetchall()
        selected = set()
        for event in reversed(candidates):
            body = json.loads(event["body"])
            kind = event["kind"]
            if body.get("_intent_version") != intent["version"] or kind in selected:
                continue
            needed = False
            if kind == "scene.changed":
                scene = Scene.model_validate_json(
                    db.execute("SELECT value FROM metadata WHERE key='scene'").fetchone()[0]
                )
                needed = bool(
                    intent["kind"]
                    and intent["status"] in {"ready", "waiting"}
                    and not intent["capture_task"]
                    and scene.allowed
                    and scene.subject_id == intent["subject_id"]
                )
            elif kind == "capture.completed" and intent["capture_task"]:
                receipt = db.execute(
                    "SELECT result FROM actions WHERE id=?", ("capture:" + intent["capture_task"],)
                ).fetchone()
                needed = bool(
                    receipt
                    and json.loads(receipt[0])["asset"]["asset_id"] == body.get("asset_id")
                    and (intent["kind"] == "capture" or not intent["cloud_job"])
                )
            elif kind == "cloud.completed" and intent["cloud_job"]:
                job = db.execute(
                    "SELECT status,result FROM conversation_delegations WHERE id=?",
                    (intent["cloud_job"],),
                ).fetchone()
                needed = bool(
                    job
                    and job["status"] == "completed"
                    and json.loads(job["result"]).get("local_asset_id")
                    == body.get("local_asset_id")
                )
            if needed:
                db.execute(
                    "UPDATE conversation_inputs SET status='queued',revision=? WHERE seq=?",
                    (conversation["revision"], event["seq"]),
                )
                self._audit(
                    db,
                    cid,
                    "conversation.continuation.resumed",
                    {"event_id": event["id"], "kind": kind, "intent_version": intent["version"]},
                )
                selected.add(kind)

    def recover_interactions(self):
        with self.connect() as db:
            db.execute("UPDATE conversation_inputs SET status='queued' WHERE status='running'")
            db.execute("UPDATE conversation_effects SET status='queued' WHERE status='running'")

    def capture_operation(self, task_id):
        if not task_id:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT id,status,result FROM conversation_effects WHERE task_id=?", (task_id,)
            ).fetchone()
        return (
            {**dict(row), "result": json.loads(row["result"]) if row["result"] else None}
            if row
            else None
        )

    def queue_capture(self, cid, revision, profile_digest, arguments):
        task_id = self.capture_task(cid, revision, profile_digest)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _, intent = self._current(db, cid, revision)
            if (
                intent["capture_task"] != task_id
                or intent["paused"]
                or intent["status"] == "cancelled"
            ):
                raise Conflict("intent changed before capture acceptance")
            effect_id = f"capture:{task_id}"
            old = db.execute(
                "SELECT * FROM conversation_effects WHERE id=?", (effect_id,)
            ).fetchone()
            if old:
                if old["status"] == "failed":
                    raise Conflict(
                        "failed capture requires an explicit new attempt, not an automatic retry"
                    )
                return {**dict(old), "result": json.loads(old["result"]) if old["result"] else None}
            db.execute(
                "INSERT INTO conversation_effects VALUES (?,?,?,?,?,?,'queued',NULL,?)",
                (
                    effect_id,
                    cid,
                    intent["version"],
                    revision,
                    task_id,
                    canonical(arguments),
                    time.time(),
                ),
            )
            intent["status"] = "capturing"
            db.execute("UPDATE conversations SET intent=? WHERE id=?", (canonical(intent), cid))
            self._audit(
                db,
                cid,
                "capture.accepted",
                {"effect_id": effect_id, "task_id": task_id, "intent_version": intent["version"]},
            )
        return {"id": effect_id, "status": "queued", "task_id": task_id, "result": None}

    def claim_effect(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in db.execute(
                "SELECT * FROM conversation_effects WHERE status='queued' ORDER BY created"
            ).fetchall():
                _, intent = self._current(db, row["conversation_id"])
                if intent["version"] != row["intent_version"] or intent["status"] == "cancelled":
                    db.execute(
                        "UPDATE conversation_effects SET status='superseded' WHERE id=?",
                        (row["id"],),
                    )
                    continue
                if intent["paused"] or intent.get("pending_question"):
                    continue
                latest = db.execute(
                    "SELECT revision,kind,status FROM conversation_inputs WHERE conversation_id=? ORDER BY seq DESC LIMIT 1",
                    (row["conversation_id"],),
                ).fetchone()
                if (
                    latest
                    and latest["revision"] > row["accepted_revision"]
                    and latest["status"] != "done"
                ):
                    # An unprocessed new user instruction may change the goal.
                    # Do not start an older queued physical effect in that gap.
                    continue
                db.execute(
                    "UPDATE conversation_effects SET status='running' WHERE id=?", (row["id"],)
                )
                return {**dict(row), "arguments": json.loads(row["arguments"])}
        return None

    def finish_effect(self, effect, status, result):
        if status not in {"completed", "failed"}:
            raise ValueError("invalid effect outcome")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _, intent = self._current(db, effect["conversation_id"])
            previous = db.execute(
                "SELECT status,result FROM conversation_effects WHERE id=?", (effect["id"],)
            ).fetchone()
            if previous and previous["status"] == "completed":
                if status != "completed" or previous["result"] != canonical(result):
                    raise Conflict("completed effect result changed")
                return "completed"
            if previous and previous["status"] == "superseded":
                return "superseded"
            stale = intent["version"] != effect["intent_version"] or intent["status"] == "cancelled"
            paused_interruption = (
                not stale
                and status == "failed"
                and (intent["paused"] or intent.get("pending_question"))
                and result.get("error") == "Conflict"
            )
            stored_status = "superseded" if stale else "queued" if paused_interruption else status
            db.execute(
                "UPDATE conversation_effects SET status=?,result=? WHERE id=?",
                (stored_status, canonical(result), effect["id"]),
            )
            if status == "completed":
                if not db.execute(
                    "SELECT 1 FROM actions WHERE id=?", (f"capture:{effect['task_id']}",)
                ).fetchone():
                    raise Conflict("cannot complete capture effect without committed receipt")
                db.execute(
                    "UPDATE tasks SET status='completed' WHERE id=? AND status NOT IN ('cancelled','expired')",
                    (effect["task_id"],),
                )
            elif not stale and not paused_interruption:
                db.execute(
                    "UPDATE tasks SET status='failed' WHERE id=? AND status NOT IN ('cancelled','expired')",
                    (effect["task_id"],),
                )
            if not stale:
                intent["status"] = (
                    "waiting" if intent["paused"] or status == "failed" else "asset_ready"
                )
                db.execute(
                    "UPDATE conversations SET intent=? WHERE id=?",
                    (canonical(intent), effect["conversation_id"]),
                )
            self._audit(
                db,
                effect["conversation_id"],
                "capture.effect.finished",
                {"effect_id": effect["id"], "status": stored_status, "result": result},
            )
            if not stale and not paused_interruption:
                kind = "capture.completed" if status == "completed" else "capture.failed"
                self.enqueue_event(
                    effect["conversation_id"],
                    kind + ":" + effect["id"],
                    kind,
                    result,
                    intent_version=effect["intent_version"],
                    _db=db,
                )
        return stored_status

    def queue_delegation(self, cid, revision, request):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            _, intent = self._current(db, cid, revision)
            if (
                intent["kind"] != "compose"
                or intent["paused"]
                or intent["status"] in {"cancelled", "unsupported"}
            ):
                raise Conflict("no current composition authorization")
            if request["intent_version"] != intent["version"]:
                raise Conflict("delegation snapshot is stale")
            if intent["cloud_job"]:
                row = db.execute(
                    "SELECT * FROM conversation_delegations WHERE id=?", (intent["cloud_job"],)
                ).fetchone()
                return {**dict(row), "body": json.loads(row["body"])}
            ident = request["id"]
            db.execute(
                "INSERT INTO conversation_delegations(id,conversation_id,intent_version,body,status,result,created) VALUES (?,?,?,?, 'pending',NULL,?)",
                (ident, cid, intent["version"], canonical(request), time.time()),
            )
            intent["cloud_job"] = ident
            intent["status"] = "cloud_pending"
            db.execute("UPDATE conversations SET intent=? WHERE id=?", (canonical(intent), cid))
            self._audit(
                db,
                cid,
                "cloud.delegation.queued",
                {"id": ident, "intent_version": intent["version"]},
            )
        return {
            "id": ident,
            "status": "pending",
            "body": request,
            "intent_version": intent["version"],
            "conversation_id": cid,
        }

    def delegations(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM conversation_delegations ORDER BY created").fetchall()
        return [
            {
                **dict(r),
                "body": json.loads(r["body"]),
                "result": json.loads(r["result"]) if r["result"] else None,
            }
            for r in rows
        ]

    def update_delegation(self, ident, status, result=None):
        if status not in {"accepted", "running", "completed", "failed", "superseded"}:
            raise ValueError("invalid delegation state")
        if status == "completed" and (
            not isinstance(result, dict)
            or result.get("verified") is not True
            or not result.get("local_asset_id")
        ):
            raise Conflict("completed delegation requires a locally verified transferred artifact")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM conversation_delegations WHERE id=?", (ident,)
            ).fetchone()
            if row is None:
                raise KeyError(ident)
            _, intent = self._current(db, row["conversation_id"])
            if (
                intent["version"] != row["intent_version"]
                or intent["status"] == "cancelled"
                or intent["cloud_job"] != ident
            ):
                status = "superseded"
            if row["status"] == "completed" and status == "completed":
                if row["result"] != canonical(result):
                    raise Conflict("completed delegation result changed")
                return False
            if row["status"] == "superseded":
                return False
            if status == "completed":
                asset = db.execute(
                    "SELECT task_id,kind,info FROM assets WHERE id=?", (result["local_asset_id"],)
                ).fetchone()
                if (
                    not asset
                    or asset["task_id"] != row["conversation_id"]
                    or asset["kind"] != "deliverable"
                ):
                    raise Conflict("cloud artifact belongs to another conversation or is missing")
                if json.loads(asset["info"])["asset"]["sha256"] != result.get("sha256"):
                    raise Conflict("cloud artifact receipt hash mismatch")
            if row["status"] == status and result is None:
                return False
            db.execute(
                "UPDATE conversation_delegations SET status=?,result=? WHERE id=?",
                (status, canonical(result) if result is not None else row["result"], ident),
            )
            if status != "superseded":
                intent["status"] = (
                    "waiting"
                    if intent["paused"] or status == "failed"
                    else "cloud_ready"
                    if status == "completed"
                    else "cloud_pending"
                )
                db.execute(
                    "UPDATE conversations SET intent=? WHERE id=?",
                    (canonical(intent), row["conversation_id"]),
                )
            self._audit(
                db,
                row["conversation_id"],
                "cloud.delegation.updated",
                {"id": ident, "status": status, "result": result},
            )
            if status in {"completed", "failed"}:
                kind = "cloud.completed" if status == "completed" else "cloud.failed"
                event_id = ("cloud:" if status == "completed" else "cloud-failed:") + ident
                self.enqueue_event(
                    row["conversation_id"],
                    event_id,
                    kind,
                    result or {},
                    intent_version=row["intent_version"],
                    _db=db,
                )
        return status == "completed"

    def delivery_failure(self, ident, *, maximum):
        with self.connect() as db:
            row = db.execute(
                "SELECT attempts FROM conversation_delegations WHERE id=?", (ident,)
            ).fetchone()
            if row is None:
                raise KeyError(ident)
            attempts = min(row["attempts"] + 1, maximum)
            delay = 30 if attempts >= maximum else min(30, 2**attempts)
            db.execute(
                "UPDATE conversation_delegations SET attempts=?,next_try=? WHERE id=?",
                (attempts, time.time() + delay, ident),
            )

    def delivery_reconnected(self, ident):
        with self.connect() as db:
            db.execute(
                "UPDATE conversation_delegations SET attempts=0,next_try=0 WHERE id=?", (ident,)
            )

    def cancellation_sent(self, ident):
        with self.connect() as db:
            db.execute("UPDATE conversation_delegations SET cancel_sent=1 WHERE id=?", (ident,))
