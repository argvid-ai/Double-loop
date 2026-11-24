"""Cloud application for the dual demo; serves API and one local control surface."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .contracts import (
    TOOL_CONTRACTS,
    Completion,
    DeliverableManifest,
    FirstResult,
    Hello,
    Profile,
    TaskInput,
    Upload,
    canonical,
    tool_schema,
)
from .dual_runtime import DualNanobotAgent
from .dual_store import TERMINAL, DualStore
from .gateway import Gateway
from .inputs import MockControlRequest
from .media import (
    SOURCE_BOUND_RENDER,
    check_clip,
    file_hash,
    inspect_video,
    media_facts,
    processor_versions,
    render,
)
from .plugins import CATALOG, require_profile, validate_profile
from .service import Service
from .store import Conflict


class CloudHost:
    def __init__(self, profile, root, token):
        self.profile, self.root, self.token = profile, root, token
        self.store = DualStore(root / "cloud", "cloud")
        active_digest = self.store.meta("active_profile_digest")
        if active_digest:
            restored = Profile.model_validate_json(
                (root / "profiles" / f"{active_digest}.json").read_text()
            )
            if restored.fingerprint != active_digest:
                raise ValueError("stored profile checksum mismatch")
            require_profile(restored)
            self.profile = profile = restored
        self.gateway = Gateway(self.store, profile)
        self.agent = DualNanobotAgent(self.store, profile, self.execute, self.context)
        self.service = Service(self.store.root, self.agent, store=self.store)

    def context(self, task_id):
        task = self.store.task(task_id)
        return {
            "goal": task["prompt"],
            "status": task["status"],
            "user_context": task["context"]["user_context"],
            "completion_mode": task["context"]["mode"],
            "edge_tasks": task["edge_tasks"],
            "assets": self.store.assets(task_id),
            "node_online": self.gateway.node()["online"],
            "simulated": True,
        }

    async def execute(self, task_id, name, args, call_id):
        self.store.assert_active(task_id)
        if name == "get_task_context":
            return self.context(task_id)
        if name == "media_list":
            return {"assets": self.store.assets(task_id)}
        if name == "edge_capture_task":
            return await self.gateway.invoke(task_id, args, call_id)
        if name == "media_inspect":
            asset = self.store.asset(args["asset_id"])
            if asset["task_id"] not in {task_id, None}:
                raise Conflict("asset belongs to another task")
            return await asyncio.to_thread(inspect_video, Path(asset["path"]))
        if name == "media_render":
            assets = [self.store.asset(ident) for ident in args["asset_ids"]]
            if [a["kind"] for a in assets] != ["wide", "closeup"] or assets[1][
                "task_id"
            ] != task_id:
                raise Conflict("render requires this task's wide then verified close-up")
            subject_id = self.store.task(task_id)["context"]["user_context"].get(
                "subject_id", "pet-cat-mimi"
            )
            if any(a["info"].get("subject_id") != subject_id for a in assets):
                raise Conflict("source assets do not match the task subject")
            for a in assets:
                if (
                    a["task_id"] not in {None, task_id}
                    or file_hash(Path(a["path"])) != a["info"]["asset"]["sha256"]
                ):
                    raise Conflict("asset ownership or checksum mismatch")
            asset_id = f"story-{task_id}"
            path = self.store.root / "assets" / f"{asset_id}.mp4"
            # Reuse belongs at the committed-receipt boundary, never inside
            # the encoder based only on the presence/shape of a loose file.
            try:
                previous = self.store.asset(asset_id)
            except KeyError:
                previous = None
            if previous is not None:
                try:
                    saved = DeliverableManifest.model_validate(previous["info"])
                except ValidationError:
                    saved = None
                expected = [(a["id"], a["info"]["asset"]["sha256"]) for a in assets]
                if (
                    saved is not None
                    and previous["task_id"] == task_id
                    and saved.task_id == task_id
                    and saved.asset.asset_id == asset_id
                    and saved.provenance.get("materialization") == SOURCE_BOUND_RENDER
                    and [(v.get("asset_id"), v.get("sha256")) for v in saved.timeline] == expected
                    and Path(previous["path"]).is_file()
                    and await asyncio.to_thread(file_hash, Path(previous["path"]))
                    == saved.asset.sha256
                ):
                    self.store.assert_active(task_id)
                    self.store.audit(
                        task_id,
                        "media.render.reused",
                        {
                            "asset_id": asset_id,
                            "sha256": saved.asset.sha256,
                            "source_binding_verified": True,
                        },
                    )
                    return previous["info"]
                # Registered assets are immutable. A conflicting/legacy receipt
                # must not be repaired by changing bytes under its existing ID.
                raise Conflict(
                    "registered deliverable cannot be reused for these verified inputs; preserve it and create a new task"
                )
            await asyncio.to_thread(render, Path(assets[0]["path"]), Path(assets[1]["path"]), path)
            self.store.assert_active(task_id)
            facts = await asyncio.to_thread(media_facts, path, asset_id, "cloud")
            manifest = DeliverableManifest(
                task_id=task_id,
                asset=facts,
                timeline=[
                    {
                        "asset_id": a["id"],
                        "sha256": a["info"]["asset"]["sha256"],
                        "start_ms": i * 6000,
                        "duration_ms": 6000,
                        "source_selection": a["info"].get(
                            "selection", a["info"].get("provenance", {}).get("selection")
                        ),
                    }
                    for i, a in enumerate(assets)
                ],
                provenance={
                    **assets[0]["info"]["provenance"],
                    "processors": processor_versions(),
                    "materialization": SOURCE_BOUND_RENDER,
                },
                qc={
                    "passed": True,
                    "technical": "360 frames fully decoded; 12s portrait; no audio",
                    "capture_mode": "replay",
                },
            )
            body = manifest.model_dump(mode="json")
            self.store.put_asset(asset_id, task_id, path, body, "deliverable")
            path.with_suffix(".json").write_text(json.dumps(body, ensure_ascii=False, indent=2))
            license_info = body["provenance"]
            path.with_suffix(".attribution.txt").write_text(
                f"{license_info['author']} — Cat playing with a round track toy\n{license_info['source_page']}\n"
                f"{license_info['license']} — {license_info['license_url']}\n"
                "Changes: two excerpts, crop/pad, resize, mute, watermark, concatenation, encode.\nNot newly captured footage.\n"
            )
            self.store.audit(task_id, "media.rendered", body)
            return body
        if name == "publish_result":
            task = self.store.task(task_id)
            if any(e["status"] in {"sent", "accepted"} for e in task["edge_tasks"]):
                raise Conflict("remote work is pending; finish this turn and wait")
            if args["status"] == "completed":
                asset = self.store.asset(f"story-{task_id}")
                facts = await asyncio.to_thread(check_clip, Path(asset["path"]), 12)
                if facts["sha256"] != asset["info"]["asset"]["sha256"]:
                    raise Conflict("deliverable checksum mismatch")
            self.store.set_outcome(task_id, args["status"], args["summary"])
            return {**args, "simulated": True}
        raise ValueError("unknown cloud tool")

    async def start(self):
        fixture = self.store.root / "fixtures" / "wide.mp4"
        if fixture.exists():
            info = json.loads(fixture.with_suffix(".json").read_text())
            if file_hash(fixture) != info["asset"]["sha256"]:
                raise ValueError("prepared wide fixture was changed")
            self.store.put_asset(info["asset"]["asset_id"], None, fixture, info, "wide")
        await self.service.start()

    def update_profile(self, incoming: Profile):
        if self.profile.interaction is not None:
            raise Conflict(
                "interactive deployment changes require an idle restart with a reviewed profile"
            )
        if any(t["status"] not in TERMINAL for t in self.store.tasks()):
            raise Conflict("finish or cancel existing tasks before changing profile")
        immutable = {
            "data_root",
            "cloud_port",
            "edge_port",
            "node_id",
            "model_base_url",
            "model_key_file",
            "gateway_url",
            "interaction",
        }
        for key in immutable:
            if getattr(incoming, key) != getattr(self.profile, key):
                raise Conflict(
                    f"{key} is local administrator configuration, not a web-editable field"
                )
        for side in ("cloud", "edge"):
            old, new = getattr(self.profile, side), getattr(incoming, side)
            for key in (
                "model_base_url",
                "model_key_file",
                "request_timeout",
                "run_timeout",
                "context_tokens",
                "output_tokens",
            ):
                if getattr(old, key) != getattr(new, key):
                    raise Conflict("side deployment settings are administrator-only")
        require_profile(incoming)
        if incoming.id != self.profile.id or incoming.revision <= self.profile.revision:
            raise Conflict("profile must keep its id and advance its revision")
        folder = self.root / "profiles"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{incoming.fingerprint}.json").write_text(incoming.model_dump_json(indent=2))
        self.store.set_meta("active_profile_digest", incoming.fingerprint)
        self.profile = incoming
        self.gateway.profile = incoming
        self.agent = DualNanobotAgent(self.store, incoming, self.execute, self.context)
        self.service.agent = self.agent
        self.store.audit(
            None,
            "profile.changed",
            {"revision": incoming.revision, "profile_digest": incoming.fingerprint},
        )


def create_cloud_app(profile: Profile, root: Path, token: str):
    host = CloudHost(profile, root, token)

    @asynccontextmanager
    async def lifespan(app):
        await host.start()
        try:
            yield
        finally:
            await host.service.close()

    app = FastAPI(title="端云工作台 · 双 Agent", version="0.2.0", lifespan=lifespan)
    app.state.host = host

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin not in {
            f"http://127.0.0.1:{profile.cloud_port}",
            f"http://localhost:{profile.cloud_port}",
        }:
            return JSONResponse({"error": "origin rejected"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"error": "task or asset not found"}, status_code=404)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"error": "invalid data"}, status_code=422)

    async def authenticate(request: Request):
        received = request.headers.get("Authorization", "")
        if not secrets.compare_digest(received, f"Bearer {host.token}"):
            raise HTTPException(401, "node authentication required")

    secured = [Depends(authenticate)]

    @app.get("/health")
    def health():
        return {
            "side": "cloud",
            "pid": os.getpid(),
            "database": str(host.store.path),
            "hostname": __import__("platform").node(),
            "machine": __import__("platform").machine(),
            "model": host.profile.cloud.model,
            "profile_digest": host.profile.fingerprint,
        }

    @app.get("/api/timeline")
    def timeline(after: int = 0):
        return host.store.timeline(after=after)

    @app.get("/api/plugins")
    def plugins():
        return {
            "catalog": [p.model_dump(mode="json") for p in CATALOG],
            "bindings": require_profile(host.profile),
            "tool_schemas": {n: tool_schema(t[0]) for n, t in TOOL_CONTRACTS.items()},
            "actual_runs": host.store.timeline(after=0)[-20:],
        }

    @app.get("/api/profiles")
    def profiles():
        return {
            "active": host.profile.model_dump(mode="json"),
            "validation": validate_profile(host.profile),
        }

    @app.post("/api/profiles/{profile_id}/validate")
    def validate(profile_id: str, body: Profile):
        if profile_id != body.id:
            raise Conflict("profile identity mismatch")
        return validate_profile(body)

    @app.post("/api/profiles")
    def save_profile(body: Profile):
        host.update_profile(body)
        return {"saved": True, "profile_digest": body.fingerprint, "edge_activation_pending": True}

    @app.get("/api/nodes")
    def nodes():
        return {"nodes": [host.gateway.node()]}

    @app.get("/api/status")
    def status():
        return {
            "cloud": health(),
            "node": host.gateway.node(),
            "profile": host.profile.id,
            "revision": host.profile.revision,
            "edge_port": host.profile.edge_port,
            "scene": host.store.meta("edge_scene"),
            "simulated": True,
            "scoring": "not_installed",
            "prepared": (host.store.root / "fixtures" / "wide.mp4").exists(),
        }

    @app.get("/api/scenario")
    def scenario():
        return json.loads(
            (Path(__file__).resolve().parents[2] / "examples" / "cat-replay.json").read_text()
        )

    @app.post("/api/edge/controls", status_code=202)
    def edge_control(body: MockControlRequest):
        return host.gateway.queue_control(body)

    @app.get("/api/edge/controls")
    def edge_controls():
        return host.gateway.controls()

    @app.post("/tasks", status_code=202)
    def submit(body: TaskInput):
        node = host.gateway.node()
        if node.get("profile_digest") != host.profile.fingerprint:
            raise Conflict("waiting for edge to activate this profile")
        return host.store.new_task(body, host.profile.fingerprint, host.profile.max_task_seconds)

    @app.get("/tasks")
    def tasks():
        return host.store.tasks()

    @app.get("/tasks/{task_id}")
    def task(task_id: str):
        value = host.store.task(task_id)
        value["available_assets"] = host.store.assets(task_id)
        snapshot_path = host.store.root / "tasks" / task_id / "profile.json"
        if snapshot_path.exists():
            snapshot = Profile.model_validate_json(snapshot_path.read_text())
            value["models"] = {"cloud": snapshot.cloud.model, "edge": snapshot.edge.model}
            value["profile_revision"] = snapshot.revision
        return value

    @app.get("/tasks/{task_id}/edge")
    def edge_tasks(task_id: str):
        task = host.store.task(task_id)
        return [
            host.store.meta(f"edge_task:{t['id']}", {"task_id": t["id"], "pending": True})
            for t in task["edge_tasks"]
        ]

    @app.post("/tasks/{task_id}/cancel")
    def cancel(task_id: str):
        host.store.cancel(task_id)
        return {"status": host.store.task(task_id)["status"]}

    @app.post("/events/{event_id}/retry")
    def retry(event_id: str):
        host.store.retry_event(event_id, host.profile.fingerprint)
        return {"status": "queued"}

    @app.get("/tasks/{task_id}/events")
    async def task_events(task_id: str, request: Request, after: int = 0):
        host.store.task(task_id)
        if request.headers.get("accept") != "text/event-stream":
            return host.store.timeline(task_id, after)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(422, "invalid event cursor") from None

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                for event in host.store.timeline(task_id, cursor):
                    cursor = event["seq"]
                    yield f"id: {cursor}\ndata: {canonical(event)}\n\n"
                yield ": heartbeat\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/assets/{asset_id}")
    def asset(asset_id: str, format: str = "video"):
        record = host.store.asset(asset_id)
        if format == "manifest":
            return record["info"]
        if format != "video":
            raise HTTPException(422, "unknown asset format")
        return FileResponse(record["path"], media_type="video/mp4")

    @app.post("/gateway/hello", dependencies=secured)
    def hello(body: Hello):
        return host.gateway.hello(body)

    @app.post("/gateway/capabilities", dependencies=secured)
    def capabilities(body: Hello):
        return host.gateway.hello(body)

    @app.post("/gateway/keepalive", dependencies=secured)
    def keepalive(body: dict):
        return host.gateway.keepalive(body.get("node_id"))

    @app.get("/gateway/tasks", dependencies=secured)
    async def pending():
        deadline = time.monotonic() + 2
        while True:
            result = host.gateway.take()
            if host.gateway.node().get("profile_digest") != host.profile.fingerprint:
                result["profile"] = host.profile.model_dump(mode="json")
            if (
                result["tasks"]
                or result["cancellations"]
                or result.get("controls")
                or result.get("profile")
                or time.monotonic() >= deadline
            ):
                return result
            await asyncio.sleep(0.1)

    @app.post("/gateway/controls/result", dependencies=secured)
    def control_result(body: dict):
        return host.gateway.control_result(body["id"], body["result"])

    @app.post("/gateway/task-state", dependencies=secured)
    def task_state(body: dict):
        value = body.get("task", {})
        if not isinstance(value, dict) or len(canonical(value)) > 2_000_000:
            raise ValueError("invalid task snapshot")
        remote = host.gateway.remote(value.get("id"))
        if (
            value.get("side") != "edge"
            or value.get("context", {}).get("session_binding") != remote["binding"]
        ):
            raise Conflict("snapshot binding mismatch")
        host.store.set_meta(f"edge_task:{value['id']}", {"task": value, "received_at": time.time()})
        return {"received": True}

    @app.post("/gateway/results", dependencies=secured)
    def first(body: FirstResult):
        return host.gateway.first_result(body)

    @app.post("/gateway/events", dependencies=secured)
    def completion(body: Completion):
        return host.gateway.complete(body)

    @app.post("/gateway/assets", dependencies=secured)
    async def upload(body: Upload):
        return await asyncio.to_thread(host.gateway.upload, body)

    @app.post("/gateway/telemetry", dependencies=secured)
    def telemetry(body: dict):
        records = body.get("records", [])
        if not isinstance(records, list) or len(records) > 500:
            raise HTTPException(422, "invalid telemetry batch")
        for item in records:
            ident = f"telemetry:{profile.node_id}:{item['seq']}"
            with host.store.connect() as db:
                if db.execute("SELECT 1 FROM inbox WHERE id=?", (ident,)).fetchone():
                    continue
                db.execute("INSERT INTO inbox VALUES (?,?)", (ident, canonical(item)))
                remote = db.execute(
                    "SELECT cloud_task FROM remote WHERE id=?", (item.get("task_id"),)
                ).fetchone()
                host.store._audit(db, remote[0] if remote else None, f"edge.{item['kind']}", item)
            if item["kind"] == "scene.changed":
                host.store.set_meta("edge_scene", item["payload"])
        return {"received": len(records)}

    web = Path(__file__).parent / "web"
    if web.exists():
        app.mount("/web", StaticFiles(directory=web), name="web")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(web / "index.html")

    return app
