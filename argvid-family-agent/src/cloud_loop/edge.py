"""Independent edge host and outbound transport. The simulator cannot complete tasks."""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict

from .capture_sources import VideoFileSource
from .contracts import (
    Completion,
    CompletionResult,
    FirstResult,
    Invocation,
    Profile,
    build_hello,
    canonical,
    utcnow,
)
from .dual_runtime import DualNanobotAgent
from .dual_store import DualStore
from .inputs import InputEvent, InputIngress
from .media import check_clip
from .plugins import require_profile
from .replay import ReplayDevice
from .service import Service
from .store import Conflict


class StageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stage: str


class EdgeHost:
    def __init__(self, profile: Profile, root: Path, token: str):
        self.profile, self.token = profile, token
        self.store = DualStore(root / "edge", "edge")
        saved = self.store.meta("applied_profile")
        if saved:
            restored = Profile.model_validate(saved)
            require_profile(restored)
            self.profile = profile = restored
        self.device = ReplayDevice(
            self.store,
            VideoFileSource(self.store.root)
            if "video-file-camera" in profile.edge.plugins
            else None,
        )
        self.inputs = InputIngress(self.store)
        self.agent = DualNanobotAgent(self.store, profile, self.execute, self.context)
        self.service = Service(self.store.root, self.agent, store=self.store)
        self.stop = asyncio.Event()
        self.network_task = None

    def context(self, task_id):
        task = self.store.task(task_id)
        # The complete packages stay in the store/tool receipts. Repeating their
        # license, crop and processor manifests twice on every model step adds
        # minutes of CPU prefill without adding a new decision fact.
        assets = []
        for item in self.store.assets(task_id):
            facts = item["info"].get("asset", {})
            assets.append(
                {
                    "id": item["id"],
                    "kind": item["kind"],
                    **{
                        key: facts.get(key)
                        for key in (
                            "duration_ms",
                            "width",
                            "height",
                            "sha256",
                            "capture_mode",
                            "simulated",
                        )
                    },
                }
            )
        package = self.store.action(f"capture:{task_id}")
        package_ref = (
            {
                "package_id": package["package_id"],
                "asset_id": package["asset"]["asset_id"],
                "qc": package["qc"],
                "simulated": package["simulated"],
            }
            if package
            else None
        )
        return {
            "goal": task["prompt"],
            "status": task["status"],
            "constraints": task["context"]["invocation"]["arguments"]["constraints"],
            "scene": self.store.scene().model_dump(mode="json"),
            "assets": assets,
            "paused": task["context"].get("paused", False),
            "capture_package": package_ref,
            "simulated": True,
        }

    def accept(self, data):
        req = Invocation.model_validate(data["invocation"])
        if req.node_id != self.profile.node_id or req.expires_at <= utcnow():
            raise Conflict("node mismatch or expired invocation")
        if "capture" not in require_profile(self.profile)["edge"]["capabilities"]:
            raise Conflict("capture capability unavailable")
        binding = data["session_binding"]
        if not isinstance(binding, str) or not binding.startswith("cloud:") or len(binding) > 200:
            raise ValueError("invalid session binding")
        payload = canonical({"invocation": req.model_dump(mode="json"), "session_binding": binding})
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT body FROM inbox WHERE id=?", (req.message_id,)).fetchone()
            if existing:
                if existing[0] != payload:
                    raise Conflict("invocation message id conflict")
                return {"duplicate": True}
            if db.execute("SELECT 1 FROM tasks WHERE id=?", (req.task_id,)).fetchone():
                raise Conflict("task id conflict")
            context = {
                "invocation": req.model_dump(mode="json"),
                "session_binding": binding,
                "expires": req.expires_at.timestamp(),
                "profile_digest": self.profile.fingerprint,
                "mode": req.arguments.completion_mode,
                "simulated": True,
            }
            db.execute("INSERT INTO inbox VALUES (?,?)", (req.message_id, payload))
            db.execute(
                "INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                (
                    req.task_id,
                    req.arguments.prompt,
                    canonical(context),
                    "queued",
                    None,
                    None,
                    time.time(),
                ),
            )
            db.execute(
                "INSERT INTO events VALUES (?,?,?,?,?,'queued',NULL,?)",
                (uuid.uuid4().hex, req.task_id, "remote.start", "{}", time.time(), time.time()),
            )
            self.store._audit(db, req.task_id, "task.accepted", req.model_dump(mode="json"))
            if req.arguments.completion_mode == "deferred":
                result = FirstResult(
                    node_id=req.node_id,
                    task_id=req.task_id,
                    original_call_id=req.original_call_id,
                    status="accepted",
                )
                db.execute(
                    "INSERT INTO outbox(id,task_id,kind,body) VALUES (?,?,?,?)",
                    (f"first:{req.task_id}", req.task_id, "first", canonical(result)),
                )
        return {"duplicate": False}

    async def execute(self, task_id, name, args, call_id):
        self.store.assert_active(task_id)
        if name == "get_task_context":
            return self.context(task_id)
        if name in {"get_scene_state", "get_robot_state"}:
            return self.store.scene().model_dump(mode="json")
        if name in {"capture_workflow", "move_to_viewpoint", "camera_capture"}:
            return await self.device.execute(task_id, name, args)
        if name == "capture_qc":
            asset = self.store.asset(args["asset_id"])
            if asset["task_id"] != task_id:
                raise Conflict("asset belongs to another task")
            return await asyncio.to_thread(check_clip, Path(asset["path"]), 6)
        if name == "publish_result":
            if args["status"] == "completed":
                package = self.store.action(f"capture:{task_id}")
                if not package:
                    raise Conflict("a verified capture package is required")
                asset = self.store.asset(package["asset"]["asset_id"])
                checked = await asyncio.to_thread(check_clip, Path(asset["path"]), 6)
                if checked["sha256"] != package["asset"]["sha256"]:
                    raise Conflict("capture file changed before publication")
                task = self.store.assert_active(task_id)
                req = Invocation.model_validate(task["context"]["invocation"])
                result = CompletionResult(
                    summary="已完成6秒近景回放，文件技术检查通过；不是本次实拍。",
                    capture_package_ref=f"edge-asset://capture/{package['package_id']}.json",
                    asset_ref=package["asset"]["uri"],
                    asset_sha256=package["asset"]["sha256"],
                    qc=package["qc"],
                )
                event = Completion(
                    event_id=f"complete:{task_id}",
                    node_id=req.node_id,
                    task_id=task_id,
                    original_call_id=req.original_call_id,
                    session_binding=task["context"]["session_binding"],
                    occurred_at=package["captured_at"],
                    result=result,
                )
                self.store.queue_output(
                    task_id,
                    "completion",
                    {
                        "event": event.model_dump(mode="json"),
                        "package": package,
                        "mode": req.arguments.completion_mode,
                    },
                )
            self.store.set_outcome(task_id, args["status"], args["summary"])
            self.store.audit(task_id, "task.outcome", args)
            return {**args, "simulated": True}
        raise ValueError("unregistered edge tool")

    async def network(self):
        url = self.profile.gateway_url or f"http://127.0.0.1:{self.profile.cloud_port}"
        headers = {"Authorization": f"Bearer {self.token}"}
        async with httpx.AsyncClient(
            base_url=url, headers=headers, timeout=20, trust_env=False
        ) as client:
            registered, heartbeat_at = False, 0.0
            while not self.stop.is_set():
                try:
                    caps = require_profile(self.profile)["edge"]["capabilities"]
                    if not registered:
                        response = await client.post(
                            "/gateway/hello",
                            json=build_hello(self.profile, caps, os.getpid()).model_dump(
                                mode="json"
                            ),
                        )
                        response.raise_for_status()
                        registered, heartbeat_at = True, time.monotonic()
                        self.store.set_meta("network_error", None)
                        self.store.audit(
                            None,
                            "bridge.connected",
                            {"selected_protocol": response.json()["selected_protocol"]},
                        )
                    if time.monotonic() - heartbeat_at >= self.profile.heartbeat_seconds:
                        response = await client.post(
                            "/gateway/keepalive", json={"node_id": self.profile.node_id}
                        )
                        response.raise_for_status()
                        heartbeat_at = time.monotonic()
                    response = await client.get("/gateway/tasks")
                    response.raise_for_status()
                    data = response.json()
                    for item in data["tasks"]:
                        self.accept(item)
                    for task_id in data["cancellations"]:
                        try:
                            self.store.cancel(task_id)
                        except KeyError:
                            pass
                    for control in data.get("controls", []):
                        result = self.apply_control(control)
                        response = await client.post(
                            "/gateway/controls/result", json={"id": control["id"], "result": result}
                        )
                        response.raise_for_status()
                    proposed = data.get("profile")
                    if proposed and not self.store.active_tasks():
                        new = Profile.model_validate(proposed)
                        require_profile(new)
                        self.profile = new
                        self.device = ReplayDevice(
                            self.store,
                            VideoFileSource(self.store.root)
                            if "video-file-camera" in new.edge.plugins
                            else None,
                        )
                        self.store.set_meta("applied_profile", new.model_dump(mode="json"))
                        self.agent = DualNanobotAgent(self.store, new, self.execute, self.context)
                        self.service.agent = self.agent
                        registered = False
                    for out in self.store.pending_outputs():
                        try:
                            await self.send_output(client, out)
                            self.store.output_sent(out["id"])
                        except (httpx.HTTPError, ValueError, Conflict) as exc:
                            self.store.output_failed(out["id"], self.profile.delivery_max_attempts)
                            self.store.audit(
                                out["task_id"],
                                "bridge.send_failed",
                                {"kind": out["kind"], "error_type": type(exc).__name__},
                            )
                    # Mirror edge audit facts over the same authenticated outbound link.
                    after = self.store.meta("audit_sent", 0)
                    records = self.store.timeline(after=after)
                    if records:
                        response = await client.post(
                            "/gateway/telemetry", json={"records": records}
                        )
                        response.raise_for_status()
                        self.store.set_meta("audit_sent", records[-1]["seq"])
                    # Cloud projections avoid direct access to the board's loopback API.
                    from .contracts import digest

                    for task in self.store.tasks()[:20]:
                        snapshot = self.store.task(task["id"])
                        stamp = digest(snapshot)
                        if self.store.meta(f"snapshot:{task['id']}") != stamp:
                            response = await client.post(
                                "/gateway/task-state", json={"task": snapshot}
                            )
                            response.raise_for_status()
                            self.store.set_meta(f"snapshot:{task['id']}", stamp)
                    await asyncio.sleep(0.4)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    registered = False
                    self.store.set_meta(
                        "network_error", {"error_type": type(exc).__name__, "at": time.time()}
                    )
                    await asyncio.sleep(2)

    def apply_control(self, control):
        previous = self.store.meta(f"control:{control['id']}")
        if previous is not None:
            return previous
        try:
            required = "input.scene" if control["kind"] == "stage" else "inputs"
            if required not in require_profile(self.profile)["edge"]["capabilities"]:
                raise Conflict("input capability unavailable")
            if control["kind"] == "resume":
                result = self.inputs.resume(control["task_id"])
            else:
                observations = [InputEvent.model_validate(x) for x in control["observations"]]
                result = {"observations": self.inputs.ingest_many(observations)}
            response = {"status": "applied", "value": result}
        except (ValueError, Conflict, KeyError) as exc:
            response = {"status": "rejected", "error_type": type(exc).__name__}
        self.store.set_meta(f"control:{control['id']}", response)
        self.store.audit(
            control.get("task_id"), "control.applied", {"id": control["id"], **response}
        )
        return response

    async def send_output(self, client, out):
        if out["kind"] == "first":
            response = await client.post("/gateway/results", json=out["body"])
        else:
            body = out["body"]
            asset = self.store.asset(body["package"]["asset"]["asset_id"])
            data = Path(asset["path"]).read_bytes()
            response = await client.post(
                "/gateway/assets",
                json={
                    "package": body["package"],
                    "content_base64": base64.b64encode(data).decode(),
                },
            )
            response.raise_for_status()
            if body["mode"] == "inline":
                e = body["event"]
                response = await client.post(
                    "/gateway/results",
                    json={
                        "node_id": e["node_id"],
                        "task_id": e["task_id"],
                        "original_call_id": e["original_call_id"],
                        "status": "completed",
                        "result": e["result"],
                        "simulated": True,
                    },
                )
            else:
                response = await client.post("/gateway/events", json=body["event"])
        response.raise_for_status()
        self.store.audit(
            out["task_id"], "bridge.sent", {"kind": out["kind"], "response": response.json()}
        )

    async def start(self):
        self.store.audit(None, "scene.changed", self.store.scene().model_dump(mode="json"))
        await self.service.start()
        self.network_task = asyncio.create_task(self.network())

    async def close(self):
        self.stop.set()
        if self.network_task:
            self.network_task.cancel()
            try:
                await self.network_task
            except asyncio.CancelledError:
                pass
        await self.service.close()


def create_edge_app(profile, root, token):
    host = EdgeHost(profile, root, token)

    @asynccontextmanager
    async def lifespan(app):
        await host.start()
        yield
        await host.close()

    app = FastAPI(title="端侧回放服务", lifespan=lifespan)
    app.state.host = host
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[f"http://127.0.0.1:{profile.cloud_port}"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    async def authenticate(request: Request):
        if not secrets.compare_digest(
            request.headers.get("Authorization", ""), f"Bearer {host.token}"
        ):
            raise HTTPException(401, "node authentication required")

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.post("/inputs", dependencies=[Depends(authenticate)])
    def observations(body: InputEvent):
        if "inputs" not in require_profile(host.profile)["edge"]["capabilities"]:
            raise Conflict("input capability unavailable")
        return host.inputs.ingest(body)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin and origin not in {
            f"http://127.0.0.1:{profile.cloud_port}",
            f"http://127.0.0.1:{profile.edge_port}",
        }:
            from fastapi.responses import JSONResponse

            return JSONResponse({"error": "origin rejected"}, status_code=403)
        return await call_next(request)

    @app.get("/health")
    def health():
        return {
            "side": "edge",
            "pid": os.getpid(),
            "database": str(host.store.path),
            "profile_digest": host.profile.fingerprint,
            "model": host.profile.edge.model,
            "scene": host.store.scene().model_dump(mode="json"),
            "hostname": __import__("platform").node(),
            "machine": __import__("platform").machine(),
            "network_error": host.store.meta("network_error"),
        }

    @app.get("/tasks/{task_id}")
    def task(task_id: str):
        try:
            return host.store.task(task_id)
        except KeyError:
            raise HTTPException(404, "task not found") from None

    @app.get("/events")
    def events(after: int = 0):
        return host.store.timeline(after=after)

    @app.post("/simulation/events")
    def simulate(body: StageInput):
        try:
            return host.store.change_scene(body.stage)
        except ValueError:
            raise HTTPException(422, "unknown stage") from None

    @app.post("/tasks/{task_id}/cancel")
    def cancel(task_id: str):
        host.store.cancel(task_id)
        return {"status": host.store.task(task_id)["status"]}

    @app.post("/outbox/{output_id}/retry")
    def retry_output(output_id: str):
        try:
            host.store.retry_output(output_id)
            return {"status": "pending"}
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/events/{event_id}/retry")
    def retry_event(event_id: str):
        try:
            host.store.retry_event(event_id, host.profile.fingerprint)
            return {"status": "queued"}
        except Conflict as exc:
            raise HTTPException(409, str(exc)) from None
        except KeyError:
            raise HTTPException(404, "event not found") from None

    return app
