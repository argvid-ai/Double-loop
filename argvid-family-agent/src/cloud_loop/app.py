"""Loopback development API; /docs exposes the runnable demo surfaces."""

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .service import Service
from .store import Conflict


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=16000)
    context: dict = Field(default_factory=dict)
    run_at: datetime | None = None

    @field_validator("run_at")
    @classmethod
    def timezone_required(cls, value):
        if value is not None and value.utcoffset() is None:
            raise ValueError("run_at must include a timezone")
        return value


class CompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=160)
    result: dict


def create_app(root: Path, agent):
    service = Service(root, agent)

    @asynccontextmanager
    async def lifespan(app):
        await service.start()
        try:
            yield
        finally:
            await service.close()

    app = FastAPI(title="端云 Loop · 云端 Demo", version="0.1.0", lifespan=lifespan)
    app.state.service = service

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=404, content={"detail": "task or event not found"})

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/health")
    def health():
        return {"status": "ok", "runtime": "nanobot", "edge": "mock"}

    @app.get("/capabilities")
    def capabilities():
        return {
            "scope": "configured",
            "configuration": agent.configuration_summary(),
            "note": "每次真实运行会校验实际加载工具，并在任务 runs 中记录。",
        }

    @app.post("/tasks", status_code=202)
    def submit(request: TaskRequest):
        return service.store.create_task(
            request.prompt, request.context, request.run_at.timestamp() if request.run_at else None
        )

    @app.get("/tasks")
    def tasks():
        return service.store.tasks()

    @app.get("/tasks/{task_id}")
    def task(task_id: str):
        return service.store.task(task_id)

    @app.post("/mock/edge-tasks/{edge_id}/complete", status_code=202)
    def complete(edge_id: str, request: CompletionRequest):
        try:
            return service.store.complete_edge(edge_id, request.event_id, request.result)
        except Conflict:
            raise
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/events/{event_id}/retry", status_code=202)
    def retry(event_id: str):
        service.store.retry_event(event_id)
        return {"event_id": event_id, "status": "queued"}

    @app.get("/tasks/{task_id}/report", response_class=PlainTextResponse)
    def report(task_id: str):
        service.store.task(task_id)
        workspace = service.store.root / "tasks" / task_id / "workspace"
        path = workspace / "report.md"
        if not path.is_file() or not path.resolve().is_relative_to(workspace.resolve()):
            raise HTTPException(404, "report.md has not been produced")
        return path.read_text()

    return app
