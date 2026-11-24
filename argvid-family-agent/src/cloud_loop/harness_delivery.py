"""User-owned result delivery, independent of task execution and model wakes."""

import json
from contextlib import nullcontext
from typing import Literal

from pydantic import Field, model_validator

from .contracts import ID, Contract, canonical, digest
from .store import Conflict


class DeliveryPolicy(Contract):
    mode: Literal["on_result", "interval", "on_request"] = "on_result"
    interval_seconds: int | None = Field(default=None, ge=60, le=366 * 86400)
    allow_agent_changes: bool = False

    @model_validator(mode="after")
    def period(self):
        if (self.mode == "interval") != (self.interval_seconds is not None):
            raise ValueError("only interval delivery requires interval_seconds")
        return self


class DeliveryQuery(Contract):
    task_id: str | None = Field(default=None, pattern=ID)


class DeliveryUpdate(DeliveryQuery):
    expected_revision: int = Field(ge=0)
    policy: DeliveryPolicy | None


class AttentionRequest(Contract):
    task_id: str = Field(pattern=ID)
    question: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=1000)


class ResultDelivery:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS delivery_preferences (
                    principal TEXT NOT NULL, scope TEXT NOT NULL,
                    revision INTEGER NOT NULL, policy TEXT, updated REAL NOT NULL,
                    source TEXT NOT NULL, PRIMARY KEY(principal,scope)
                );
                CREATE TABLE IF NOT EXISTS delivery_results (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, task_id TEXT NOT NULL,
                    packet TEXT NOT NULL, created REAL NOT NULL, published REAL
                );
                CREATE INDEX IF NOT EXISTS delivery_results_pending
                    ON delivery_results(principal,task_id,published,created);
                CREATE INDEX IF NOT EXISTS delivery_pending_groups
                    ON delivery_results(principal,task_id,created) WHERE published IS NULL;
                CREATE TABLE IF NOT EXISTS delivery_periods (
                    principal TEXT NOT NULL, task_id TEXT NOT NULL, last_sent REAL NOT NULL,
                    PRIMARY KEY(principal,task_id)
                );
            """)
            if "agent_grant" not in {r[1] for r in db.execute("PRAGMA table_info(delivery_preferences)")}:
                db.execute("ALTER TABLE delivery_preferences ADD COLUMN agent_grant INTEGER")
                db.execute("UPDATE delivery_preferences SET agent_grant=json_extract(policy,'$.allow_agent_changes')")
        self.next_poll = 0

    def read(self, principal, task_id=None, *, db=None, validate=True):
        with nullcontext(db) if db is not None else self.store.connect() as conn:
            if task_id and validate:
                self.store._task(conn, task_id, principal)
            scope = task_id or ""
            own = conn.execute(
                "SELECT * FROM delivery_preferences WHERE principal=? AND scope=?",
                (principal, scope),
            ).fetchone()
            default = conn.execute(
                "SELECT * FROM delivery_preferences WHERE principal=? AND scope=''",
                (principal,),
            ).fetchone()
            chosen = own if own and own["policy"] else default
            policy = json.loads(chosen["policy"]) if chosen and chosen["policy"] else DeliveryPolicy().model_dump()
            grant = own["agent_grant"] if own and own["agent_grant"] is not None else (
                default["agent_grant"] if default else False)
            policy["allow_agent_changes"] = bool(grant)
            queued = conn.execute(
                "SELECT COUNT(*) FROM delivery_results WHERE principal=? AND (? IS NULL OR task_id=?) AND published IS NULL",
                (principal, task_id, scope),
            ).fetchone()[0]
            last = conn.execute(
                "SELECT last_sent FROM delivery_periods WHERE principal=? AND task_id=?",
                (principal, scope),
            ).fetchone()
            anchor = max(chosen["updated"] if chosen else 0, last[0] if last else 0)
            return {
                "task_id": task_id, "revision": own["revision"] if own else 0,
                "policy": json.loads(own["policy"]) if own and own["policy"] else None,
                "effective": policy,
                "inherited": bool(task_id and (not own or not own["policy"])),
                "source": "task" if task_id and own and own["policy"] else
                          "user_default" if chosen and chosen["policy"] else "existing_behavior",
                "pending_results": queued,
                "next_delivery_at": anchor + policy["interval_seconds"]
                    if policy["mode"] == "interval" and queued else None,
                "meaning": "notification only; does not alter recording, task wakes or stored results",
            }

    def configure(self, principal, update, *, source, agent_task=None):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.read(principal, update.task_id, db=db)
            if current["revision"] != update.expected_revision:
                raise Conflict("汇报设置已被修改，请重新读取后再保存")
            if agent_task is not None:
                if (update.task_id != agent_task or not current["effective"]["allow_agent_changes"]
                        or update.policy is None or not update.policy.allow_agent_changes):
                    raise Conflict("任务只能在用户授权后调整自己的汇报安排，不能改变授权或通用设置")
                previous = db.execute(
                    "SELECT agent_grant FROM delivery_preferences WHERE principal=? AND scope=?",
                    (principal, update.task_id),
                ).fetchone()
                grant = previous[0] if previous else None
            else:
                grant = int(update.policy.allow_agent_changes) if update.policy else None
            db.execute(
                "INSERT INTO delivery_preferences VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(principal,scope) DO UPDATE SET revision=excluded.revision,"
                "policy=excluded.policy,updated=excluded.updated,source=excluded.source,"
                "agent_grant=excluded.agent_grant",
                (principal, update.task_id or "", current["revision"] + 1,
                 canonical(update.policy) if update.policy is not None else None,
                 self.store.clock(), source, grant),
            )
            self.store._audit(db, update.task_id, "delivery.configured",
                              {"source": source, "update": update.model_dump()})
            return self.read(principal, update.task_id, db=db)

    def work_result(self, event, reply, db):
        """Do not infer user-facing importance from arbitrary generated prose."""
        if reply.get("pending_receipt") or reply.get("quiet"):
            return False
        if self.answers_user(event):
            return True
        if reply.get("error"):
            return False
        if reply.get("reviewed_result"):
            return reply["reviewed_result"].get("review_status") == "approved"
        task = db.execute(
            "SELECT status FROM business_tasks WHERE id=? AND principal=?",
            (event.get("task_id"), event["principal"]),
        ).fetchone()
        return bool(task and task["status"] == "completed")

    @staticmethod
    def answers_user(event):
        body = event.get("body", {})
        return (event["kind"] == "work.message" and body.get("request_kind") == "question"
                and body.get("source_kind") in {"user", "voice"} and body.get("user_directed") is True)

    def offer(self, principal, task_id, packet, *, db, attention=False):
        target = self.store.conversations.get(principal, packet["target"], db=db)
        if target["kind"] != "main":
            raise Conflict("user result delivery must target its owner's main conversation")
        ident = packet["id"]
        old = db.execute("SELECT * FROM delivery_results WHERE id=?", (ident,)).fetchone()
        if old:
            if (old["principal"] != principal or old["task_id"] != (task_id or "")
                    or old["packet"] != canonical(packet)):
                raise Conflict("交付编号已对应不同的结果，不能覆盖")
            return {"status": "already_recorded", "id": ident}
        db.execute("INSERT INTO delivery_results VALUES (?,?,?,?,?,NULL)",
                   (ident, principal, task_id or "", canonical(packet), self.store.clock()))
        policy = self.read(principal, task_id, db=db, validate=False)
        if attention or policy["effective"]["mode"] == "on_result":
            self._emit(db, principal, task_id, packet)
            db.execute("UPDATE delivery_results SET published=? WHERE id=?",
                       (self.store.clock(), ident))
            return {"status": "notified", "id": ident}
        return {"status": "stored_for_delivery", "id": ident,
                "mode": policy["effective"]["mode"]}

    def _emit(self, db, principal, task_id, packet):
        ident, target = packet["id"], packet["target"]
        local_task = task_id if db.execute(
            "SELECT 1 FROM business_tasks WHERE id=? AND principal=?", (task_id, principal)
        ).fetchone() else None
        self.store.submit(ident, principal, packet["channel"], packet["kind"],
                          packet["body"], task_id=local_task, read_only=True, _db=db)
        reply = {**packet["reply"], "event_id": ident, "session_key": target,
                 "delivery_managed": True}
        db.execute(
            "UPDATE harness_events SET status='done',session_key=?,reply=?,finished=? WHERE id=?",
            (target, canonical(reply), self.store.clock(), ident),
        )
        self.store.context_mail.put(
            principal, target, ident,
            {"origin": "user_delivery", "task_id": task_id,
             "source_event": packet["body"].get("execution_event_id")
                 or packet["body"].get("report", {}).get("source_event")
                 or packet["body"].get("origin_event_id"),
             "result": reply, "authority": "result_only_not_new_authorization"}, db=db)
        self.store._audit(db, task_id, "delivery.notified", {"event_id": ident, "target": target})

    def flush(self):
        now = self.store.clock()
        if now < self.next_poll:
            return 0
        self.next_poll = now + 1
        count = 0
        with self.store.connect() as db:
            groups = db.execute(
                "SELECT DISTINCT principal,task_id FROM delivery_results WHERE published IS NULL LIMIT 100"
            ).fetchall()
            if not groups:
                return 0
            db.execute("BEGIN IMMEDIATE")
            for group in groups:
                principal, task_id = group["principal"], group["task_id"] or None
                policy = self.read(principal, task_id, db=db, validate=False)
                mode = policy["effective"]["mode"]
                if mode == "on_request" or (mode == "interval" and now < policy["next_delivery_at"]):
                    continue
                rows = db.execute(
                    "SELECT * FROM delivery_results WHERE principal=? AND task_id=? AND published IS NULL ORDER BY created,id LIMIT 100",
                    (principal, task_id or ""),
                ).fetchall()
                packets = [json.loads(r["packet"]) for r in rows]
                first = packets[0]
                if len(packets) == 1:
                    packet = first
                else:
                    ident = "digest-" + digest([r["id"] for r in rows])[:32]
                    text = "这项任务有 " + str(len(packets)) + " 项新成果，已保存：\n" + "\n".join(
                        "- " + p["reply"].get("text", "新成果已入库")[:400] for p in packets[:5])
                    if len(packets) > 5:
                        text += "\n其余成果可在对应任务和成片库查看。"
                    packet = {
                        **first, "id": ident, "kind": "task.notification",
                        "body": {**first["body"], "text": text},
                        "reply": {"text": text, "model_invoked": False, "audio_generated": False,
                                  "source": "delivery_digest",
                                  "simulated": any(p["reply"].get("simulated") for p in packets),
                                  "result_count": len(packets),
                                  "delivery_ids": [r["id"] for r in rows]},
                    }
                self._emit(db, principal, task_id, packet)
                db.executemany("UPDATE delivery_results SET published=? WHERE id=?",
                               [(now, r["id"]) for r in rows])
                db.execute("INSERT INTO delivery_periods VALUES (?,?,?) ON CONFLICT(principal,task_id) "
                           "DO UPDATE SET last_sent=excluded.last_sent",
                           (principal, task_id or "", now))
                count += 1
        return count

    def attention(self, event, args):
        """Explicit task-to-owner question; does not pause shared services."""
        from .harness_authority import require_work_target

        if event["read_only"] or not event.get("task_id"):
            raise Conflict("需要正在执行的任务才能提出求助")
        require_work_target(self.store, event, args.task_id)
        saved = self.store.event(event["id"], event["principal"])
        if saved["status"] != "running" or saved["task_id"] != args.task_id:
            raise Conflict("求助必须来自当前任务")
        with self.store.connect() as db:
            target = self.store.conversations.for_task(event["principal"], args.task_id, db=db)
            visited = set()
            while target["kind"] != "main" and target.get("parent_id") and target["id"] not in visited:
                visited.add(target["id"])
                target = self.store.conversations.get(event["principal"], target["parent_id"], db=db)
            if target["kind"] != "main":
                raise Conflict("任务没有已登记的主对话交接关系")
            version = self.store._task(db, args.task_id, event["principal"])["version"]
            ident = "attention-" + digest([args.task_id, version, args.question.strip()])[:32]
            if db.execute("SELECT 1 FROM delivery_results WHERE id=? AND principal=? AND task_id=?",
                          (ident, event["principal"], args.task_id)).fetchone():
                return {"status": "already_recorded", "id": ident}
            text = args.question.strip() + "\n" + args.reason.strip()
            return self.offer(event["principal"], args.task_id, {
                "id": ident, "target": target["id"], "kind": "task.notification",
                "channel": "system", "body": {"text": text, "task_id": args.task_id,
                    "conversation_id": target["id"], "origin_event_id": event["id"]},
                "reply": {"text": text, "source": "user_attention", "model_invoked": False,
                          "audio_generated": False, "simulated": bool(event["body"].get("simulated"))},
            }, db=db, attention=True)
