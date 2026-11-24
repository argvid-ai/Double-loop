"""Bounded node-private media ingress; no model-supplied paths or credentials."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
import uuid
from contextlib import suppress
from pathlib import Path

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import Field

from .browser_video import BrowserVideoCache
from .contracts import Contract
from .harness_input_pairing import InputPairing
from .harness_input_store import InputFile, InputSourceSpec
from .observation_contracts import Reference
from .observation_inputs import InputInterruption
from .store import Conflict


class CaptureClientState(Contract):
    client_id: Reference
    recording: bool = Field(strict=True)
    tracks_active: bool = Field(strict=True)
    connected: bool = Field(strict=True)


def mount_input_api(app, host, principal, node_auth):
    media = host.input_media
    uploads = asyncio.Semaphore(2)
    source_locks = {}
    previews = BrowserVideoCache(
        host.store.root / "input-previews", budget_bytes=host.settings.browser_preview_budget_bytes,
        shared_storage=getattr(host.store, "shared_media_storage", None),
        shutdown=host.stop,
    )
    host.input_previews = previews
    pairing = InputPairing(media)
    host.input_pairing = pairing
    from .shared_recording_frames import mount_shared_frames

    mount_shared_frames(app, host, principal)

    def edge_only():
        if host.side != "edge" or host.stop.is_set():
            raise HTTPException(409, "capture inputs must be installed on the edge node")

    @app.get("/harness/input-media/devices", dependencies=[Depends(node_auth)])
    async def devices():
        edge_only()
        from .input_devices import discover_inputs

        return await asyncio.to_thread(discover_inputs)

    @app.get("/harness/input-media/sources", dependencies=[Depends(node_auth)])
    async def sources():
        return {"sources": media.list(principal), "capture_started": False}

    @app.post("/harness/input-media/sources", dependencies=[Depends(node_auth)])
    async def register(spec: InputSourceSpec):
        edge_only()
        return media.register(principal, spec)

    @app.get("/harness/input-media/sources/{source_ref}/entries", dependencies=[Depends(node_auth)])
    async def entries(source_ref: str, after: int = -1, limit: int = 100):
        return {"entries": media.entries(principal, source_ref, after, limit)}

    @app.get(
        "/harness/input-media/sources/{source_ref}/capture-state", dependencies=[Depends(node_auth)]
    )
    async def capture_state(source_ref: str):
        from .paired_recording import active_recording

        source = media.source(principal, source_ref)
        recording = active_recording(host, principal, source_ref)
        with host.store.connect() as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='observation_tasks'"
            ).fetchone()
            tasks = (
                db.execute(
                    """SELECT task_id,version,generation,config FROM observation_tasks WHERE principal=?
                AND status IN ('starting','running') AND json_extract(config,'$.source_ref')=?""",
                    (principal, source_ref),
                ).fetchall()
                if table
                else []
            )
        windows = []
        for task in tasks:
            cfg = json.loads(task["config"])
            registry = host.observation_registry
            if registry:
                windows.append(registry.config.batches[cfg["batch_policy_ref"]].window_ms)
        released = not tasks
        if released and host.observations.installation:
            with host.store.connect() as db:
                last = db.execute(
                    "SELECT task_id FROM observation_tasks WHERE principal=? AND json_extract(config,'$.source_ref')=? ORDER BY updated DESC LIMIT 1",
                    (principal, source_ref),
                ).fetchone()
            if last:
                runtime = host.observations.installation.gateway.status(principal, last[0])
                released = runtime["task"]["generation"] == 0 or (
                    runtime.get("shutdown") or {}
                ).get("confirmed", False)
        return {
            "source": source,
            "active": bool(tasks),
            "window_ms": max(windows, default=5000),
            "capture_revision": media.active_revision(principal, source_ref),
            "capture_mode": "continuous_input_segmented"
            if source["spec"].get("continuous_capture")
            else "closed_windows_non_realtime",
            "recording_segment_ms": source["spec"].get("recording_segment_ms", 60000),
            "recording": recording.state() if recording else None,
            "resources_released": released,
            "model_quality_verified": False,
            "voice_available": bool(
                host.paired_voice.configured and "audio" in source["spec"]["binding"]["modalities"]
            ),
            "note": "explicit task demand only; device discovery never starts recording",
        }

    @app.post("/harness/input-media/sources/{source_ref}/chunks", dependencies=[Depends(node_auth)])
    async def upload(source_ref: str, request: Request):
        edge_only()
        spec = InputSourceSpec.model_validate(media.source(principal, source_ref)["spec"])
        if spec.capture and spec.capture.driver == "paired_stream":
            raise HTTPException(409, "this source requires incremental recording uploads")
        raw = request.headers.get("x-media-event", "")
        if len(raw) > 4096:
            raise HTTPException(413, "input metadata exceeds bound")
        event = InputFile.model_validate_json(raw)
        with host.store.connect() as db:
            duplicate = db.execute(
                "SELECT 1 FROM managed_input_entries WHERE principal=? AND source_ref=? AND event_id=?",
                (principal, source_ref, event.event_id),
            ).fetchone()
        declared = request.headers.get("content-length")
        if declared is not None and (
            not declared.isdecimal() or int(declared) > spec.max_file_bytes
        ):
            raise HTTPException(413, "input exceeds configured byte bound")
        if not spec.binding.finite and not duplicate:
            # A paused/stopped task does not continue accepting its capture.
            with host.store.connect() as db:
                table = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='observation_tasks'"
                ).fetchone()
                active = (
                    table
                    and db.execute(
                        """SELECT 1 FROM observation_tasks WHERE principal=?
                    AND status IN ('starting','running') AND json_extract(config,'$.source_ref')=?""",
                        (principal, source_ref),
                    ).fetchone()
                )
            if not active:
                raise HTTPException(409, "live input has no active observation subscription")
        async with uploads, source_locks.setdefault(source_ref, asyncio.Lock()):
            folder = media.root / "incoming"
            folder.mkdir(exist_ok=True)
            if folder.resolve() != folder:
                raise HTTPException(409, "input staging directory changed")
            path = folder / (uuid.uuid4().hex + ".part")
            count = 0
            try:
                async with asyncio.timeout(30):
                    with path.open("xb") as output:
                        async for chunk in request.stream():
                            count += len(chunk)
                            if count > spec.max_file_bytes:
                                raise HTTPException(413, "input exceeds configured byte bound")
                            if shutil.disk_usage(folder).free < spec.min_free_bytes + len(chunk):
                                raise HTTPException(409, "input free-space floor reached")
                            output.write(chunk)
                    if not count or (declared is not None and count != int(declared)):
                        raise HTTPException(422, "incomplete media upload")
                    cancelled = threading.Event()

                    def active():
                        if cancelled.is_set() or host.stop.is_set():
                            raise Conflict("media ingestion cancelled or node shutting down")

                    work = asyncio.create_task(
                        asyncio.to_thread(
                            media.add_owned_file if spec.binding.finite else media.add_recording,
                            principal,
                            source_ref,
                            event,
                            path,
                            check_active=active,
                        )
                    )
                    host.control_tasks.add(work)
                    try:
                        return await asyncio.shield(work)
                    except asyncio.CancelledError:
                        cancelled.set()
                        with suppress(Exception):
                            await asyncio.shield(work)
                        raise
                    finally:
                        host.control_tasks.discard(work)
            finally:
                path.unlink(missing_ok=True)

    @app.post("/harness/input-media/sources/{source_ref}/gaps", dependencies=[Depends(node_auth)])
    async def gap(source_ref: str, body: InputInterruption):
        edge_only()
        return media.add_gap(principal, source_ref, body)

    @app.post("/harness/input-media/sources/{source_ref}/seal", dependencies=[Depends(node_auth)])
    async def seal(source_ref: str):
        edge_only()
        return media.seal(principal, source_ref)

    @app.post(
        "/harness/input-media/sources/{source_ref}/begin-dataset", dependencies=[Depends(node_auth)]
    )
    async def begin_dataset(source_ref: str):
        edge_only()
        return media.begin_dataset(principal, source_ref)

    @app.post("/harness/input-media/sources/{source_ref}/pair", dependencies=[Depends(node_auth)])
    async def pair_source(source_ref: str, purpose: str = "observation", rotate: bool = False):
        edge_only()
        return pairing.issue(principal, source_ref, purpose=purpose, rotate=rotate)

    def device_auth(request, source_ref, purpose=None):
        result = pairing.authenticate(
            principal, source_ref, request.headers.get("authorization", ""), purpose=purpose
        )
        if result is None:
            raise HTTPException(401, "source-scoped device key required")
        return result

    from .paired_voice import mount_paired_voice

    mount_paired_voice(app, host, principal, device_auth)
    from .paired_recording import mount_recording_api

    mount_recording_api(app, host, principal, device_auth)

    @app.get("/harness/capture/{source_ref}/state")
    async def paired_state(source_ref: str, request: Request):
        purpose = device_auth(request, source_ref)
        return {**await capture_state(source_ref), "pairing_purpose": purpose}

    @app.post("/harness/capture/{source_ref}/window")
    async def paired_window(source_ref: str, request: Request):
        device_auth(request, source_ref, "observation")
        source = media.source(principal, source_ref)
        event = InputFile.model_validate_json(request.headers.get("x-media-event", ""))
        with host.store.connect() as db:
            old = db.execute(
                "SELECT 1 FROM managed_input_entries WHERE principal=? AND source_ref=? AND event_id=?",
                (principal, source_ref, event.event_id),
            ).fetchone()
        if (
            not source["spec"]["binding"]["finite"]
            and not old
            and (
                not event.capture_revision
                or event.capture_revision != media.active_revision(principal, source_ref)
            )
        ):
            raise HTTPException(
                409, "capture generation changed; do not relabel an old window as new"
            )
        return await upload(source_ref, request)

    @app.post("/harness/capture/{source_ref}/heartbeat")
    async def paired_heartbeat(source_ref: str, body: CaptureClientState, request: Request):
        purpose = device_auth(request, source_ref)
        media.heartbeat(
            principal,
            source_ref,
            body.client_id,
            purpose,
            recording=body.recording,
            tracks_active=body.tracks_active,
            connected=body.connected,
        )
        if purpose == "observation" and not body.recording and not body.tracks_active:
            installation = host.observations.installation
            if installation:
                await installation.gateway.recheck_source_release(principal, source_ref)
        return {
            **await capture_state(source_ref),
            "client_state_source": "client_reported_not_physical_verification",
        }

    @app.post("/harness/capture/{source_ref}/begin-dataset")
    async def paired_dataset(source_ref: str, request: Request):
        device_auth(request, source_ref, "observation")
        return await begin_dataset(source_ref)

    @app.post("/harness/capture/{source_ref}/seal")
    async def paired_seal(source_ref: str, request: Request):
        device_auth(request, source_ref, "observation")
        return await seal(source_ref)

    class RawFileResponse(FileResponse):
        async def __call__(self, scope, receive, send):
            with media.retention.lease(principal, self.media_ref):
                item = await asyncio.to_thread(media.resolve, principal, self.media_ref)
                response = FileResponse(
                    Path(item["path"]),
                    media_type=item["facts"]["media_type"],
                    headers={
                        "ETag": '"' + item["facts"]["sha256"] + '"',
                        "X-Content-Type-Options": "nosniff",
                    },
                )
                await response(scope, receive, send)

    @app.get("/harness/input-media/media/{media_ref}", dependencies=[Depends(node_auth)])
    async def raw_media(media_ref: str):
        response = RawFileResponse("unused-until-lease")
        response.media_ref = media_ref
        return response

    @app.get("/harness/input-media/media/{media_ref}/{kind}", dependencies=[Depends(node_auth)])
    async def preview(media_ref: str, kind: str):
        with media.retention.lease(principal, media_ref):
            return await preview_leased(media_ref, kind)

    async def preview_leased(media_ref: str, kind: str):
        if kind not in {"preview", "poster", "info"}:
            raise HTTPException(404, "unknown preview kind")
        item = await asyncio.to_thread(media.resolve, principal, media_ref)
        if kind == "info":
            return {
                key: item[key]
                for key in ("media_ref", "source_ref", "start_ms", "end_ms", "facts", "simulated")
            }
        if "image" not in item["facts"]["modalities"]:
            raise HTTPException(409, "audio input has no video preview")
        file = await previews.registered_range(
            item["path"],
            item["file_start_ms"],
            item["file_start_ms"] + item["end_ms"] - item["start_ms"],
            "poster" if kind == "poster" else "video",
        )
        return previews.response(file, kind)
