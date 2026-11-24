"""Shared-recorder facts and notification enrollment, without camera authority."""
import asyncio
from datetime import datetime
import json
from zoneinfo import ZoneInfo

import httpx

from .contracts import canonical, digest

SHARED_ORIGIN = "shared-default-recording"


def display_recording_clock(value):
    """Supply a readable state clock; never relabel receipt time as capture time."""
    task = value.get("task")
    if task and isinstance(task.get("updated"), (int, float)):
        task["state_updated_at"] = datetime.fromtimestamp(
            task["updated"], ZoneInfo("Asia/Shanghai")).isoformat()
        task["state_time_meaning"] = (
            "这是当前录像状态更新时刻，已换算为北京时间；直接引用，不再换算。"
            "不是文件末尾、画面拍摄或本轮查看时刻；不能单凭此字段认定是谁停止。"
        )
    return value


def current_recording(store, principal):
    """Bounded current rows only; no media totals or command/audit history scan."""
    with store.connect() as db:
        row = db.execute(
            """SELECT d.revision,d.body,t.task_id,t.version,t.generation,t.status,
                      t.reason,t.last_control,t.config,t.updated
               FROM observation_defaults d LEFT JOIN observation_tasks t
                 ON t.principal=d.principal AND t.task_id=json_extract(d.body,'$.task_id')
               WHERE d.principal=?""", (principal,),
        ).fetchone()
        if row is None:
            return {"checked_at": store.clock(), "reachable": True, "configured": False}
        configuration = json.loads(row["body"])
        if row["task_id"] is None:
            return {"checked_at": store.clock(), "reachable": True, "configured": True,
                    "configuration": configuration, "task": None, "error": "task_missing"}
        task = {key: row[key] for key in ("task_id", "version", "generation", "status", "reason", "updated")}
        cfg = json.loads(row["config"])
        task.update(source_ref=cfg["source_ref"], expires_at=cfg.get("expires_at"),
                    last_control=json.loads(row["last_control"] or "{}"))
        latest = {}
        for name, condition in (("latest_input", "1"), ("latest_judged", "seen IS NOT NULL")):
            item = db.execute(
                f"""SELECT sequence,segment_id,start_ms,end_ms,received_at,seen_at
                    FROM observation_media WHERE task_id=? AND kind='batch' AND {condition}
                    ORDER BY sequence DESC LIMIT 1""", (task["task_id"],),
            ).fetchone()
            latest[name] = dict(item) if item else None
        return display_recording_clock({
            "checked_at": store.clock(), "reachable": True, "configured": True,
            "revision": row["revision"], "configuration": configuration, "task": task, **latest,
            "meaning": "实际录像状态；标记分页不是新录像进度。stop指令不证明是用户本人发起。",
        })


class SharedRecordingStatus:
    def __init__(self, bridge):
        self.bridge, self.store = bridge, bridge.store
        self.next_poll = 0
        self.lock = asyncio.Lock()

    def snapshot(self, principal):
        value = self.store.meta("shared_recording_status:" + principal, None)
        if value is None:
            return {"reachable": False, "error": "not_checked"}
        age = max(0, self.store.clock() - value["checked_at"])
        if age > 2 * self.bridge.host.settings.observation_status_fallback_seconds:
            return {"reachable": False, "error": "stale_status", "last_observed": value}
        return value

    async def refresh(self, principal="local-user", *, force=False):
        if self.bridge.host.side != "cloud" or (not force and self.store.clock() < self.next_poll):
            return
        async with self.lock:
            if not force and self.store.clock() < self.next_poll:
                return
            self.next_poll = self.store.clock() + self.bridge.host.settings.observation_status_fallback_seconds
            try:
                response = await self.bridge.host.client.get("/harness/observation/current", timeout=2)
                response.raise_for_status()
                value = response.json()
                if not isinstance(value, dict) or value.get("reachable") is not True:
                    raise ValueError("invalid current recorder response")
                display_recording_clock(value)  # Also accepts older node responses.
                # Reception time is local; the node's time remains explicit.
                value["node_checked_at"] = value.get("checked_at")
                value["checked_at"] = self.store.clock()
            except (httpx.HTTPError, ValueError):
                value = {"checked_at": self.store.clock(), "reachable": False,
                         "error": "recording_status_unavailable"}
            await asyncio.to_thread(self._save, principal, value)
            return value

    def _save(self, principal, value):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)",
                       ("shared_recording_status:" + principal, canonical(value)))
            task = value.get("task")
            if not task:
                return
            revision = "shared:" + digest([
                value["revision"], task["task_id"], task["version"],
                task["generation"], task["status"], task.get("last_control"),
            ])
            old = db.execute(
                "SELECT source_event,revision FROM observation_remote_tasks WHERE principal=? AND task_id=?",
                (principal, task["task_id"]),
            ).fetchone()
            if old and old["source_event"] != SHARED_ORIGIN:
                return  # Preserve the real originating conversation when enrolled.
            if old is None:
                db.execute(
                    """INSERT INTO observation_remote_tasks(principal,task_id,source_event,revision)
                    VALUES (?,?,?,?)""", (principal, task["task_id"], SHARED_ORIGIN, revision),
                )
            elif old["revision"] != revision:
                db.execute(
                    """UPDATE observation_remote_tasks SET revision=?,active=1,next_poll=0
                    WHERE principal=? AND task_id=?""", (revision, principal, task["task_id"]),
                )
