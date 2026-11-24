"""Read-only operational summaries. Never call a model to describe a failure."""
import json
from pathlib import Path


def snapshot(store, principal):
    result = {"checked_at": store.clock(), "items": []}
    with store.connect() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "runtime_health" in tables:
            for row in db.execute("SELECT body FROM runtime_health WHERE principal=?", (principal,)):
                health = json.loads(row[0])
                if health["component"].startswith("worker_") and health["active"]:
                    result["items"].append(dict(
                        component=health["component"], severity="warning",
                        title="一项后台工作出错，程序正在自动重试",
                        reason=health["details"].get("error_type"),
                        retry_seconds=health["details"].get("retry_seconds"),
                        observed_at=health["updated"]))
        if {"observation_defaults", "observation_tasks", "observation_media"} <= tables:
            task = db.execute(
                """SELECT t.*,d.body AS default_configuration FROM observation_defaults d JOIN observation_tasks t
                ON t.task_id=json_extract(d.body,'$.task_id') AND t.principal=d.principal
                WHERE d.principal=?""", (principal,),
            ).fetchone()
            if task:
                cfg = json.loads(task["config"])
                progress = db.execute(
                    """SELECT MAX(received_at) received,MAX(seen_at) judged,
                    COALESCE(SUM(CASE WHEN kind='batch' AND seen IS NULL THEN end_ms-start_ms ELSE 0 END),0) pending,
                    COALESCE(SUM(CASE WHEN gap_reason='media_unavailable' THEN end_ms-start_ms ELSE 0 END),0) unavailable
                    FROM observation_media WHERE task_id=?""", (task["task_id"],),
                ).fetchone()
                active = task["status"] in {"running", "starting"}
                expired = bool(cfg.get("capture_until") and store.clock() >= cfg["capture_until"])
                current_control = json.loads(task["last_control"] or "{}") if "last_control" in task.keys() else {}
                last = [current_control["action"]] if current_control.get("action") else None
                fault = not active and task["status"] == "paused" and bool(
                    task["reason"] and (not last or last[0] not in {"pause", "stop"}))
                automatic = bool(json.loads(task["default_configuration"]).get("enabled")
                                 and not expired and (cfg.get("expires_at") is None or cfg.get("resume_after_restart")))
                title = ("本轮已到期，录像曾异常中断" if expired and fault else
                         "录制已到期" if expired else "录像运行中" if active
                         else "录像异常，等待自动恢复" if fault and automatic
                         else "录像已异常停止" if fault
                         else "录像已关闭，当前不会产生新素材" if task["status"] == "stopped"
                         else "录像已暂停")
                severity = "warning" if active and progress["received"] and store.clock()-progress["received"] > 30 else (
                    "error" if fault else "warning" if task["status"] == "stopped" else "ok")
                if active and severity == "warning":
                    title = "录像任务仍开着，但暂未收到新片段"
                result["items"].append(dict(component="recording", title=title,
                    severity=severity, last_success=progress["received"], reason=task["reason"],
                    task_id=task["task_id"], deadline=cfg.get("capture_until")))
                issue = None
                if "runtime_health" in tables:
                    row = db.execute(
                        """SELECT body FROM runtime_health WHERE principal=?
                        AND json_extract(body,'$.task_id')=?
                        AND json_extract(body,'$.component')='visual_worker'""",
                        (principal, task["task_id"]),
                    ).fetchone()
                    issue = json.loads(row[0]) if row else None
                pending = max(0, progress["pending"]-progress["unavailable"])/1000
                result["items"].append(dict(component="analysis",
                    title="判断正在等待恢复" if issue and issue["active"] else "本地判断",
                    severity="warning" if (issue and issue["active"]) or pending > 30 else "ok",
                    last_success=progress["judged"], pending_seconds=pending,
                    unavailable_seconds=progress["unavailable"]/1000,
                    reason=issue["code"] if issue else None))
        if store.side == "cloud":
            model = db.execute(
                """SELECT payload,created FROM audit WHERE kind='model.response'
                AND json_extract(payload,'$.principal')=? ORDER BY seq DESC LIMIT 1""",
                (principal,),
            ).fetchone()
            if model:
                value = json.loads(model["payload"])
                failed = bool(value.get("error"))
                message = value.get("error_message") or ""
                denied = any(s in message.lower() for s in ("forbidden","unauthorized","401","403"))
                result["items"].append(dict(component="cloud_model",
                    title="云端上游拒绝访问，需检查网关" if denied else
                          "云端请求失败，任务保留等待接续" if failed else "云端最近请求成功",
                    severity="error" if failed else "ok", observed_at=model["created"],
                    # Do not expose provider response text or credentials.
                    reason="access_denied" if denied else "request_failed" if failed else None))
            pending = db.execute(
                """SELECT id FROM business_tasks WHERE principal=?
                AND json_extract(result,'$.review_required')=1
                AND COALESCE(json_extract(result,'$.review_status'),'')!='approved'
                AND status NOT IN ('cancelled','completed','expired')""", (principal,),
            ).fetchall()
            if pending:
                result["items"].append(dict(component="delivery", severity="warning",
                    title=f"{len(pending)}项影片尚未完成复看交付", pending_count=len(pending)))
    guard_path = store.meta("service_guard_status_file", None)
    if guard_path:
        try:
            with Path(guard_path).open() as stream:
                guard = json.loads(stream.read(65536))
            if store.clock() - guard["checked_at"] > 120:
                result["items"].append(dict(
                    component="service_guard", severity="warning",
                    title="服务保护检查已超过两分钟未更新", observed_at=guard["checked_at"]))
            elif guard.get("mode") == "maintenance":
                result["items"].append(dict(
                    component="service_guard", severity="warning",
                    title="服务处于维护模式，自动重启暂时关闭", observed_at=guard["checked_at"]))
            else:
                for unit, record in guard.get("services", {}).items():
                    if record.get("status") not in {"healthy", "transitioning"}:
                        result["items"].append(dict(
                            component="service_guard", severity="warning",
                            title=unit + " 无响应，服务保护正在尝试恢复",
                            reason=record.get("status"), observed_at=record.get("observed_at"),
                            retry_seconds=max(0, record.get("next_recovery", 0) - store.clock())))
        except (OSError, ValueError, KeyError, TypeError):
            result["items"].append(dict(component="service_guard", severity="warning",
                                       title="暂未取得服务保护状态"))
    return result
