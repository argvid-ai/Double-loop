"""Loopback authenticated edge API; the Mac proxies user input without model reasoning."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import Field

from .contracts import ID, Contract, UserMessage
from .inputs import InputEvent, mock_stage, mock_transcript
from .interaction_service import ConversationService
from .media import file_hash
from .store import Conflict


class NewConversation(Contract):
    id: str | None = Field(default=None, pattern=ID)


class ControlInput(Contract):
    event_id: str = Field(pattern=ID)
    action: str


class StageInput(Contract):
    stage: str


class MockTranscriptInput(UserMessage):
    conversation_id: str = Field(pattern=ID)


def create_interaction_edge_app(profile, root, token, *, runtime_factory=None):
    options = {} if runtime_factory is None else {"runtime_factory": runtime_factory}
    host = ConversationService(profile, root, token, **options)

    @asynccontextmanager
    async def lifespan(app):
        await host.start()
        try:
            yield
        finally:
            await host.close()

    app = FastAPI(title="O6N 端侧会话实验", lifespan=lifespan)
    app.state.host = host

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        allowed = {
            f"http://127.0.0.1:{profile.edge_port}",
            f"http://localhost:{profile.edge_port}",
            f"http://127.0.0.1:{profile.cloud_port}",
            f"http://localhost:{profile.cloud_port}",
        }
        if origin and origin not in allowed:
            return JSONResponse({"error": "origin rejected"}, status_code=403)
        return await call_next(request)

    async def authenticate(request: Request):
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            raise HTTPException(401, "node authentication required")

    secured = [Depends(authenticate)]

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"error": "conversation or artifact not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"error": "invalid input"}, status_code=422)

    @app.get("/health")
    def health():
        workers = host.worker_health()
        body = {
            "side": "edge",
            "hostname": platform.node(),
            "machine": platform.machine(),
            "pid": os.getpid(),
            "model": profile.edge.model,
            "context_tokens": profile.edge.context_tokens,
            "profile_digest": profile.fingerprint,
            "interaction_owner": "edge_nanobot",
            "simulated_inputs": True,
            "workers": workers,
        }
        return JSONResponse(
            body,
            status_code=200
            if len(workers) == 3 and all(v == "running" for v in workers.values())
            else 503,
        )

    @app.get("/conversations", dependencies=secured)
    def conversations():
        return host.store.conversations()

    @app.post("/conversations", dependencies=secured, status_code=201)
    def create(body: NewConversation):
        return host.store.create_conversation(body.id)

    @app.get("/conversations/{cid}", dependencies=secured)
    def state(cid: str):
        return host.projection(cid, rich=True)

    @app.post("/conversations/{cid}/messages", dependencies=secured, status_code=202)
    async def message(cid: str, body: UserMessage):
        return host.submit(cid, body)

    @app.post("/conversations/{cid}/controls", dependencies=secured)
    async def control(cid: str, body: ControlInput):
        return host.control(cid, body.action, body.event_id)

    @app.get("/conversations/{cid}/inputs", dependencies=secured)
    def inputs(cid: str):
        return host.store.inputs(cid)

    @app.get("/conversations/{cid}/trace", dependencies=secured)
    def trace(cid: str, after: int = 0):
        host.store.conversation(cid)
        return host.store.timeline(cid, after=max(0, after))

    @app.get("/conversations/{cid}/events", dependencies=secured)
    async def events(cid: str, request: Request, after: int = 0):
        host.store.conversation(cid)

        async def generate():
            cursor = max(after, 0)
            while not await request.is_disconnected():
                rows = host.store.timeline(cid, after=cursor)
                for item in rows:
                    cursor = item["seq"]
                    if (
                        item["kind"].startswith(("conversation.", "intent.", "capture.", "cloud."))
                        or item["kind"] == "tool.started"
                    ):
                        yield f"id: {cursor}\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                if not rows:
                    yield ": waiting\n\n"
                    await asyncio.sleep(0.1)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    def scoped_asset(cid, ident, intent_version=None):
        intent = host.store.conversation(cid)["intent"]
        asset = host.store.asset(ident)
        owner_version = asset["info"].get("intent_version")
        if asset["task_id"] != cid:
            task = host.store.task(asset["task_id"])
            if task["context"].get("conversation_id") != cid:
                raise Conflict("artifact belongs to another conversation")
            owner_version = task["context"].get("intent_version")
        selected = (
            host.store.action("capture:" + intent["capture_task"])
            if intent["capture_task"]
            else None
        )
        selected_here = bool(
            selected
            and selected["asset"]["asset_id"] == ident
            and selected["subject_id"] == intent["subject_id"]
        )
        if intent_version is not None and owner_version != intent_version and not selected_here:
            raise Conflict("artifact belongs to another intent version")
        path = Path(asset["path"]).resolve()
        if not path.is_relative_to((host.store.root / "assets").resolve()):
            raise Conflict("artifact path outside owned storage")
        return asset, path

    @app.get("/conversations/{cid}/assets", dependencies=secured)
    def assets(cid: str):
        intent = host.store.conversation(cid)["intent"]
        current = intent["version"]
        selected = (
            host.store.action("capture:" + intent["capture_task"])
            if intent["capture_task"]
            else None
        )
        with host.store.connect() as db:
            rows = db.execute(
                """SELECT a.*,t.context AS owner_context FROM assets a LEFT JOIN tasks t ON t.id=a.task_id
                               WHERE a.task_id=? OR json_extract(t.context,'$.conversation_id')=?""",
                (cid, cid),
            ).fetchall()
        values = []
        for row in rows:
            info = json.loads(row["info"])
            version = (
                json.loads(row["owner_context"]).get("intent_version")
                if row["owner_context"]
                else info.get("intent_version")
            )
            reused = bool(selected and selected["asset"]["asset_id"] == row["id"])
            values.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "info": info,
                    "intent_version": version,
                    "current": version == current or reused,
                    "selected_for_intent_version": current if reused else version,
                }
            )
        return values

    @app.get("/conversations/{cid}/assets/{ident}/metadata", dependencies=secured)
    def asset_metadata(cid: str, ident: str, intent_version: int | None = None):
        if (
            intent_version is not None
            and host.store.conversation(cid)["intent"]["version"] != intent_version
        ):
            raise Conflict("intent version changed")
        asset, _ = scoped_asset(cid, ident, intent_version)
        return asset["info"]

    @app.get("/conversations/{cid}/assets/{ident}", dependencies=secured)
    async def asset_file(cid: str, ident: str, intent_version: int | None = None):
        if (
            intent_version is not None
            and host.store.conversation(cid)["intent"]["version"] != intent_version
        ):
            raise Conflict("intent version changed")
        asset, path = scoped_asset(cid, ident, intent_version)
        if await asyncio.to_thread(file_hash, path) != asset["info"]["asset"]["sha256"]:
            raise Conflict("artifact bytes changed")
        return FileResponse(path, media_type="video/mp4", filename=ident + ".mp4")

    @app.post("/inputs", dependencies=secured)
    async def input_event(body: InputEvent):
        return host.ingest(body)

    @app.post("/simulation/transcript", dependencies=secured)
    async def transcript(body: MockTranscriptInput):
        host.store.conversation(body.conversation_id)
        with host.store.connect() as db:
            old = db.execute(
                "SELECT body FROM input_events WHERE id=?", (body.event_id,)
            ).fetchone()
            head = db.execute(
                "SELECT sequence FROM input_heads WHERE provider='mock-transcript'"
            ).fetchone()
        if old:
            event = InputEvent.model_validate_json(old["body"])
            if (
                event.task_id != body.conversation_id
                or event.data.text != body.text
                or event.provider != "mock-transcript"
            ):
                raise Conflict("mock transcript identity conflict")
        else:
            event = mock_transcript(body.conversation_id, body.text, (head[0] if head else 0) + 1)
            event.event_id = body.event_id
        return host.ingest(event)

    @app.post("/simulation/stage", dependencies=secured)
    async def stage(body: StageInput):
        if body.stage not in {"S0", "S1", "S2"}:
            raise ValueError("unknown stage")
        with host.store.connect() as db:
            row = db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM input_heads WHERE provider IN ('mock-scene','mock-robot')"
            ).fetchone()
        events = mock_stage(body.stage, row[0] + 1, viewpoint=host.store.scene().viewpoint)
        results = host.inputs.ingest_many(events)
        for c in host.store.conversations():
            intent = c["intent"]
            if intent["kind"] and intent["status"] in {"ready", "waiting"} and not intent["paused"]:
                host.store.enqueue_event(
                    c["id"],
                    "scene:" + events[-1].event_id + ":" + c["id"],
                    "scene.changed",
                    {},
                    intent_version=intent["version"],
                )
        host.input_wake.set()
        return {
            "results": results,
            "scene": host.store.scene().model_dump(mode="json"),
            "simulated": True,
        }

    @app.get("/capabilities", dependencies=secured)
    def capabilities():
        return {
            "binding": host.binding,
            "inputs": "cix.input/1.0",
            "harness": "nanobot-ai==0.3.0",
            "context_mode": profile.interaction.context_mode,
            "model_window": profile.edge.context_tokens,
            "native_auto_compaction": False,
            "final_request_budget": True,
            "simulated": True,
        }

    return app
