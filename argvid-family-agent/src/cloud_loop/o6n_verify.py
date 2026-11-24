"""Real-model Mock verification through cloud APIs only; never completes edge tasks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone

import httpx


def check_edge_failure(task):
    """A failed/unsupported edge must not leave verification waiting for hours."""
    status = task.get("status")
    if status in {"unsupported", "cancelled", "expired"} or (
        status in {"failed", "needs_input"}
        and not any(e.get("status") == "queued" for e in task.get("events", []))
    ):
        raise RuntimeError(f"edge task ended as {status}")


def write_report(folder, evidence):
    """Readable report derived from saved observations, not a second task plan."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "trajectory.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
    status = evidence["status"]
    env = evidence.get("environment", {})
    cloud = env.get("cloud", {})
    node = env.get("node", {})
    profile = evidence.get("profile", {})
    rows = []
    for item in evidence.get("timeline", []):
        edge = item["kind"].startswith("edge.")
        record = item["payload"] if edge else item
        payload = record.get("payload", {})
        stamp = datetime.fromtimestamp(
            record.get("created", item["created"]), timezone.utc
        ).isoformat(timespec="seconds")
        detail = (
            payload.get("name")
            or payload.get("summary")
            or payload.get("status")
            or payload.get("model")
            or payload.get("text")
            or ""
        )
        detail = str(detail).replace("|", "\\|").replace("\n", " ")
        rows.append(
            f"| {stamp} | {'端侧' if edge else '云端'} | {record['kind']} | {detail[:240]} |"
        )
    artifacts = evidence.get("cloud_task", {}).get("assets", [])
    lines = [
        "# Mock 运行报告",
        "",
        f"运行结论：**{status}**。验证范围：**{evidence['scope']}**。",
        "",
        "本报告只覆盖这一条实际场景；完整阶段是否通过，以阶段任务验收表为准。",
        "",
        "## 模型与运行位置",
        "",
        "| 一侧 | 主机 | 模型 | 提供者 |",
        "|---|---|---|---|",
        f"| 云端 | {cloud.get('hostname', '未记录')} | {cloud.get('model', '未记录')} | traex-bridge |",
        f"| 端侧 | {node.get('hostname', '未记录')} | {profile.get('edge', {}).get('model', '未记录')} | {'llama.cpp 本地接口' if 'llamacpp-qwen' in profile.get('edge', {}).get('plugins', []) else 'traex-bridge 远程接口'} |",
        "",
        "本地模型权重的来源与校验须结合模型准备清单；节点名称或模型别名本身不能证明权重已核验。",
        "",
        "## 数据来源",
        "",
        "视频来自已登记的 Wikimedia 授权实拍素材，端侧使用视频文件回放，非本次拍摄。音频输入为人工编写的模拟转写文本，未调用语音识别；机械臂为模拟状态与回执。",
        "",
        "实际文件的作者、许可、来源页、选段、尺寸与校验值保存在下方素材清单及完整轨迹中。未测量的画面语义和物理安全指标不算通过。",
        "",
        "## 实际产物",
        "",
    ]
    for asset in artifacts:
        lines += [f"- `{asset['id']}`：{asset['kind']}；清单见完整轨迹中的 `cloud_task.assets`。"]
    if not artifacts:
        lines += ["没有取得产物；不能宣称视频任务完成。"]
    lines += [
        "",
        "## 验证观察",
        "",
        "```json",
        json.dumps(evidence.get("checks", {}), ensure_ascii=False, indent=2),
        "```",
        "",
        "## 完整运行顺序（UTC）",
        "",
        "| 时间 | 一侧 | 事件 | 说明 |",
        "|---|---|---|---|",
        *rows,
        "",
        "工具参数、工具回执、模型回答、输入来源和原始时间均保存在 [trajectory.json](trajectory.json)。不导出模型隐藏推理或凭据。",
        "",
    ]
    if evidence.get("failure"):
        lines += [
            "## 失败记录",
            "",
            f"本次失败：`{evidence['failure']}`。保留本次轨迹，修复后使用新的运行记录，不覆盖失败证据。",
            "",
        ]
    (folder / "report.md").write_text("\n".join(lines))


async def verify(profile, root):
    started = time.time()
    folder = (
        root
        / "evidence"
        / f"mock-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{time.time_ns() % 1000000}"
    )
    evidence = {
        "status": "running",
        "scope": "尚未识别运行位置",
        "started_at": started,
        "profile": profile.model_dump(mode="json"),
        "checks": {},
        "controls": [],
        "timeline": [],
    }
    task_id = None
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{profile.cloud_port}", trust_env=False, timeout=30
    ) as client:

        async def get(url):
            r = await client.get(url)
            r.raise_for_status()
            return r.json()

        async def post(url, data):
            r = await client.post(url, json=data)
            r.raise_for_status()
            return r.json()

        async def until(check, seconds=None):
            deadline = time.monotonic() + (seconds or profile.max_task_seconds)
            while time.monotonic() < deadline:
                result = await check()
                if result:
                    return result
                await asyncio.sleep(0.5)
            raise TimeoutError("verification condition did not occur within its budget")

        async def control(body):
            queued = await post("/api/edge/controls", body)

            async def applied():
                for row in await get("/api/edge/controls"):
                    if row["id"] == queued["id"] and row["result"]:
                        if row["result"]["status"] != "applied":
                            raise RuntimeError("edge rejected verification input")
                        return row

            result = await until(applied, 30)
            evidence["controls"].append(result)
            return result

        async def edge_wait(previous_runs=0, paused=None):
            async def ready():
                task = await get(f"/tasks/{task_id}")
                if task["status"] in {"failed", "unsupported", "expired", "cancelled"}:
                    raise RuntimeError(f"cloud ended as {task['status']}")
                snapshots = await get(f"/tasks/{task_id}/edge")
                for snap in snapshots:
                    local = snap.get("task", {})
                    check_edge_failure(local)
                    if (
                        local.get("status") == "waiting"
                        and len(local.get("runs", [])) > previous_runs
                        and (paused is None or bool(local["context"].get("paused")) == paused)
                    ):
                        if local["assets"]:
                            raise RuntimeError("capture happened while waiting or paused")
                        return local

            return await until(ready)

        try:
            environment = await get("/api/status")
            evidence["environment"] = environment
            if not environment["node"].get("online"):
                raise RuntimeError("edge is offline")
            if any(
                environment[side].get("profile_digest") != profile.fingerprint
                for side in ("cloud", "node")
            ):
                raise RuntimeError(
                    "running services differ from the requested verification profile"
                )
            distinct = environment["node"].get("hostname") != environment["cloud"].get("hostname")
            local_model = "llamacpp-qwen" in profile.edge.plugins
            evidence["scope"] = (
                "两机 Mock / 本地模型"
                if distinct and local_model
                else "同机或远程模型联调；不是 O6N 本地模型验收"
            )
            await control({"kind": "stage", "stage": "S0"})
            scenario = await get("/api/scenario")
            task = await post("/tasks", scenario)
            task_id = task["id"]
            evidence["task_id"] = task_id
            local = await edge_wait()
            evidence["checks"]["S0_wait_without_assets"] = True
            print(f"task={task_id} S0 waiting", flush=True)
            await control({"kind": "stage", "stage": "S1"})
            local = await edge_wait(len(local["runs"]))
            evidence["checks"]["S1_wait_without_assets"] = True
            await control({"kind": "transcript", "task_id": task_id, "text": "暂停当前拍摄任务"})
            await control({"kind": "stage", "stage": "S2"})
            await edge_wait(paused=True)
            evidence["checks"]["paused_when_scene_allowed"] = True
            await control({"kind": "resume", "task_id": task_id})
            print("S2 resumed; waiting for real capture and render", flush=True)

            async def finished():
                task = await get(f"/tasks/{task_id}")
                for snapshot in await get(f"/tasks/{task_id}/edge"):
                    check_edge_failure(snapshot.get("task", {}))
                if task["status"] in {
                    "failed",
                    "unsupported",
                    "expired",
                    "cancelled",
                    "needs_input",
                }:
                    if not any(e["status"] == "queued" for e in task["events"]):
                        raise RuntimeError(f"task ended as {task['status']}")
                return task if task["status"] == "completed" else None

            task = await until(finished)
            artifacts = [a for a in task["assets"] if a["kind"] == "deliverable"]
            if len(artifacts) != 1:
                raise RuntimeError("missing unique deliverable")
            response = await client.get(f"/assets/{artifacts[0]['id']}")
            response.raise_for_status()
            if (
                hashlib.sha256(response.content).hexdigest()
                != artifacts[0]["info"]["asset"]["sha256"]
            ):
                raise RuntimeError("downloaded video checksum mismatch")
            evidence["checks"]["downloaded_video_verified"] = True
            evidence["status"] = "passed"
        except Exception as exc:
            evidence["status"] = "failed"
            evidence["failure"] = (
                type(exc).__name__
                + ": "
                + (str(exc) if not isinstance(exc, httpx.HTTPError) else "HTTP request failed")
            )
            raise
        finally:
            evidence["finished_at"] = time.time()
            try:
                if task_id:
                    evidence["cloud_task"] = await get(f"/tasks/{task_id}")
                    evidence["edge_tasks"] = await get(f"/tasks/{task_id}/edge")
                cursor = 0
                while True:
                    items = await get(f"/api/timeline?after={cursor}")
                    evidence["timeline"].extend(
                        e
                        for e in items
                        if e["created"] >= started and (e["task_id"] in {None, task_id})
                    )
                    if len(items) < 500:
                        break
                    cursor = items[-1]["seq"]
            finally:
                write_report(folder, evidence)
                print(f"{evidence['status']}: {folder / 'report.md'}", flush=True)
