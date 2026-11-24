"""Persistent component health and deduplicated cloud handling, on the existing store."""

from __future__ import annotations

import json

from .contracts import canonical, digest


class RuntimeHealth:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS runtime_health_cursor (principal TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )
            db.execute("""CREATE TABLE IF NOT EXISTS runtime_health (
                id TEXT PRIMARY KEY, principal TEXT NOT NULL, body TEXT NOT NULL,
                signature TEXT NOT NULL, revision INTEGER NOT NULL, since REAL NOT NULL,
                updated REAL NOT NULL)""")

    def record(
        self,
        principal,
        component,
        code,
        *,
        task_id=None,
        version=None,
        generation=None,
        details=None,
        simulated=False,
    ):
        ident = "health-" + digest([principal, component, task_id])[:32]
        signature = digest([code, version, generation])
        now = self.store.clock()
        with self.store.connect() as db:
            old = db.execute("SELECT * FROM runtime_health WHERE id=?", (ident,)).fetchone()
            if (
                old
                and old["signature"] == signature
                and (code != "analysis_backlog" or now - old["updated"] < 30)
            ):
                return json.loads(old["body"])
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM runtime_health WHERE id=?", (ident,)).fetchone()
            changed = old is None or old["signature"] != signature
            # Health is a transition record, not a high-frequency heartbeat.
            # A legacy sustained-backlog producer may refresh at most every 30s.
            if not changed and (code != "analysis_backlog" or now - old["updated"] < 30):
                return json.loads(old["body"])
            revision = (old["revision"] if old else 0) + int(changed)
            since = now if changed else old["since"]
            body = dict(
                principal=principal,
                id=ident,
                component=component,
                code=code,
                task_id=task_id,
                version=version,
                generation=generation,
                details=details or {},
                simulated=simulated,
                revision=revision,
                since=since,
                updated=now,
                active=code not in {"healthy", "paused", "restarting"},
            )
            db.execute(
                "INSERT INTO runtime_health VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body,signature=excluded.signature,revision=excluded.revision,since=excluded.since,updated=excluded.updated",
                (ident, principal, canonical(body), signature, revision, since, now),
            )
            if changed:
                self.store._audit(db, task_id, "runtime.health.changed", body)
        return body

    def snapshot(self, principal):
        with self.store.connect() as db:
            return [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT body FROM runtime_health WHERE principal=? ORDER BY id", (principal,)
                )
            ]

    def cursor(self, principal, value=None):
        with self.store.connect() as db:
            if value is not None:
                db.execute(
                    "INSERT INTO runtime_health_cursor VALUES (?,?) ON CONFLICT(principal) DO UPDATE SET value=MAX(value,excluded.value)",
                    (principal, value),
                )
            row = db.execute(
                "SELECT value FROM runtime_health_cursor WHERE principal=?", (principal,)
            ).fetchone()
            return row[0] if row else 0

    def events(self, principal, after):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT seq,payload FROM audit WHERE seq>? AND kind='runtime.health.changed' AND json_extract(payload,'$.principal')=? ORDER BY seq LIMIT 100",
                (after, principal),
            ).fetchall()
        return {
            "after": after,
            "cursor": rows[-1][0] if rows else after,
            "events": [json.loads(r[1]) for r in rows],
        }

    def enqueue_cloud(
        self, principal, records, *, enabled=True, sustain_seconds=0, max_actions_per_hour=2
    ):
        """Only authenticated node projections call this; no model runs per poll."""
        if not enabled:
            return 0
        count = 0
        for record in records:
            if record.get("code") == "storage_review_required" and record.get("active"):
                from .storage_review import enqueue_review

                count += enqueue_review(self.store, principal, record)
                continue
            # A recovery updates the remote snapshot but does not trigger a new action.
            # Duration belongs to the reporting node's clock. Comparing two
            # hosts can silently discard a just-raised fault before advancing
            # the delivery cursor when the edge clock is slightly ahead.
            if not record.get("active") or (
                sustain_seconds > 0 and record["updated"] - record["since"] < sustain_seconds
            ):
                continue
            ident = "health-event-" + digest([record["id"], record["revision"]])[:32]
            with self.store.connect() as db:
                if db.execute("SELECT 1 FROM harness_events WHERE id=?", (ident,)).fetchone():
                    continue
            with self.store.connect() as db:
                actions = db.execute(
                    "SELECT COUNT(*) FROM harness_events WHERE principal=? AND kind='observation.alert' AND read_only=0 AND created>? AND json_extract(body,'$.health.task_id') IS ? AND json_extract(body,'$.health.component')=?",
                    (
                        principal,
                        self.store.clock() - 3600,
                        record.get("task_id"),
                        record["component"],
                    ),
                ).fetchone()[0]
            budget_exhausted = actions >= max_actions_per_hour
            # A backlog is pending work, not evidence that its healthy consumer
            # should be stopped. Let the model inspect/report without granting
            # a program-generated backlog notification device-control authority.
            backlog_notice = record.get("code") == "analysis_backlog"
            read_only = backlog_notice or budget_exhausted
            if budget_exhausted:
                with self.store.connect() as db:
                    warned = db.execute(
                        "SELECT 1 FROM harness_events WHERE principal=? AND kind='observation.alert' AND read_only=1 AND json_extract(body,'$.automatic_action_budget_exhausted')=1 AND created>? AND json_extract(body,'$.health.task_id') IS ? AND json_extract(body,'$.health.component')=? LIMIT 1",
                        (
                            principal,
                            self.store.clock() - 3600,
                            record.get("task_id"),
                            record["component"],
                        ),
                    ).fetchone()
                if warned:
                    continue  # Full transitions remain in the node's persistent journal.
            body = {
                "text": "端侧程序异常事件（不是用户新指令）。先查询当前状态。仅可在此次任务身份和已授权范围内暂停或恢复；不得改写用户目标、存储策略、采样或创建新任务。无法处理时说明原因。\n"
                + canonical(record),
                "automatic_action_budget_exhausted": budget_exhausted,
                "health": record,
                "simulated": record.get("simulated", False),
            }
            # A known task owns its incident. Unbound infrastructure faults are
            # status-page records and stay under the independent service guard.
            # Never run a maintenance model turn inside the user's main chat.
            with self.store.connect() as db:
                owners = db.execute(
                    """SELECT b.id FROM business_tasks b JOIN wake_rules r ON r.task_id=b.id
                    WHERE b.principal=? AND b.status NOT IN ('paused','completed','cancelled','expired')
                    AND r.deleted=0 AND json_extract(r.spec,'$.observation_task_id')=?""",
                    (principal, record.get("task_id")),
                ).fetchall() if record.get("task_id") else []
            owner = owners[0]["id"] if len(owners) == 1 else None
            if owner:
                body["conversation_id"] = self.store.conversations.for_task(principal, owner)["id"]
            else:
                read_only = True
            if backlog_notice:
                body["text"] = (
                    "端侧判断积压提醒（不是用户新指令）。本次只读查询当前进度并报告；"
                    "积压本身不是暂停依据，不得暂停或恢复录像/判断，也不得改配置或创建任务。"
                    "正在补处理时让它继续，不能把尚未判断的录像说成已完成。\n"
                    + canonical(record)
                )
            if budget_exhausted:
                body["text"] = "本小时自动操作额度已用完，本次只能查询并说明情况。\n" + body["text"]
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self.store.submit(
                    ident,
                    principal,
                    "system",
                    "observation.alert",
                    body,
                    task_id=owner,
                    read_only=read_only,
                    _db=db,
                )
                if budget_exhausted or owner is None:
                    reply = {
                        "text": "端侧异常已保留在服务状态页，由独立保护程序继续处理；未向主对话发送自动维护消息。",
                        "model_invoked": False,
                        "source": "runtime_health_budget",
                        "observed_state": record,
                        "simulated": record.get("simulated", False),
                        "event_id": ident,
                        "session_key": body.get("conversation_id"),
                        "quiet": True,
                    }
                    db.execute(
                        "UPDATE harness_events SET status='done',reply=?,session_key=?,finished=? WHERE id=?",
                        (canonical(reply), body.get("conversation_id"), self.store.clock(), ident),
                    )
            count += 1
        return count
