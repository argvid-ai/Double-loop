"""Publish only saved video packet time, retaining one identity through sealing.

The packet reader does no vision inference and creates no small video files.
A growing recording is an explicitly writer-owned inode, not an immutable file.
After sealing, its full bytes are hashed and normal immutable validation applies.
"""

import csv
import json
import os
from datetime import timedelta
from pathlib import Path

import av

from .contracts import canonical, digest
from .observation_media import _fingerprint
from .recorded_audio import GrowingRecording
from .store import Conflict


def recording_rows(folder):
    path = Path(folder) / "segments.csv"
    if not path.exists():
        return []
    text = path.read_text()
    return [r for r in csv.reader(text[: text.rfind("\n") + 1].splitlines()) if len(r) == 3]


def validate_recording(raw, path):
    """Active writers may append; replacement/truncation is never permitted."""
    actual = _fingerprint(path)
    saved = tuple(raw.get("file_fingerprint", raw["source_fingerprint"]))
    if raw.get("recording_state") == "growing":
        if actual[:2] != saved[:2] or actual[2] < saved[2]:
            raise Conflict("growing recording was replaced or truncated")
    elif actual != saved:
        raise Conflict("sealed recording changed")


def publish_video_progress(controller, path, start, end, modalities):
    media, run = controller.media, controller.run
    controller.current()
    if path.is_symlink() or path.resolve().parent != controller.folder.resolve():
        raise Conflict("recording escaped its capture directory")
    event_id = controller.capture_id + "-" + path.stem
    ident = (
        "input-media-" + digest([run.principal, controller.spec.binding.source_ref, event_id])[:40]
    )
    target = media.root / "raw" / (ident + ".mkv")
    target.parent.mkdir(exist_ok=True)
    if target.is_symlink() or target.parent.resolve() != target.parent:
        raise Conflict("growing recording escaped owned storage")
    existed = target.exists()
    if not existed:
        os.link(path, target)  # One inode, not a second recording/copy.
    created_here = target.stat().st_nlink >= 2 and not existed
    try:
        return _publish_linked_progress(
            controller, path, target, ident, event_id, start, end, modalities
        )
    except BaseException:
        if created_here:
            with media.retention.lock, media.store.connect() as db:
                committed = db.execute(
                    "SELECT 1 FROM managed_input_entries WHERE path=?", (str(target),)
                ).fetchone()
                if (
                    not committed
                    and target.exists()
                    and path.exists()
                    and _fingerprint(target)[:2] == _fingerprint(path)[:2]
                ):
                    target.unlink()
        raise


def _publish_linked_progress(controller, path, target, ident, event_id, start, end, modalities):
    media, run = controller.media, controller.run
    fingerprint = _fingerprint(target)
    if fingerprint[:2] != _fingerprint(path)[:2]:
        raise Conflict("growing recording link differs from capture inode")
    body = {
        "event_id": event_id,
        "segment_id": controller.capture_id,
        "start_ms": start,
        "end_ms": end,
        "file_start_ms": start,
        "file_timestamps": "absolute",
        "recorded_start_at": (controller.began + timedelta(milliseconds=start)).isoformat(),
        "clock_authority": getattr(controller, "clock_authority", "node_reported"),
        "capture_revision": digest([[run.task_id, run.version, run.generation]]),
        "facts": {
            "modalities": modalities,
            "duration_ms": end - start,
            "bytes": fingerprint[2],
            "sha256": None,
            "media_type": "video/x-matroska",
            "probe": "saved_video_packet_frontier",
        },
        "source_fingerprint": list(fingerprint),
        "source_fingerprint_kind": "recording_identity",
        "file_fingerprint": list(fingerprint),
        "recording_state": "growing",
        "captured_at": media.store.clock(),
        "simulated": controller.spec.binding.simulated,
        "location_ref": controller.spec.location_ref,
        "source_ref": controller.spec.binding.source_ref,
    }
    with media.retention.lock:
        with media.store.connect() as db:
            old = db.execute(
                "SELECT body FROM managed_input_entries WHERE id=?", (ident,)
            ).fetchone()
        if old is None:
            media._append(
                run.principal,
                body["source_ref"],
                event_id,
                "media",
                body,
                target,
                fingerprint[2],
                rolling=True,
            )
        else:
            previous = json.loads(old[0])
            validate_recording(previous, target)
            if previous["recording_state"] != "growing" or previous["start_ms"] != start:
                raise Conflict("recording publication changed identity or reopened sealed file")
            if end <= previous["end_ms"]:
                return ident
            body["source_fingerprint"] = previous["source_fingerprint"]
            with media.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                task = db.execute(
                    "SELECT status,version,generation FROM observation_tasks WHERE task_id=? AND principal=?",
                    (run.task_id, run.principal),
                ).fetchone()
                if (
                    not task
                    or task["status"] not in {"starting", "running"}
                    or (task["version"], task["generation"]) != (run.version, run.generation)
                ):
                    raise Conflict("old capture generation cannot publish video progress")
                db.execute(
                    "UPDATE managed_input_entries SET body=?,bytes=? WHERE id=?",
                    (canonical(body), fingerprint[2], ident),
                )
    media.store.audit(
        run.task_id,
        "capture.video.available",
        {
            **run.model_dump(),
            "media_ref": ident,
            "start_ms": start,
            "end_ms": end,
            "recording_state": "growing",
            "saved_first": True,
            "simulated": body["simulated"],
        },
    )
    return ident


def seal_video_recording(controller, path, left, right):
    from .harness_input_store import probe_input

    media, run = controller.media, controller.run
    ident = (
        "input-media-"
        + digest(
            [
                run.principal,
                controller.spec.binding.source_ref,
                controller.capture_id + "-" + path.stem,
            ]
        )[:40]
    )
    with media.retention.lock:
        with media.store.connect() as db:
            row = db.execute(
                "SELECT body,path FROM managed_input_entries WHERE id=?", (ident,)
            ).fetchone()
        if row is None:
            return False
        body = json.loads(row["body"])
        target = Path(row["path"])
        validate_recording(body, target)
        if body["start_ms"] != left or right < body["end_ms"]:
            raise Conflict("sealed timeline differs from published saved video")
        if path != target:
            path.unlink(missing_ok=True)
        before = _fingerprint(target)
        cached = getattr(controller, "sealed_facts", {}).pop(path.name, None)
        facts = (
            cached["facts"]
            if cached and cached["fingerprint"] == before[:4]
            else probe_input(target)
        )
        if _fingerprint(target) != before:
            raise Conflict("recording changed during seal")
        body.update(
            end_ms=right,
            facts=facts,
            file_fingerprint=list(before),
            recording_state="sealed",
            captured_at=media.store.clock(),
        )
        with media.store.connect() as db:
            task = db.execute(
                "SELECT version,generation FROM observation_tasks WHERE task_id=? AND principal=?",
                (run.task_id, run.principal),
            ).fetchone()
            if not task or tuple(task) != (run.version, run.generation):
                raise Conflict("superseded recorder cannot seal new generation")
            db.execute(
                "UPDATE managed_input_entries SET body=?,bytes=? WHERE id=?",
                (canonical(body), facts["bytes"], ident),
            )
    media.store.audit(
        run.task_id,
        "capture.video.sealed",
        {
            **run.model_dump(),
            "media_ref": ident,
            "start_ms": left,
            "end_ms": right,
            "sha256": facts["sha256"],
        },
    )
    return True


def interrupt_video_recording(controller, path, reason):
    """Freeze only the previously published safe range after a writer has stopped."""
    from .media import file_hash

    media, run = controller.media, controller.run
    ident = (
        "input-media-"
        + digest(
            [
                run.principal,
                controller.spec.binding.source_ref,
                controller.capture_id + "-" + path.stem,
            ]
        )[:40]
    )
    with media.retention.lock, media.store.connect() as db:
        row = db.execute(
            "SELECT body,path FROM managed_input_entries WHERE id=?", (ident,)
        ).fetchone()
        if row is None:
            return False
        body = json.loads(row["body"])
        if body.get("recording_state") != "growing":
            return False
        target = Path(row["path"])
        validate_recording(body, target)
        before = _fingerprint(target)
        checksum = file_hash(target)
        if _fingerprint(target) != before:
            raise Conflict("interrupted recording still has an active writer")
        body.update(
            recording_state="interrupted",
            file_fingerprint=list(before),
            interruption={
                "reason": reason,
                "safe_end_ms": body["end_ms"],
                "complete_file_decode_verified": False,
            },
        )
        body["facts"].update(
            bytes=before[2], sha256=checksum, probe="saved_video_frontier_interrupted"
        )
        db.execute(
            "UPDATE managed_input_entries SET body=?,bytes=? WHERE id=?",
            (canonical(body), before[2], ident),
        )
        media.store._audit(
            db,
            run.task_id,
            "capture.video.interrupted",
            {
                "media_ref": ident,
                "safe_end_ms": body["end_ms"],
                "reason": reason,
                "file_bytes_retained": True,
                "simulated": body["simulated"],
            },
        )
    return True


def saved_video_frontier(controller, capture_finished, stopped):
    """Read each growing container once, publish conservative DTS/PTS frontiers."""
    metrics = dict(
        sync_calls=0, sync_seconds=0.0, max_sync_seconds=0.0, recording_bytes_read=0, opens=0
    )
    interval = controller.spec.video_publish_interval_ms or controller.analysis_window_ms
    index = 0
    try:
        while not stopped.is_set():
            path = controller.folder / f"part-{index:06d}.mkv"
            if not path.exists():
                if capture_finished():
                    return
                stopped.wait(0.01)
                continue
            if path.is_symlink() or path.resolve().parent != controller.folder.resolve():
                raise Conflict("recording reader escaped capture directory")
            rows = recording_rows(controller.folder)
            if index and len(rows) < index:
                stopped.wait(0.01)
                continue
            if index and f"part-{index - 1:06d}.mkv" not in controller.committed:
                stopped.wait(0.01)
                continue
            previous_end = round(float(rows[index - 1][2]) * 1000) if index else 0
            start = None

            def sealed():
                return capture_finished() or any(
                    Path(r[0]).name == path.name for r in recording_rows(controller.folder)
                )

            published = None
            with GrowingRecording(path, sealed, stopped, metrics) as source:
                with av.open(
                    source, format="matroska", options={"probesize": "32", "analyzeduration": "0"}
                ) as container:
                    metrics["opens"] += 1
                    if not container.streams.video:
                        raise Conflict("incremental video requires a video track")
                    modalities = ["image"] + (["audio"] if container.streams.audio else [])
                    for packet in container.demux(video=0):
                        if stopped.is_set():
                            return
                        if not packet.size or packet.pts is None:
                            continue
                        # Exclude this packet itself: all prior decoding time is saved.
                        stamp = (
                            min(packet.pts, packet.dts) if packet.dts is not None else packet.pts
                        )
                        end = int(stamp * packet.time_base * 1000)
                        if start is None:
                            start = end if index else 0
                            published = start
                            if start < previous_end:
                                raise Conflict("recording timestamps moved backwards")
                            if start > previous_end:
                                from .observation_inputs import InputInterruption

                                controller.media.add_gap(
                                    controller.run.principal,
                                    controller.spec.binding.source_ref,
                                    InputInterruption(
                                        event_id=controller.capture_id
                                        + f"-gap-{previous_end}-{start}",
                                        sequence=0,
                                        segment_id=controller.capture_id,
                                        start_ms=previous_end,
                                        end_ms=start,
                                        captured_at=controller.began.timestamp()
                                        + previous_end / 1000,
                                        reason="container_timestamp_gap",
                                    ),
                                    _capture_run=controller.run,
                                )
                        if end - published >= interval:
                            end = start + ((end - start) // interval) * interval
                            publish_video_progress(controller, path, start, end, modalities)
                            published = end
            controller.video_read_files.add(path.name)
            index += 1
    finally:
        controller.video_metrics = metrics
