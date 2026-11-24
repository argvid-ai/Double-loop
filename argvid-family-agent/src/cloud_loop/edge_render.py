"""Execute a saved composition on the media-owning node, without another agent turn."""

import asyncio
import hashlib
import json
import os
import threading
from contextlib import ExitStack, suppress
from pathlib import Path

from fastapi import HTTPException

from pydantic import Field, model_validator

from .contracts import Contract, canonical
from .harness_media_contracts import MediaRecipe
from .harness_media_render import render_recipe
from .media import inspect_video, processor_versions
from .observation_attachment import ObservationAssetQuery, requested_input_id
from .observation_contracts import Reference
from .store import Conflict


class EdgeRenderInput(Contract):
    selection: ObservationAssetQuery | None = None
    render_job_id: Reference | None = None
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def one_source(self):
        if (self.selection is None) == (self.render_job_id is None):
            raise ValueError("select an observation input or a completed render")
        return self


class EdgeRenderRequest(Contract):
    job_id: Reference
    recipe: MediaRecipe
    inputs: list[EdgeRenderInput] = Field(min_length=1)


class EdgeRenderer:
    def __init__(self, host):
        self.host, self.store = host, host.store
        self.root = self.store.root / "edge-renders"
        self.lock = asyncio.Lock()
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS edge_renders (
                id TEXT PRIMARY KEY, principal TEXT NOT NULL, request TEXT NOT NULL,
                status TEXT NOT NULL, result TEXT, updated REAL NOT NULL)""")

    def result(self, principal, job_id):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM edge_renders WHERE id=? AND principal=?", (job_id, principal)
            ).fetchone()
        if not row or row["status"] != "completed":
            raise KeyError(job_id)
        result = json.loads(row["result"])
        path = self.root / (job_id + ".mp4")
        if not path.is_file() or path.resolve() != path:
            raise Conflict("edge composition is unavailable")
        return result, path

    async def render(self, principal, request, disconnected):
        if self.host.side != "edge" or request.recipe.renderer != "ffmpeg":
            raise Conflict("edge composition currently requires the FFmpeg adapter")
        if len(request.inputs) != len(request.recipe.segments):
            raise ValueError("one verified input is required for each segment")
        async with self.lock:
            body = canonical(request.model_dump(mode="json"))
            with self.store.connect() as db:
                prior = db.execute(
                    "SELECT * FROM edge_renders WHERE id=?", (request.job_id,)
                ).fetchone()
                if prior and (
                    canonical(
                        EdgeRenderRequest.model_validate_json(prior["request"]).model_dump(
                            mode="json"
                        )
                    )
                    != body
                    or prior["principal"] != principal
                ):
                    raise Conflict("edge composition identity changed")
                if prior and prior["status"] == "completed":
                    return self.result(principal, request.job_id)[0]
                db.execute(
                    "INSERT OR REPLACE INTO edge_renders VALUES (?,?,?,?,?,?)",
                    (request.job_id, principal, body, "running", None, self.store.clock()),
                )
            cancelled = threading.Event()

            def check():
                if cancelled.is_set() or self.host.stop.is_set():
                    raise Conflict("edge composition cancelled")

            def work():
                temporary_inputs = []
                try:
                    with ExitStack() as stack:
                        stack.enter_context(
                            self.host.media_storage.reserve(
                                "edge-renders", self.host.settings.observation_input_file_bytes
                            )
                        )
                        for item in request.inputs:
                            if item.render_job_id:
                                _, source_path = self.result(principal, item.render_job_id)
                            else:
                                source_path = self.host.requested_observation_media.root / (
                                    requested_input_id(principal, item.selection) + ".mp4"
                                )
                                if item.selection.source_kind == "raw_input":
                                    temporary_inputs.append(source_path)
                            stack.enter_context(self.host.media_storage.pin(source_path))
                        return render_work()
                finally:
                    if getattr(self.store, "shared_media_storage", None):
                        for source_path in temporary_inputs:
                            try:
                                self.host.media_storage.release_temporary(source_path, "requested-edit-inputs")
                            except Exception as exc:
                                self.host.health.record(principal, "shared_temporary_cleanup", type(exc).__name__)

            def render_work():
                inputs = []
                for item in request.inputs:
                    if item.render_job_id:
                        previous, path = self.result(principal, item.render_job_id)
                        if previous["asset"]["sha256"] != item.sha256:
                            raise Conflict("completed render identity differs from selected media")
                        inputs.append(
                            {
                                "path": str(path),
                                "facts": previous["asset"],
                                "simulated": previous["asset"].get("simulated", True),
                            }
                        )
                        continue
                    record = self.host.observations.local_asset(
                        principal, item.selection, check_active=check
                    )
                    if record["sha256"] != item.sha256:
                        raise Conflict("edge candidate differs from inspected candidate")
                    if item.selection.source_kind == "raw_input":
                        path = self.host.requested_observation_media.get(
                            principal, record["asset_id"]
                        )["path"]
                    else:
                        path = self.host.observations._installed().archive.media_path(
                            principal, record["asset_id"]
                        )
                    inputs.append(
                        {
                            "path": str(path),
                            "facts": inspect_video(path, check_active=check),
                            "simulated": record.get("simulated", True),
                        }
                    )
                self.root.mkdir(exist_ok=True)
                path = self.root / (request.job_id + ".mp4")
                facts = render_recipe(
                    inputs,
                    request.recipe,
                    path,
                    check_active=check,
                    threads=self.host.settings.media_render_threads,
                )
                facts["simulated"] = any(item["simulated"] is not False for item in inputs)
                result = {
                    "job_id": request.job_id,
                    "execution_side": "edge",
                    "storage_side": "edge",
                    "asset": facts,
                    "recipe": request.recipe.model_dump(mode="json"),
                    "processors": processor_versions(),
                }
                self.host.media_storage.register(path, "edge-renders")
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE edge_renders SET status='completed',result=?,updated=? WHERE id=?",
                        (canonical(result), self.store.clock(), request.job_id),
                    )
                    self.store._audit(db, None, "media.edge_render.completed", result)
                return result

            worker = asyncio.create_task(asyncio.to_thread(work))
            try:
                while not worker.done():
                    if await disconnected():
                        cancelled.set()
                    await asyncio.wait({worker}, timeout=0.5)
                return worker.result()
            except BaseException:
                cancelled.set()
                with suppress(Exception):
                    await asyncio.shield(worker)
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE edge_renders SET status='failed',updated=? WHERE id=? AND status='running'",
                        (self.store.clock(), request.job_id),
                    )
                raise


async def render_on_peer(host, job, inputs, recipe, destination, check):
    selections = []
    for segment in recipe.segments:
        if not segment.asset_id:
            raise Conflict("peer composition requires attached observation candidates")
        asset = host.store.asset(segment.asset_id)
        facts = asset["info"]["asset"]
        if asset["kind"] == "deliverable" and facts.get("remote_job_id"):
            selections.append(
                EdgeRenderInput(render_job_id=facts["remote_job_id"], sha256=facts["sha256"])
            )
            continue
        binding = asset["info"].get("binding", {})
        selection = ObservationAssetQuery.model_validate(
            {key: binding[key] for key in ObservationAssetQuery.model_fields if key in binding}
        )
        selections.append(
            EdgeRenderInput(selection=selection, sha256=asset["info"]["asset"]["sha256"])
        )
    request = EdgeRenderRequest(job_id=job["id"], recipe=recipe, inputs=selections)
    check()
    response = await host.client.post(
        "/harness/media/render",
        json=request.model_dump(mode="json", exclude_none=True),
        timeout=host.profile.max_task_seconds,
    )
    response.raise_for_status()
    result = response.json()
    check()
    await download_edge_render(host, job["id"], result["asset"], destination, check)
    return {
        **result["asset"],
        "execution_side": "edge",
        "storage_side": "edge",
        "remote_job_id": job["id"],
        "local_copy_role": "delivery_cache",
        "processors": result.get("processors"),
    }


async def download_edge_render(host, job_id, facts, destination, check=lambda: None):
    destination.parent.mkdir(exist_ok=True)
    temporary = destination.with_suffix(".incoming.mp4")
    checksum = hashlib.sha256()
    size = 0
    try:
        async with host.client.stream(
            "GET", "/harness/media/render/" + job_id, timeout=120
        ) as stream:
            stream.raise_for_status()
            with temporary.open("wb") as output:
                async for block in stream.aiter_bytes():
                    check()
                    size += len(block)
                    if size > facts["size_bytes"]:
                        raise Conflict("edge render download exceeded declared size")
                    checksum.update(block)
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
        if size != facts["size_bytes"] or checksum.hexdigest() != facts["sha256"]:
            raise Conflict("edge render download checksum mismatch")
        check()
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


async def ensure_local_asset(host, asset):
    """Restore an expired delivery copy through either the API or workbench route."""
    path = Path(asset["path"]).resolve()
    if not path.is_relative_to(host.store.root.resolve()):
        raise HTTPException(404, "asset not found")
    if host.media_storage.expired(path):
        if getattr(host.store, "shared_media_storage", None):
            raise HTTPException(410, "media expired under the shared final-library policy")
        facts = asset["info"].get("asset", {})
        if not facts.get("remote_job_id"):
            raise HTTPException(410, "media expired under its retention policy")
        async with host.media_storage.download_lock:
            if host.media_storage.expired(path):
                with (
                    host.media_storage.reserve("assets", facts["size_bytes"]),
                    host.media_storage.pin(path),
                ):
                    await download_edge_render(host, facts["remote_job_id"], facts, path)
                    host.media_storage.register(path, "assets")
    if not path.is_file():
        raise HTTPException(404, "asset not found")
    return path
