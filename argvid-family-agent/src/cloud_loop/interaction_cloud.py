"""Cloud NanoBot renders delegated media; browser traffic is forwarded to the edge."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import Field

from .contracts import ID, CapturePackage, Contract, canonical
from .dual_app import create_cloud_app
from .interaction_summary import SummaryInput, request_summary
from .media import check_clip, file_hash
from .store import Conflict


class DelegatedComposition(Contract):
    id: str = Field(pattern=ID)
    conversation_id: str = Field(pattern=ID)
    intent_version: int = Field(ge=1)
    goal: str = Field(min_length=1, max_length=1000)
    subject_id: str = Field(pattern=ID)
    duration_ms: Literal[12000]
    orientation: Literal["portrait"]
    capture_asset_id: str = Field(pattern=ID)
    capture_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    expires_at: float = Field(gt=0)
    simulated: Literal[True] = True


def create_interaction_cloud_app(profile, root, token):
    if profile.interaction is None:
        raise ValueError("interactive profile required")
    if "remote-capture" in profile.cloud.plugins:
        raise ValueError(
            "edge-owned composition must not silently delegate capture back to a legacy edge loop"
        )
    app = create_cloud_app(profile, root, token)
    host = app.state.host
    if host.profile.fingerprint != profile.fingerprint:
        raise Conflict("persisted cloud profile differs from the requested interaction profile")
    edge = httpx.AsyncClient(
        base_url=profile.interaction.edge_proxy_url,
        headers={"Authorization": "Bearer " + token},
        timeout=20,
        trust_env=False,
    )
    lock = asyncio.Lock()
    with host.store.connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS interaction_jobs (
            id TEXT PRIMARY KEY, body TEXT NOT NULL, task_id TEXT, created REAL NOT NULL,
            cancelled INTEGER NOT NULL DEFAULT 0
        )""")
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original_lifespan(application):
            try:
                yield
            finally:
                await edge.aclose()

    app.router.lifespan_context = lifespan

    async def authenticate(request: Request):
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            raise HTTPException(401, "node authentication required")

    secured = [Depends(authenticate)]

    @app.post("/interaction/summary", dependencies=secured)
    async def summarize(body: SummaryInput):
        if profile.interaction.summary_provider != "cloud":
            raise Conflict("cloud summaries are not enabled by this profile")
        # UTF-8 bytes are a deliberately conservative upper bound for this
        # text-only byte-tokenized provider; reserve policy/output overhead.
        if len(body.history_json.encode()) > min(48000, profile.cloud.context_tokens - 4096):
            raise ValueError("history exceeds bounded cloud summary input")
        key = (
            Path(profile.cloud.model_key_file or profile.model_key_file)
            .expanduser()
            .read_text()
            .strip()
        )
        base = profile.cloud.model_base_url or profile.model_base_url
        async with httpx.AsyncClient(
            base_url=base,
            headers={"Authorization": "Bearer " + key},
            timeout=profile.cloud.request_timeout,
            trust_env=False,
        ) as client:
            result = await request_summary(client, profile.cloud.model, body.history_json)
        host.store.audit(
            None,
            "interaction.summary.completed",
            {
                **{k: v for k, v in result.items() if k != "text"},
                "conversation_id": body.conversation_id,
                "revision": body.revision,
                "covered_input_seq": body.covered_input_seq,
            },
        )
        return result

    def job(ident):
        with host.store.connect() as db:
            row = db.execute("SELECT * FROM interaction_jobs WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        return {**dict(row), "body": json.loads(row["body"])}

    @app.post("/interaction/delegations", dependencies=secured, status_code=202)
    async def delegate(body: DelegatedComposition):
        if body.profile_digest != host.profile.fingerprint:
            raise Conflict("delegation profile mismatch")
        if (
            body.expires_at <= time.time()
            or body.expires_at > time.time() + profile.max_task_seconds + 30
        ):
            raise Conflict("invalid delegation deadline")
        encoded = canonical(body)
        async with lock:
            with host.store.connect() as db:
                old = db.execute("SELECT * FROM interaction_jobs WHERE id=?", (body.id,)).fetchone()
                if old:
                    if old["body"] != encoded:
                        raise Conflict("delegation identity conflict")
                    if old["cancelled"]:
                        raise Conflict("delegation was cancelled")
                    if old["task_id"]:
                        return {
                            "id": body.id,
                            "accepted": True,
                            "duplicate": True,
                            "task_id": old["task_id"],
                        }
                else:
                    db.execute(
                        "INSERT INTO interaction_jobs(id,body,task_id,created) VALUES (?,?,NULL,?)",
                        (body.id, encoded, time.time()),
                    )
            route = f"/conversations/{body.conversation_id}/assets/{body.capture_asset_id}"
            params = {"intent_version": body.intent_version}
            metadata = await edge.get(route + "/metadata", params=params)
            metadata.raise_for_status()
            package = CapturePackage.model_validate(metadata.json())
            if (
                package.subject_id != body.subject_id
                or package.asset.asset_id != body.capture_asset_id
                or package.asset.sha256 != body.capture_sha256
            ):
                raise Conflict("delegated capture binding mismatch")
            folder = host.store.root / "imports" / body.id
            folder.mkdir(parents=True, exist_ok=True)
            temp = folder / "closeup.partial.mp4"
            path = folder / "closeup.mp4"
            async with edge.stream("GET", route, params=params) as response:
                response.raise_for_status()
                count = 0
                with temp.open("wb") as out:
                    async for chunk in response.aiter_bytes():
                        count += len(chunk)
                        if count > 32 * 1024**2:
                            raise ValueError("capture exceeds transfer limit")
                        out.write(chunk)
            facts = await asyncio.to_thread(check_clip, temp, 6)
            if facts["sha256"] != body.capture_sha256:
                raise Conflict("capture transfer checksum mismatch")
            temp.replace(path)
            task_id = uuid.uuid4().hex
            asset_id = "closeup-" + body.id
            info = package.model_dump(mode="json")
            info["transport"] = {
                "origin_asset_id": body.capture_asset_id,
                "conversation_id": body.conversation_id,
                "intent_version": body.intent_version,
            }
            info["asset"]["asset_id"] = asset_id
            info["asset"]["uri"] = "cloud-asset://" + asset_id
            context = {
                "user_context": {"subject_id": body.subject_id, "delegation_id": body.id},
                "mode": "deferred",
                "profile_digest": profile.fingerprint,
                "expires": body.expires_at,
                "simulated": True,
            }
            context["user_context"]["composition"] = {
                "duration_ms": 12000,
                "orientation": "portrait",
                "timeline": ["wide", "closeup"],
                "capture_phase": "already_completed",
                "note": "拍摄先后是工作步骤；本演示成片顺序固定为六秒全景、六秒近景。",
            }
            # Queue the model only after its imported material is committed.
            with host.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute(
                    "SELECT cancelled FROM interaction_jobs WHERE id=?", (body.id,)
                ).fetchone()[0]:
                    raise Conflict("delegation cancelled during transfer")
                db.execute(
                    "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                    (task_id, body.goal, canonical(context), "queued", None, None, time.time()),
                )
                db.execute(
                    "INSERT INTO assets VALUES (?,?,?,?,?)",
                    (asset_id, task_id, str(path.resolve()), canonical(info), "closeup"),
                )
                db.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,'queued',NULL,?)",
                    (uuid.uuid4().hex, task_id, "user", "{}", time.time(), time.time()),
                )
                db.execute("UPDATE interaction_jobs SET task_id=? WHERE id=?", (task_id, body.id))
                host.store._audit(
                    db,
                    task_id,
                    "delegation.imported",
                    {
                        "job_id": body.id,
                        "asset_id": asset_id,
                        "sha256": facts["sha256"],
                        "source": "edge_conversation",
                    },
                )
            return {"id": body.id, "accepted": True, "duplicate": False, "task_id": task_id}

    @app.get("/interaction/delegations/{ident}", dependencies=secured)
    def status(ident: str):
        item = job(ident)
        body = item["body"]
        if item["cancelled"]:
            return {
                "id": ident,
                "intent_version": body["intent_version"],
                "status": "failed",
                "reason": "cancelled",
            }
        if not item["task_id"]:
            return {"id": ident, "intent_version": body["intent_version"], "status": "accepted"}
        task = host.store.task(item["task_id"])
        state = (
            "completed"
            if task["status"] == "completed"
            else "failed"
            if task["status"] in {"failed", "unsupported", "cancelled", "expired", "needs_input"}
            else "accepted"
            if task["status"] == "queued"
            else "running"
        )
        result = {
            "id": ident,
            "intent_version": body["intent_version"],
            "task_id": task["id"],
            "status": state,
        }
        if state == "completed":
            result["asset"] = host.store.asset("story-" + task["id"])["info"]["asset"]
        return result

    @app.get("/interaction/delegations/{ident}/asset", dependencies=secured)
    async def artifact(ident: str):
        item = job(ident)
        if not item["task_id"] or host.store.task(item["task_id"])["status"] != "completed":
            raise Conflict("artifact not complete")
        asset = host.store.asset("story-" + item["task_id"])
        path = Path(asset["path"])
        if await asyncio.to_thread(file_hash, path) != asset["info"]["asset"]["sha256"]:
            raise Conflict("artifact changed")
        return FileResponse(path, media_type="video/mp4")

    @app.post("/interaction/delegations/{ident}/cancel", dependencies=secured)
    def cancel(ident: str):
        with host.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM interaction_jobs WHERE id=?", (ident,)).fetchone()
            if row is None:
                raise KeyError(ident)
            item = dict(row)
            if item["task_id"]:
                task_status = db.execute(
                    "SELECT status FROM tasks WHERE id=?", (item["task_id"],)
                ).fetchone()[0]
                if task_status in {"completed", "unsupported", "expired"}:
                    return {"id": ident, "cancelled": False, "status": task_status}
            db.execute("UPDATE interaction_jobs SET cancelled=1 WHERE id=?", (ident,))
        if item["task_id"]:
            task = host.store.task(item["task_id"])
            if task["status"] not in {"completed", "cancelled", "unsupported", "expired"}:
                host.store.cancel(task["id"])
            status = host.store.task(item["task_id"])["status"]
            return {"id": ident, "cancelled": status == "cancelled", "status": status}
        return {"id": ident, "cancelled": True, "status": "cancelled_before_queue"}

    # Fixed path families only; the browser never supplies a destination URL or key.
    @app.api_route("/interaction/edge/{path:path}", methods=["GET", "POST"])
    async def proxy(path: str, request: Request):
        allowed = path in {
            "health",
            "conversations",
            "inputs",
            "simulation/stage",
            "simulation/transcript",
            "capabilities",
        } or path.startswith("conversations/")
        if not allowed or ".." in path or "://" in path:
            raise HTTPException(404, "unknown edge route")
        body = await request.body()
        if len(body) > 65536:
            raise HTTPException(413, "request too large")
        req = edge.build_request(
            request.method,
            "/" + path,
            params=request.query_params,
            content=body,
            headers={"Content-Type": request.headers.get("content-type", "application/json")},
        )
        try:
            response = await edge.send(req, stream=True)
        except httpx.HTTPError:
            return JSONResponse({"error": "edge_unavailable"}, status_code=503)
        content_type = response.headers.get("content-type", "application/octet-stream")

        async def stream():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()

        return StreamingResponse(
            stream(),
            status_code=response.status_code,
            media_type=content_type,
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # The old cloud task UI remains available on its original deployment.
    app.router.routes = [
        route for route in app.router.routes if getattr(route, "path", None) != "/"
    ]

    @app.get("/")
    def page():
        return FileResponse(Path(__file__).parent / "web" / "interaction.html")

    return app
