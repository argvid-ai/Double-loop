"""Local workbench aggregation with a narrow same-origin node proxy.

Browser clients never receive node credentials. This is a loopback single-user
UI, not an Internet-facing relay or arbitrary URL fetcher.
"""

import asyncio
import json
import re
import shutil
import time
from pathlib import Path

import anyio
import httpx
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import Field

from .browser_video import BrowserVideoCache
from .contracts import Contract
from .harness_contracts import RuleControlArgs
from .inputs import mock_stage


class SceneControl(Contract):
    stage: str = Field(pattern="^S[012]$")
    valid_for_ms: int = Field(default=60000, ge=1000, le=300000)


ALLOWED = {
    "GET": r"(input-media/(?:devices|sources/[\w.:-]+/entries|media/[\w.:-]+(?:/(?:preview|poster|info))?)|observation/(?:default|view|catalogue|markers(?:/[\w.:-]+/[\w.:-]+/frame)?|gimbals/[\w.:-]+/receipts/[\w.:-]+|media/[\w.:-]+|sources/[\w.:-]+/(?:video|preview|poster))|channels/feishu|media/storage|recordings/policy|autonomy(?:/activity(?:/[\w.:-]+)?)?|components(?:/r1)?|library|cameras|cameras/[\w-]+/frame|snapshot|capabilities|conversations|conversations/[\w.:-]+(?:/history)?|events/[\w.:-]+|trace/[\w.:-]+|tasks/[\w.:-]+|assets/[\w.:-]+|sources|sources/[\w.:-]+/(?:frame|video)|rules|reports|scene|voice/config)",
    "POST": r"(observation/(?:default|configure|control|gimbal)|components/(?:probe|apply|remove|skills/import)|cameras|cameras/[\w-]+/probe|channels/feishu/authorize/[\w.-]+|media/storage/(?:policy|collect|protect)|recordings/policy|autonomy(?:/run)?|input|conversations|sources|voice/mock|observations|scene/control|rules/control)",
    "PUT": r"(voice/(?:config|device/config)|sources/playback)",
}
ALLOWED["GET"] = "(?:" + ALLOWED["GET"] + r"|delivery|voice/(?:device|events/[\w.:-]+))"
ALLOWED["POST"] = "(?:" + ALLOWED["POST"] + r"|voice/(?:delivered|wake-preview))"
ALLOWED["PUT"] = "(?:" + ALLOWED["PUT"] + r"|delivery)"


class ProxyVideoResponse(StreamingResponse):
    """Release upstream even if disconnect occurs before body iteration starts."""

    def __init__(self, upstream):
        self.upstream = upstream
        headers = {
            k: v
            for k, v in upstream.headers.items()
            if k.lower()
            in {
                "content-type",
                "content-range",
                "accept-ranges",
                "content-length",
                "content-encoding",
            }
        }
        super().__init__(
            upstream.aiter_raw(65536), status_code=upstream.status_code, headers=headers
        )

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self.upstream.aclose()


def mount_workbench(app, host, principal, token, node_auth):
    previews = BrowserVideoCache(
        host.store.root, budget_bytes=host.settings.browser_preview_budget_bytes,
        shared_storage=getattr(host.store, "shared_media_storage", None),
        shutdown=host.stop,
    )

    async def local_preview(file, kind):
        before = file.stat()
        if before.st_size > previews.max_input_bytes:
            raise HTTPException(413, "media exceeds browser preview input limit")
        fingerprint = (str(file), before.st_size, before.st_mtime_ns, before.st_ctime_ns)

        async def load(destination):
            await asyncio.to_thread(shutil.copyfile, file, destination)
            after = file.stat()
            if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != fingerprint[1:]:
                raise HTTPException(409, "source changed during preview preparation")

        preview = await previews.get(str(fingerprint), load, kind)
        return previews.response(preview, kind)

    async def remote_preview(target, route, headers, kind):
        # Always authorize on the peer before looking up a derived cache entry.
        probe = await host.client.send(
            host.client.build_request("GET", route, headers={**headers, "range": "bytes=0-0"}),
            stream=True,
        )
        try:
            if probe.status_code not in {200, 206}:
                raise HTTPException(probe.status_code, "registered media unavailable")
            etag = probe.headers.get("etag")
            size = (
                probe.headers.get("content-range", "").rsplit("/", 1)[-1]
                if probe.status_code == 206
                else probe.headers.get("content-length", "")
            )
            if not etag or not size.isdigit():
                raise HTTPException(409, "source has no stable preview identity")
            size = int(size)
            if size > previews.max_input_bytes:
                raise HTTPException(413, "media exceeds browser preview input limit")
        finally:
            await probe.aclose()

        async def load(destination):
            count = 0
            async with host.client.stream(
                "GET",
                route,
                headers={k: v for k, v in headers.items() if k.lower() != "range"},
                timeout=180,
            ) as response:
                if response.status_code != 200 or response.headers.get("etag") != etag:
                    raise HTTPException(409, "registered media changed; retry preview")
                with destination.open("wb") as output:
                    async for chunk in response.aiter_bytes(65536):
                        count += len(chunk)
                        if count > previews.max_input_bytes:
                            raise HTTPException(413, "media exceeds browser preview input limit")
                        output.write(chunk)
            if count != size:
                raise HTTPException(409, "incomplete source for browser preview")

        preview = await previews.get(str((target, route, etag, size)), load, kind)
        return previews.response(preview, kind)

    @app.get("/harness/snapshot")
    def snapshot():
        from .runtime_status import snapshot as runtime_snapshot
        if host.side == "cloud":
            host.store.conversations.main(principal)
        with host.store.connect() as db:
            tasks = [
                host.store._task(db, r[0], principal)
                for r in db.execute(
                    "SELECT id FROM business_tasks WHERE principal=? ORDER BY updated DESC LIMIT 100",
                    (principal,),
                )
            ]
            conversations = host.store.conversations.list(principal)
            for conversation in conversations:
                first = db.execute(
                    """SELECT kind,body FROM harness_events WHERE principal=? AND
                    (session_key=? OR json_extract(body,'$.conversation_id')=?) ORDER BY created,rowid LIMIT 1""",
                    (principal, conversation["id"], conversation["id"]),
                ).fetchone()
                conversation["entry_kind"] = (
                    "main"
                    if conversation["kind"] == "main"
                    else "task"
                    if conversation["task_id"]
                    else "user_work"
                    if first and first["kind"] in {"user", "voice"}
                    else "consultation"
                    if first
                    else "empty"
                )
                conversation["entry_text"] = (
                    json.loads(first["body"]).get("text", "")
                    if first and first["kind"] in {"user", "voice"}
                    else ""
                )
            observation_tasks = []
            if host.observations.installation:
                observation_tasks = [
                    {
                        "id": row["task_id"],
                        "goal": json.loads(row["config"])["goal"],
                        "status": row["status"],
                    }
                    for row in db.execute(
                        "SELECT task_id,config,status FROM observation_tasks WHERE principal=? ORDER BY updated DESC LIMIT 100",
                        (principal,),
                    )
                ]
        return {
            "runtime_status": runtime_snapshot(host.store, principal),
            "autonomy": dict(host.autonomy.cache.get(principal, {}))
            if getattr(host, "autonomy", None)
            else None,
            "side": host.side,
            "now": host.store.clock(),
            "capabilities": host.capabilities(),
            "observation_tasks": observation_tasks,
            "conversations": conversations,
            "tasks": tasks,
            "assets": host.effects.assets(None, principal),
            "sources": host.sources.list(principal),
            "rules": host.store.scheduler.list(principal),
            "reports": host.reports.list(principal),
            "scene": host.scene_projection() if "scene" in host.binding["capabilities"] else None,
            "workers": [{"done": w.done(), "cancelled": w.cancelled()} for w in host.workers],
        }

    @app.get("/harness/scene")
    async def scene():
        return (
            host.scene_projection()
            if "scene" in host.binding["capabilities"]
            else {"available": False}
        )

    @app.post("/harness/scene/control")
    async def scene_control(body: SceneControl, request: Request):
        await node_auth(request)
        with host.store.connect() as db:
            last = db.execute("SELECT MAX(sequence) FROM input_heads").fetchone()[0] or 0
        observations = mock_stage(
            body.stage,
            max(last + 1, time.time_ns() // 1000),
            viewpoint="front-mid",
            valid_for_ms=body.valid_for_ms,
        )
        return {
            "results": host.ingest_observations(observations),
            "simulated": True,
            "visual_inference": False,
        }

    @app.post("/harness/rules/control")
    async def rule_control(body: RuleControlArgs, request: Request):
        await node_auth(request)
        return host.store.scheduler.control(
            principal, body, source_event="web-rule-control-" + str(time.time_ns())
        )

    @app.api_route("/workbench/{target}/{path:path}", methods=["GET", "POST", "PUT"])
    async def node_proxy(target: str, path: str, request: Request):
        if target not in {"edge", "cloud"} or not re.fullmatch(ALLOWED[request.method], path):
            raise HTTPException(404, "workbench route not available")
        # Never forward arbitrary headers, destination hosts, peer RPCs or test
        # controls. Explicit UI actions reach the same validated node handlers.
        headers = {"Authorization": "Bearer " + token}
        for key in ("content-type", "range"):
            if key in request.headers:
                headers[key] = request.headers[key]
        route = "/harness/" + path
        preview_kind = request.query_params.get("preview")
        if preview_kind is not None and preview_kind not in {"video", "poster"}:
            raise HTTPException(422, "unknown browser preview format")
        if request.url.query and preview_kind is None:
            route += "?" + request.url.query
        data = await request.body()
        if len(data) > 2_000_000:
            raise HTTPException(413, "workbench request too large")
        try:
            if request.method == "GET" and path.startswith("input-media/media/"):
                if preview_kind:
                    raise HTTPException(422, "use the registered preview route")
                if target != host.side:
                    result = await host.client.send(
                        host.client.build_request("GET", route, headers=headers, timeout=200),
                        stream=True,
                    )
                    return ProxyVideoResponse(result)
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://node"
                ) as client:
                    result = await client.get(route, headers=headers, timeout=200)
                return Response(
                    result.content,
                    status_code=result.status_code,
                    headers={
                        k: v
                        for k, v in result.headers.items()
                        if k.lower() in {"content-type", "content-range", "accept-ranges"}
                    },
                )
            if (
                request.method == "GET"
                and re.fullmatch(r"observation/sources/[\w.:-]+/(?:preview|poster)", path)
                and target != host.side
            ):
                result = await host.client.send(
                    host.client.build_request("GET", route, headers=headers, timeout=200),
                    stream=True,
                )
                return ProxyVideoResponse(result)
            media = request.method == "GET" and (
                path.endswith("/video") or path.startswith(("assets/", "observation/media/"))
            )
            if preview_kind and not media:
                raise HTTPException(422, "preview is only available for registered media")
            if media and target == host.side:
                if path.startswith("observation/sources/"):
                    if host.observation_registry is None:
                        raise HTTPException(409, "observation sources are not configured")
                    file = host.observation_registry.source_path(principal, path.split("/")[2])
                    return (
                        await local_preview(file, preview_kind)
                        if preview_kind
                        else FileResponse(file)
                    )
                if path.startswith("observation/media/"):
                    installed = host.observations._installed()
                    if installed.archive is None:
                        raise HTTPException(409, "observation archive is not configured")
                    file = await __import__("asyncio").to_thread(
                        installed.archive.media_path, principal, path.split("/")[-1]
                    )
                    return (
                        await local_preview(file, preview_kind)
                        if preview_kind
                        else FileResponse(file, media_type="video/mp4")
                    )
                if path.startswith("sources/"):
                    file = Path(
                        host.sources.get(principal, path.split("/")[1], include_path=True)["path"]
                    )
                    return (
                        await local_preview(file, preview_kind)
                        if preview_kind
                        else FileResponse(file)
                    )
                asset_id = path.split("/")[-1]
                if asset_id not in {a["id"] for a in host.effects.assets(None, principal)}:
                    raise HTTPException(404, "asset not found")
                from .edge_render import ensure_local_asset
                from starlette.background import BackgroundTask

                asset = host.store.asset(asset_id)
                lease = host.media_storage.pin(asset["path"])
                lease.__enter__()
                try:
                    file = await ensure_local_asset(host, asset)
                    if preview_kind:
                        response = await local_preview(file, preview_kind)
                        lease.__exit__(None, None, None)
                        return response
                    return FileResponse(
                        file,
                        media_type="video/mp4",
                        background=BackgroundTask(lease.__exit__, None, None, None),
                    )
                except BaseException:
                    lease.__exit__(None, None, None)
                    raise
            if media:
                if preview_kind:
                    return await remote_preview(target, route, headers, preview_kind)
                result = await host.client.send(
                    host.client.build_request("GET", route, headers=headers), stream=True
                )
                return ProxyVideoResponse(result)
            if target == host.side:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://node"
                ) as client:
                    result = await client.request(
                        request.method, route, headers=headers, content=data
                    )
            else:
                result = await host.client.request(
                    request.method,
                    route,
                    headers=headers,
                    content=data,
                    # State aggregation can exceed a liveness ping's budget.
                    # Match other reads, below the browser's 35s deadline.
                    timeout=30,
                )
        except httpx.TimeoutException:
            return JSONResponse(
                {
                    "error": "状态请求超时，实际运行状态未知；页面会继续重试。",
                    "code": "peer_request_timeout",
                    "side": target,
                },
                status_code=504,
            )
        except httpx.HTTPError:
            return JSONResponse(
                {
                    "error": "暂时无法取得对侧状态，实际运行状态未知；页面会继续重试。",
                    "code": "peer_request_unavailable",
                    "side": target,
                },
                status_code=503,
            )
        forwarded = {
            k: v
            for k, v in result.headers.items()
            if k.lower()
            in {"content-type", "content-range", "accept-ranges", "x-frame-time-ms", "x-simulated"}
        }
        return Response(result.content, status_code=result.status_code, headers=forwarded)
