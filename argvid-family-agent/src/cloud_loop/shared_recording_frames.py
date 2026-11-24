"""Read fresh frames from the recorder's saved bytes, never from camera hardware."""

import base64
from datetime import datetime
import hashlib
import io
import os
from pathlib import Path
import time

import av

from .observation_media import _fingerprint
from .recorded_video import validate_recording
from .store import Conflict


class FilePrefix(io.RawIOBase):
    """A fixed-size view: the decoder cannot chase an indefinitely growing file."""

    def __init__(self, path):
        super().__init__()
        self.file = Path(path).open("rb")
        self.length = os.fstat(self.file.fileno()).st_size

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.file.tell()

    def read(self, size=-1):
        left = max(0, self.length - self.tell())
        return self.file.read(left if size < 0 else min(size, left))

    def seek(self, offset, whence=os.SEEK_SET):
        base = {os.SEEK_SET: 0, os.SEEK_CUR: self.tell(), os.SEEK_END: self.length}[whence]
        position = base + offset
        if position < 0:
            raise ValueError("negative recording offset")
        return self.file.seek(position)

    def close(self):
        self.file.close()
        super().close()


def latest_recorded_frame(host, principal, source_ref, *, max_age_seconds=8):
    """An owner-local read with a retention lease, current generation and real age."""
    media = host.input_media
    media.source(principal, source_ref)
    revision = media.active_revision(principal, source_ref)
    if not revision:
        raise Conflict("recording is not active; no fresh shared frame")
    with media.store.connect() as db:
        row = db.execute(
            """SELECT id FROM managed_input_entries WHERE principal=? AND source_ref=?
            AND kind='media' AND json_extract(body,'$.capture_revision')=?
            ORDER BY sequence DESC LIMIT 1""",
            (principal, source_ref, revision),
        ).fetchone()
    if row is None:
        raise Conflict("recording is starting; no saved shared frame yet")
    with media.retention.lease(principal, row["id"]):
        item = media.resolve(principal, row["id"], allow_growing=True)
        if "image" not in item["facts"]["modalities"]:
            raise Conflict("recording has no image track")
        origin = datetime.fromisoformat(item["recorded_start_at"]).timestamp()
        file_start = item["file_start_ms"]
        # Do not spend decoding work on an already stale range.
        newest_registered = origin + (item["end_ms"] - item["start_ms"]) / 1000
        if media.store.clock() - newest_registered > max_age_seconds:
            raise Conflict("shared recording frame is stale")
        path = Path(item["path"])
        before = _fingerprint(path)
        wanted = file_start + max(0, item["end_ms"] - item["start_ms"] - 500)
        chosen = None
        started = time.monotonic()
        with FilePrefix(path) as prefix:
            with av.open(prefix, format="matroska") as container:
                stream = container.streams.video[0]
                if stream.codec_context.width * stream.codec_context.height > 16_777_216:
                    raise Conflict("shared recording image exceeds decode bound")
                stream.codec_context.thread_count = 1
                container.seek(int(wanted / 1000 / float(stream.time_base)), stream=stream)
                try:
                    for index, frame in enumerate(container.decode(stream)):
                        if index > 2400 or time.monotonic() - started > 3:
                            raise Conflict("shared recording decode exceeded work bound")
                        if frame.pts is not None and float(frame.pts * frame.time_base) * 1000 >= file_start:
                            chosen = frame
                except av.error.InvalidDataError:
                    # A fixed-size snapshot may end inside the last muxed packet.
                    # Only a previously decoded complete frame can be returned.
                    if chosen is None:
                        raise Conflict("latest recording packet is not complete") from None
        if chosen is None:
            raise Conflict("recording has no decodable shared frame yet")
        if item.get("recording_state") == "growing":
            validate_recording({**item, "file_fingerprint": before}, path)
        elif _fingerprint(path) != before:
            raise Conflict("shared recording changed during reading")
        if media.active_revision(principal, source_ref) != revision:
            raise Conflict("recording stopped or changed while reading")
        frame_ms = float(chosen.pts * chosen.time_base) * 1000
        captured_at = origin + (frame_ms - file_start) / 1000
        age = media.store.clock() - captured_at
        if age < -1 or age > max_age_seconds:
            raise Conflict("shared recording frame is stale or its clock changed")
        picture = chosen.to_image()
        if chosen.rotation:
            picture = picture.rotate(chosen.rotation, expand=True)
        image = io.BytesIO()
        picture.save(image, format="JPEG", quality=85)
        raw = image.getvalue()
        return {
            "source_ref": source_ref,
            "media_ref": item["media_ref"],
            "capture_revision": revision,
            "frame_time_ms": frame_ms,
            "captured_at": captured_at,
            "age_seconds": max(0, age),
            "width": picture.width,
            "height": picture.height,
            "image_sha256": hashlib.sha256(raw).hexdigest(),
            "simulated": item["simulated"],
            "input_kind": "saved_recording",
            "clock_authority": item.get("clock_authority", "node_reported"),
            "content_base64": base64.b64encode(raw).decode(),
        }


def mount_shared_frames(app, host, principal):
    """Read-only loopback capability; never share the node's control credential."""
    import asyncio
    import ipaddress

    from fastapi import HTTPException, Request
    from fastapi.responses import JSONResponse

    busy = asyncio.Lock()

    @app.get("/harness/shared-recording/{source_ref}/frame")
    async def shared_frame(source_ref: str, request: Request):
        try:
            local = request.client and ipaddress.ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        if not local or host.side != "edge":
            raise HTTPException(403, "shared frames are read-only on the local edge node")
        if host.stop.is_set() or busy.locked():
            raise HTTPException(503, "shared frame reader unavailable; retry later")
        async with busy:
            # Shield the decoder and retain the lock if the reader disconnects.
            # A slow reader must not launch overlapping abandoned decode jobs.
            work = asyncio.create_task(
                asyncio.to_thread(latest_recorded_frame, host, principal, source_ref)
            )
            try:
                value = await asyncio.shield(work)
            except asyncio.CancelledError:
                try:
                    await work
                except Exception:
                    pass
                raise
            except (Conflict, OSError, av.error.FFmpegError):
                raise HTTPException(503, "fresh shared recording frame unavailable") from None
            return JSONResponse(value, headers={"Cache-Control": "no-store"})
