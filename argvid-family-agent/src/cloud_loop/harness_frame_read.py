"""Read registered media on its owning node without creating an editing task."""

import asyncio
import base64
import hashlib
import io
from pathlib import Path

import av
import httpx

from .harness_media_contracts import FrameRead
from .observation_media import _fingerprint
from .store import Conflict
from .recording_time import peer_times, recording_times


def _raw_frame(host, principal, args: FrameRead):
    # The lease lives in the decoder thread, including after caller cancellation.
    with host.input_media.retention.lease(principal, args.media_ref):
        item = host.input_media.resolve(principal, args.media_ref)
        duration = item["end_ms"] - item["start_ms"]
        if "image" not in item["facts"]["modalities"]:
            raise Conflict("registered recording has no video frames")
        if args.position_ms >= duration:
            raise ValueError(
                f"position_ms is relative to this media segment: allowed 0 <= position_ms < {duration}; "
                f"recording range is [{item['start_ms']}, {item['end_ms']}) ms; "
                "subtract range_start_ms from a recording-global position"
            )
        path = Path(item["path"])
        before = _fingerprint(path)
        file_start = item["file_start_ms"]
        wanted = file_start + args.position_ms
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            # file_start_ms uses the file's presentation timestamps, including
            # minute segments that retain their original recording clock.
            container.seek(
                int(wanted / 1000 / float(stream.time_base)),
                stream=stream,
                backward=True,
            )
            chosen = None
            actual = None
            for frame in container.decode(stream):
                if frame.pts is None:
                    continue
                at = round(frame.pts * stream.time_base * 1000)
                if at < file_start:
                    continue
                if at >= file_start + duration:
                    break
                chosen, actual = frame, at
                if at >= wanted:
                    break
            if chosen is None:
                raise Conflict("no frame inside the registered media range")
            picture = chosen.to_image()
            if chosen.rotation:
                picture = picture.rotate(chosen.rotation, expand=True)
            picture.thumbnail((960, 960))
            output = io.BytesIO()
            picture.save(output, format="JPEG", quality=85)
        if _fingerprint(path) != before:
            raise Conflict("recording changed during frame reading")
        return {
            "media_ref": args.media_ref,
            "source_ref": item["source_ref"],
            "requested_position_ms": args.position_ms,
            "frame_time_ms": actual - file_start,
            "original_position_ms": item["start_ms"] + actual - file_start,
            "range_start_ms": item["start_ms"],
            "range_end_ms": item["end_ms"],
            "source_sha256": item["facts"]["sha256"],
            "simulated": item["simulated"],
            "vision_inference": False,
            "audio_understood": False,
            "selection_policy": "first_at_or_after_or_last_frame_in_registered_range",
            "recording_time": recording_times(
                item, item["start_ms"] + actual - file_start),
        }, output.getvalue()


def local_frame_packet(host, principal, args: FrameRead):
    if args.target != "local":
        raise ValueError("node frame endpoint only reads local registered media")
    if args.media_ref:
        metadata, raw = _raw_frame(host, principal, args)
    else:
        metadata = host.sources.frame(principal, args)
        raw = Path(metadata.pop("path")).read_bytes()
    return {
        "side": host.side,
        "principal": principal,
        "frame": {**metadata, "image_sha256": hashlib.sha256(raw).hexdigest()},
        "content_base64": base64.b64encode(raw).decode(),
    }


async def read_frame(host, principal, args: FrameRead):
    if args.target == "local":
        packet = await asyncio.to_thread(local_frame_packet, host, principal, args)
    else:
        response = await host.client.post(
            "/harness/source-frame",
            json={**args.model_dump(), "target": "local", "principal": principal},
            timeout=host.settings.peer_timeout_seconds,
        )
        if response.status_code in {400, 404, 409, 422}:
            try:
                body = response.json()
                detail = body.get("detail") or body.get("error")
            except ValueError:
                detail = None
            # Older edge releases intentionally sanitize ValueError messages.
            # Recover coordinates from the existing read-only metadata endpoint,
            # without restarting capture or silently changing the requested time.
            if response.status_code == 422 and args.media_ref:
                try:
                    info = await host.client.get(
                        f"/harness/input-media/media/{args.media_ref}/info",
                        timeout=host.settings.peer_timeout_seconds,
                    )
                    if info.is_success:
                        item = info.json()
                        if item.get("media_ref") == args.media_ref:
                            duration = item["end_ms"] - item["start_ms"]
                            if args.position_ms >= duration:
                                detail = (
                                    f"position_ms is segment-relative; allowed 0 <= position_ms < {duration}; "
                                    f"recording range [{item['start_ms']}, {item['end_ms']}) ms; "
                                    "subtract range start from a recording-global position"
                                )
                except (httpx.HTTPError, KeyError, ValueError, TypeError):
                    pass  # Keep the original failure if metadata cannot be read.
            if isinstance(detail, str):
                raise Conflict(f"source frame rejected ({response.status_code}): {detail}")
        response.raise_for_status()
        packet = response.json()
        if packet["side"] != host.peer or packet["principal"] != principal:
            raise Conflict("frame response belongs to a different node or owner")
    raw = base64.b64decode(packet["content_base64"], validate=True)
    if hashlib.sha256(raw).hexdigest() != packet["frame"]["image_sha256"]:
        raise Conflict("frame response bytes changed")
    frame = packet["frame"]
    if args.media_ref and frame.get("media_ref") != args.media_ref:
        raise Conflict("frame response belongs to different recording media")
    if args.media_ref and "recording_time" not in frame:
        frame["recording_time"] = {"time_status": "unknown"}
        try:
            items = await peer_times(host).lookup(principal, frame["source_ref"], {args.media_ref})
        except (httpx.HTTPError, TimeoutError):
            items = {}
        item = items.get(args.media_ref)
        if item:
            if (item["sha256"], item["start_ms"], item["end_ms"]) != (
                frame["source_sha256"], frame["range_start_ms"], frame["range_end_ms"]
            ):
                raise Conflict("recording time manifest differs from decoded media")
            frame["recording_time"] = recording_times(item, frame["original_position_ms"])
    return {**packet["frame"], "origin_side": packet["side"]}, raw
