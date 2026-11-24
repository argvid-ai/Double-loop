"""Append acknowledged camera bytes to the existing saved-recording pipeline."""

import asyncio
from collections import OrderedDict
from datetime import datetime
import fcntl
import os
import threading
import time

from pydantic import Field, model_validator

from .contracts import Contract, digest
from .continuous_capture import ContinuousCaptureController
from .harness_input_store import probe_input
from .observation_contracts import Reference
from .observation_media import _fingerprint
from .store import Conflict

CHUNK_BYTES = 256 * 1024


class RecordingChunk(Contract):
    session_id: Reference
    capture_revision: Reference
    client_id: Reference
    part: int = Field(ge=0, le=1_000_000, strict=True)
    offset: int = Field(ge=0, strict=True)
    recorded_start_at: datetime

    @model_validator(mode="after")
    def aware(self):
        if self.recorded_start_at.utcoffset() is None:
            raise ValueError("device recording clock needs a timezone")
        return self


class RecordingSeal(RecordingChunk):
    size_bytes: int = Field(gt=0, strict=True)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    final: bool = False


class RecordingFinish(Contract):
    session_id: Reference
    capture_revision: Reference
    client_id: Reference


class PairedInputDisconnected(Conflict):
    pass


class PairedRecordingController(ContinuousCaptureController):
    pause_on_end = True
    clock_authority = "device_reported"

    def __init__(self, *args):
        super().__init__(*args)
        self.upload_lock = threading.RLock()
        self.finish_requested = asyncio.Event()
        self.accepting = True
        self.client_id = None
        self.next_part = 0
        self.last_received = time.monotonic()
        self.seals = OrderedDict()
        self.sealed_facts = {}
        self.stop_requested_at = None
        self.closing_requested = False

    async def _window(self, path):
        # Keep the existing capture ownership lock while an external writer feeds it.
        lease_path = (
            self.media.store.root
            / "capture-devices"
            / (digest(["paired", self.spec.binding.source_ref]) + ".lock")
        )
        lease_path.parent.mkdir(exist_ok=True)
        with lease_path.open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            while not self.finish_requested.is_set():
                if time.monotonic() - self.last_received > self.spec.idle_timeout_seconds:
                    raise PairedInputDisconnected("paired camera stopped sending media")
                try:
                    await asyncio.wait_for(self.finish_requested.wait(), 1)
                except TimeoutError:
                    pass

    def _authorize(self, request):
        if self.closing_requested:
            with self.media.store.connect() as db:
                task = db.execute(
                    "SELECT version,generation FROM observation_tasks WHERE principal=? AND task_id=?",
                    (self.run.principal, self.run.task_id),
                ).fetchone()
            if not task or tuple(task) != (self.run.version, self.run.generation):
                raise Conflict("capture generation changed during drain")
        else:
            self.current()
        if not self.accepting or request.session_id != self.capture_id:
            raise Conflict("recording session is closed or superseded")
        if request.capture_revision != digest(
            [[self.run.task_id, self.run.version, self.run.generation]]
        ):
            raise Conflict("capture generation changed")
        if self.client_id is not None and request.client_id != self.client_id:
            raise Conflict("another client owns this recording")

    def state(self):
        with self.upload_lock:
            path = self.folder / f"part-{self.next_part:06d}.mkv"
            return {
                "session_id": self.capture_id,
                "next_part": self.next_part,
                "offset": path.stat().st_size if path.exists() else 0,
                "chunk_bytes": CHUNK_BYTES,
                "recording_segment_ms": self.window_ms,
                "accepting": self.accepting,
            }

    def append(self, request, data):
        if not data or len(data) > CHUNK_BYTES:
            raise ValueError("recording chunk exceeds the negotiated byte limit")
        with self.upload_lock:
            self._authorize(request)
            if request.part != self.next_part:
                raise Conflict("recording part is out of sequence")
            path = self.folder / f"part-{request.part:06d}.mkv"
            if path.is_symlink():
                raise Conflict("recording file was redirected")
            size = path.stat().st_size if path.exists() else 0
            if request.offset > size or request.offset + len(data) > self.spec.max_file_bytes:
                raise Conflict("recording offset or segment size exceeds its bound")
            if self.client_id is None:
                self.client_id = request.client_id
                self.began = request.recorded_start_at
            elif request.recorded_start_at != self.began:
                raise Conflict("recording clock changed within a session")
            if request.offset < size:
                with path.open("rb") as source:
                    source.seek(request.offset)
                    if source.read(len(data)) != data:
                        raise Conflict("retry differs from already saved recording bytes")
                return {"offset": size, "duplicate": True}
            with path.open("ab") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            self.last_received = self.last_progress = time.monotonic()
            return {"offset": size + len(data), "duplicate": False}

    def seal(self, request):
        with self.upload_lock:
            self._authorize(request)
            if request.part in self.seals:
                old = self.seals[request.part]
                if old["request"] != request.model_dump(mode="json"):
                    raise Conflict("sealed recording receipt changed")
                return old["receipt"]
            if request.part != self.next_part:
                raise Conflict("recording part is out of sequence")
            path = self.folder / f"part-{request.part:06d}.mkv"
            if not path.is_file() or path.stat().st_size != request.size_bytes:
                raise Conflict("recording is incomplete")
            facts = probe_input(path)
            if facts["sha256"] != request.sha256:
                raise Conflict("recording differs from the client file")
            video = next((item for item in facts["streams"] if item["modality"] == "image"), {})
            left = video.get("decoded_start_ms", facts["decoded_start_ms"])
            right = video.get("decoded_end_ms", facts["duration_ms"])
            span = right - left
            if (
                span <= 0
                or span > self.window_ms + 1000
                or abs(request.start_ms - left) > 100
                or abs(request.end_ms - right) > 100
                or (not request.final and span < self.window_ms - 1000)
            ):
                raise Conflict("encoder segment boundaries differ from recorded media")
            if not set(self.spec.binding.modalities).issubset(facts["modalities"]):
                raise Conflict("paired recording lacks an installed video or audio track")
            self.sealed_facts[path.name] = {"facts": facts, "fingerprint": _fingerprint(path)[:4]}
            with self.manifest.open("a") as manifest:
                manifest.write(f"{path.name},{left / 1000},{right / 1000}\n")
                manifest.flush()
                os.fsync(manifest.fileno())
            receipt = {
                "part": request.part,
                "size_bytes": facts["bytes"],
                "sha256": facts["sha256"],
                "sealed": True,
                "start_ms": left,
                "end_ms": right,
            }
            self.seals[request.part] = {
                "request": request.model_dump(mode="json"),
                "receipt": receipt,
            }
            if len(self.seals) > 2:
                self.seals.popitem(last=False)
            self.next_part += 1
            self.last_received = self.last_progress = time.monotonic()
            self.media.store.audit(
                self.run.task_id,
                "capture.paired.segment_received",
                {
                    **receipt,
                    "client_id": self.client_id,
                    "session_id": self.capture_id,
                    "simulated": self.spec.binding.simulated,
                },
            )
            return receipt

    def finish(self, request):
        with self.upload_lock:
            if (
                self.input_ended
                and request.session_id == self.capture_id
                and request.client_id == self.client_id
                and request.capture_revision
                == digest([[self.run.task_id, self.run.version, self.run.generation]])
            ):
                return {
                    "accepted": True,
                    "parts": self.next_part,
                    "analysis_draining": not self.closed,
                }
            self._authorize(request)
            if (self.folder / f"part-{self.next_part:06d}.mkv").exists():
                raise Conflict("seal the final recording part before finishing")
            self.input_ended = True
            self.accepting = False
            self.finish_requested.set()
            return {"accepted": True, "parts": self.next_part, "analysis_draining": True}

    async def close(self):
        with self.upload_lock:
            self.closing_requested = True
            if self.stop_requested_at is None:
                self.stop_requested_at = self.media.store.clock()
        await super().close()
        while not self.media.client_stop_confirmed(
            self.run.principal,
            self.spec.binding.source_ref,
            since=self.stop_requested_at,
            client_id=self.client_id,
        ):
            await asyncio.sleep(0.1)
        return True

    async def _finish(self, worker):
        self.closing_requested = True
        if not self.input_ended and self.client_id is not None:
            try:
                await asyncio.wait_for(self.finish_requested.wait(), self.spec.idle_timeout_seconds)
            except TimeoutError:
                self.media.store.audit(
                    self.run.task_id,
                    "capture.paired.drain_timeout",
                    {
                        "session_id": self.capture_id,
                        "remaining_part": self.next_part,
                        "simulated": self.spec.binding.simulated,
                    },
                )
        with self.upload_lock:
            self.accepting = False
        await super()._finish(worker)


def active_recording(host, principal, source_ref, *, include_closed=False):
    registry = host.observation_registry
    if registry is None:
        return None
    for source in registry.sources.values():
        controller = getattr(getattr(source, "driver", None), "capture", None)
        if (
            isinstance(controller, PairedRecordingController)
            and controller.run.principal == principal
            and controller.spec.binding.source_ref == source_ref
            and (include_closed or not controller.closed)
        ):
            return controller
    return None


def mount_recording_api(app, host, principal, device_auth):
    from fastapi import HTTPException, Request

    def current(source_ref, request, *, include_closed=False):
        device_auth(request, source_ref, "observation")
        controller = active_recording(host, principal, source_ref, include_closed=include_closed)
        if controller is None:
            raise HTTPException(409, "paired recording is not active")
        return controller

    @app.post("/harness/capture/{source_ref}/recording/append")
    async def append(source_ref: str, request: Request):
        controller = current(source_ref, request, include_closed=True)
        metadata = request.headers.get("x-media-event", "")
        if len(metadata) > 4096:
            raise HTTPException(413, "recording metadata exceeds bound")
        args = RecordingChunk.model_validate_json(metadata)
        data = bytearray()
        async with asyncio.timeout(15):
            async for block in request.stream():
                data.extend(block)
                if len(data) > CHUNK_BYTES:
                    raise HTTPException(413, "recording chunk exceeds bound")
        return await asyncio.to_thread(controller.append, args, data)

    @app.post("/harness/capture/{source_ref}/recording/seal")
    async def seal(source_ref: str, body: RecordingSeal, request: Request):
        return await asyncio.to_thread(current(source_ref, request, include_closed=True).seal, body)

    @app.post("/harness/capture/{source_ref}/recording/finish")
    async def finish(source_ref: str, body: RecordingFinish, request: Request):
        return current(source_ref, request, include_closed=True).finish(body)
