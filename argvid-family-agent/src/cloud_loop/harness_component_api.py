"""Operator endpoints; model tools cannot edit their own configuration."""

import asyncio
import subprocess
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse

from .harness_catalog import CameraSpec
from .harness_components import ApplyComponents, RemoveComponent, SkillImport, SourceSpec


def r1_reader_status():
    """Read the installed R1 unit; expose only service state and the live mode flag."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "r1-director.service",
             "--property=LoadState,ActiveState,MainPID"],
            capture_output=True, text=True, timeout=3, check=True,
        )
        props = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if props.get("LoadState") == "not-found":
            return {"state": "not_installed"}
        state = props.get("ActiveState", "unknown")
        pid = int(props.get("MainPID", "0"))
        if state == "active" and pid > 0:
            args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\x00")
            return {"state": state, "observe_only": b"--observe-only" in args}
        return {"state": state}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"state": "unknown"}


def mount_components(app, host, principal, node_auth):
    from fastapi.exception_handlers import request_validation_exception_handler
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        if "/components" in request.url.path or "/cameras" in request.url.path:
            return JSONResponse(
                {"error": "配置格式不正确；请检查字段、协议和地址，不要在地址中填写凭据。"},
                status_code=422,
            )
        return await request_validation_exception_handler(request, exc)

    @app.get("/harness/components/r1", dependencies=[Depends(node_auth)])
    async def reader_status():
        return await asyncio.to_thread(r1_reader_status)

    @app.get("/harness/components")
    async def components():
        return host.components.catalog()

    @app.post("/harness/components/probe")
    async def probe(body: SourceSpec, request: Request):
        await node_auth(request)
        try:
            return await host.components.probe(body)
        except Exception as exc:
            raise HTTPException(
                422,
                {
                    "message": "工具来源检查失败；请核对协议、地址及节点上的凭据文件。",
                    "error_type": type(exc).__name__,
                },
            ) from None

    @app.post("/harness/components/skills/import")
    async def import_skill(body: SkillImport, request: Request):
        await node_auth(request)
        try:
            return await asyncio.to_thread(host.components.import_skill, body)
        except Exception as exc:
            raise HTTPException(
                422,
                {
                    "message": "Skill导入失败：检查目录、SKILL.md、包大小、重名及依赖。",
                    "error_type": type(exc).__name__,
                },
            ) from None

    @app.post("/harness/components/apply")
    async def apply(body: ApplyComponents, request: Request):
        await node_auth(request)
        return await host.components.apply(body)

    @app.post("/harness/components/remove")
    async def remove(body: RemoveComponent, request: Request):
        await node_auth(request)
        return host.components.remove(body)

    @app.get("/harness/library")
    async def library(category: str | None = None, task_id: str | None = None, query: str = ""):
        if category not in {None, "original", "working", "deliverable"}:
            raise HTTPException(422, "unknown category")
        return {"side": host.side, "items": host.catalog.files(principal, category, task_id, query)}

    @app.get("/harness/cameras")
    async def cameras():
        return {
            "side": host.side,
            "sources": host.catalog.cameras(principal),
            "discovery": host.catalog.discover(),
        }

    @app.post("/harness/cameras")
    async def camera(body: CameraSpec, request: Request):
        await node_auth(request)
        return host.catalog.save(principal, body)

    @app.post("/harness/cameras/{ident}/probe")
    async def camera_probe(ident: str, request: Request):
        await node_auth(request)
        return await host.catalog.probe(principal, ident)

    @app.get("/harness/cameras/{ident}/frame")
    async def camera_frame(ident: str):
        source = host.catalog.get(principal, ident)
        probe = source.get("last_probe") or {}
        if not source["enabled"] or not probe.get("preview_available"):
            raise HTTPException(404, "no verified preview")
        path = Path(probe["frame_path"]).resolve()
        if not path.is_relative_to(host.store.root) or not path.is_file():
            raise HTTPException(404, "preview missing")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "no-store"})
