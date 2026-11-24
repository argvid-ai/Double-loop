"""Capacity transitions schedule work on an explicitly bound existing goal."""

import json

from .contracts import canonical, digest


def record_pressure(
    health, principal, policy, used_bytes, reserve_bytes, revision, simulated=False
):
    component = "recording_capacity"
    ident = "health-" + digest([principal, component, None])[:32]
    with health.store.connect() as db:
        row = db.execute("SELECT body FROM runtime_health WHERE id=?", (ident,)).fetchone()
    previous = json.loads(row[0]) if row else {}
    if not policy.enabled or not policy.review_task_id or not policy.quota_bytes:
        if previous.get("active"):
            health.record(principal, component, "healthy")
        return
    same_goal = previous.get("details", {}).get("review_task_id") == policy.review_task_id
    active = previous.get("active") and same_goal and previous.get("version") == revision
    # Preserve the writer's reservation through the pressure episode. A periodic
    # maintenance call with no reservation must not re-arm the same episode.
    reserved = max(
        reserve_bytes,
        previous.get("details", {}).get("reserved_bytes", 0) if active else 0,
    )
    projected = used_bytes + reserved
    if active and projected > policy.quota_bytes * policy.low_water:
        return
    if projected < policy.quota_bytes * policy.review_water:
        if previous.get("active"):
            health.record(principal, component, "healthy")
        return
    health.record(
        principal,
        component,
        "storage_review_required",
        version=revision,
        simulated=simulated,
        details={
            "review_task_id": policy.review_task_id,
            "used_bytes": used_bytes,
            "reserved_bytes": reserved,
            "projected_bytes": projected,
            "quota_bytes": policy.quota_bytes,
            "review_water": policy.review_water,
            "high_water": policy.high_water,
            "low_water": policy.low_water,
        },
    )


def enqueue_review(store, principal, record):
    task_id = record.get("details", {}).get("review_task_id")
    if not task_id:
        return 0
    ident = "storage-review-" + digest([record["id"], record["revision"]])[:32]
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM harness_events WHERE id=?", (ident,)).fetchone():
            return 0
        task = db.execute(
            "SELECT * FROM business_tasks WHERE id=? AND principal=?", (task_id, principal)
        ).fetchone()
        if (
            not task
            or task["kind"] != "goal"
            or task["status"] in {"paused", "cancelled", "completed", "expired"}
        ):
            store._audit(
                db,
                task_id if task else None,
                "storage.review.ignored",
                {"reason": "goal_not_active", "source": record},
            )
            return 0
        main = store.conversations.main(principal, db=db)
        conversation = store.conversations.for_task(principal, task_id, db=db)
        body = {
            "text": "原录像池达到已配置的整理水位。读取本事项目标、最新容量和候选记录，按已授权的保留要求处理。可继续制作、审阅并释放无需保留的原片保护，向主对话说明结果。原片回收由存储策略执行。",
            "conversation_id": conversation["id"],
            "origin_conversation_id": main["id"],
            "storage_pressure": record,
            "simulated": record.get("simulated", False),
        }
        pending = db.execute(
            "SELECT id FROM harness_events WHERE principal=? AND task_id=? AND kind='storage.review' AND status='queued' ORDER BY created LIMIT 1",
            (principal, task_id),
        ).fetchone()
        store.submit(ident, principal, "system", "storage.review", body, task_id=task_id, _db=db)
        if pending:
            db.execute("UPDATE harness_events SET body=? WHERE id=?", (canonical(body), pending[0]))
            db.execute(
                "UPDATE harness_events SET status='superseded',reply=?,finished=? WHERE id=?",
                (
                    canonical({"merged_into": pending[0], "model_invoked": False}),
                    store.clock(),
                    ident,
                ),
            )
        store._audit(
            db,
            task_id,
            "storage.review.routed",
            {
                "source": record,
                "event_id": pending[0] if pending else ident,
                "coalesced": bool(pending),
            },
        )
    return int(pending is None)
