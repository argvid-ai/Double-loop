"""Read-only activity projection. Reuses saved facts; never invokes a model."""

import json
import re
from pathlib import Path

from .store import Conflict


def unpack(value):
    return json.loads(value) if value else {}


def activity(store, principal, event_id=None):
    with store.connect() as db:
        origins = db.execute(
            "SELECT * FROM harness_events WHERE principal=? AND kind='autonomy' "
            + ("AND id=? " if event_id else "")
            + "ORDER BY created DESC LIMIT 20",
            (principal, event_id) if event_id else (principal,),
        ).fetchall()
        if event_id and not origins:
            raise Conflict("自主活动不存在或不属于当前用户")
        runs = []
        for origin in origins:
            body, reply = unpack(origin["body"]), unpack(origin["reply"])
            tasks = db.execute(
                "SELECT t.* FROM business_tasks t JOIN task_routes r ON r.task_id=t.id "
                "WHERE r.source_event=? AND r.principal=? AND t.principal=? ORDER BY t.created",
                (origin["id"], principal, principal),
            ).fetchall()
            task_ids = [t["id"] for t in tasks]
            placeholders = ",".join("?" for _ in task_ids) or "NULL"
            events = db.execute(
                "SELECT * FROM harness_events WHERE principal=? AND (id=? OR "
                "json_extract(body,'$.origin_event_id')=? OR task_id IN ("
                + placeholders
                + ")) ORDER BY created,rowid",
                (principal, origin["id"], origin["id"], *task_ids),
            ).fetchall()
            notices = [e for e in events if e["kind"] == "task.notification"]
            outputs = []
            for notice in notices:
                answer = unpack(notice["reply"])
                result = answer.get("reviewed_result") or {}
                if not result.get("verified") or result.get("review_status") != "approved":
                    continue
                manifest = result.get("media_manifest") or {}
                text = answer.get("text", "")
                title = re.search(r"^#{1,6}\s+([^\n]+)|^\*\*\[([^\]\n]+)\]", text)
                outputs.append(
                    {
                        "asset_id": result["asset_id"],
                        "title": manifest.get("title")
                        or (next(g for g in title.groups() if g) if title else "已交付影片"),
                        "duration_ms": manifest.get("asset", {}).get("duration_ms"),
                        "created": notice["created"],
                        "text": text,
                    }
                )
            unfinished = any(
                t["status"] not in {"completed", "cancelled", "expired", "failed"} for t in tasks
            )
            failed = bool(origin["error"]) or any(
                t["status"] in {"failed", "expired"} for t in tasks
            )
            cancelled = origin["status"] == "cancelled" or any(
                t["status"] == "cancelled" for t in tasks
            )
            status = (
                "failed"
                if failed
                else "working"
                if unfinished
                else "cancelled"
                if cancelled
                else "delivered"
                if outputs
                else "quiet"
                if reply.get("quiet")
                else "waiting"
                if reply.get("pending_receipt") and not tasks
                else "done"
                if origin["status"] == "done"
                else origin["status"]
            )
            finished = (
                None
                if status in {"working", "running", "queued", "waiting"}
                else max(
                    [e["finished"] or e["created"] for e in events] + [t["updated"] for t in tasks]
                )
            )
            run = {
                "id": origin["id"],
                "created": origin["created"],
                "finished": finished,
                "status": status,
                "manual": bool(body.get("manual")),
                "duties_file": Path(body.get("duties_file") or "AUTONOMY.md").name,
                "text": reply.get("text", ""),
                "error": origin["error"],
                "title": outputs[0]["title"]
                if outputs
                else ("按工作文件处理事项" if tasks else "查看近期情况"),
                "tasks": [{"id": t["id"], "goal": t["goal"], "status": t["status"]} for t in tasks],
                "outputs": outputs,
                "notifications": len(notices),
                "model_calls": sum(
                    unpack(e["reply"]).get("request_count", 0)
                    for e in events
                    if e["kind"] != "task.notification"
                ),
            }
            if event_id:
                ids = [e["id"] for e in events if e["kind"] != "task.notification"]
                rows = db.execute(
                    "SELECT seq,kind,payload,created FROM audit WHERE kind IN "
                    "('tool.started','tool.completed','tool.failed') "
                    "AND json_extract(payload,'$.principal')=? AND json_extract(payload,'$.event_id') IN ("
                    + ",".join("?" for _ in ids)
                    + ") ORDER BY seq LIMIT 600",
                    (principal, *ids),
                ).fetchall()
                calls = {}
                for row in rows:
                    payload = unpack(row["payload"])
                    key = (payload["event_id"], payload.get("call_id", row["seq"]))
                    call = calls.setdefault(
                        key,
                        {
                            "name": payload.get("name", "工具"),
                            "started": row["created"],
                            "status": "running",
                        },
                    )
                    # Do not send model messages, image bytes, or arbitrary tool output to this overview.
                    arguments = payload.get("arguments") or {}
                    call["input"] = {
                        k: v
                        for k, v in arguments.items()
                        if k
                        in {
                            "path",
                            "command",
                            "enabled",
                            "interval_seconds",
                            "next_wake_in_seconds",
                            "task_id",
                            "asset_id",
                            "positions_ms",
                            "action",
                            "title",
                            "query",
                        }
                    }
                    if row["kind"] != "tool.started":
                        call["finished"] = row["created"]
                        call["status"] = "failed" if row["kind"] == "tool.failed" else "done"
                        call["error"] = payload.get("error_type")
                run["tools"] = list(calls.values())
                run["events"] = [
                    {
                        "kind": e["kind"],
                        "created": e["created"],
                        "finished": e["finished"],
                        "status": e["status"],
                        "text": unpack(e["reply"]).get("text", ""),
                        "error": e["error"],
                    }
                    for e in events
                ]
            runs.append(run)
        if event_id:
            return runs[0]
        changes = []
        for row in db.execute(
            "SELECT created,payload FROM audit WHERE kind='autonomy.configured' "
            "AND json_extract(payload,'$.principal')=? ORDER BY seq DESC LIMIT 12",
            (principal,),
        ):
            p = unpack(row["payload"])
            changes.append(
                {
                    "created": row["created"],
                    "by": "user" if p.get("source") == "user-interface" else "model",
                    "config": p.get("config", {}),
                    "duties_changed": p.get("duties_changed", False),
                    "persona_changed": p.get("persona_changed", False),
                }
            )
        return {"runs": runs, "changes": changes, "limit": 20, "now": store.clock()}
