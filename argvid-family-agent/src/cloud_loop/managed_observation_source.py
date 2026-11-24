"""Durable capture manifests -> existing Gateway source and media payloads.

Capture runs independently; this subscription stores only durable positions.
The existing image/audio decoders are reused, with local file time explicitly
mapped to the continuous capture timeline. No model is selected in this module.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import wave
from dataclasses import asdict
import threading
import time
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path

from .contracts import canonical, digest
from .harness_input_store import InputMediaStore, InputSourceSpec
from .input_capture import CaptureController
from .observation_inputs import InputInterruption, InputWindow
from .observation_media import FileReplayWindow, SamplingPolicy
from .store import Conflict
from .raw_retention import RawRecordingExpired


def segment_ref(source_ref, segment_id):
    return "capture-" + digest([source_ref, segment_id])[:32]


class RegisteredInputDriver:
    def __init__(
        self,
        media: InputMediaStore,
        principal,
        source_ref,
        *,
        window_ms=5000,
        pacing="unpaced",
        prefetch_batches=0,
    ):
        self.media, self.principal = media, principal
        self.spec = InputSourceSpec.model_validate(media.source(principal, source_ref)["spec"])
        self.binding = self.spec.binding
        self.complete_task_on_end = self.binding.finite or self.spec.replay_duration_ms is not None
        self.silence_timeout = (
            max(self.spec.idle_timeout_seconds, self.spec.recording_segment_ms / 1000 + 15)
            if self.spec.continuous_capture
            else self.spec.idle_timeout_seconds
        )
        self.closed = asyncio.Event()
        self.last = None
        self.capture = None
        self.window_ms = window_ms
        self.pacing = pacing
        self.ack = asyncio.Event()
        self.pending_sequence = None
        self.pending_sequences = set()
        self.capacity = 1 + prefetch_batches
        self.growing_end = None
        self.last_progress = time.monotonic()
        self.cleanup = None
        self.reads = {}
        self.opened_at = None
        self.on_progress = None

    async def open(self, config, run, cursor):
        if run.principal != self.principal or config.source_ref != self.binding.source_ref:
            raise Conflict("managed input subscription identity mismatch")
        if config.capture_until is not None:
            self.complete_task_on_end = True
        self.opened_at = self.media.source_run_started(run.principal, run.task_id, run.generation)
        with self.media.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM managed_input_subscriptions WHERE task_id=? AND source_ref=?",
                (run.task_id, config.source_ref),
            ).fetchone()
            if row is not None and cursor and cursor["source_ref"] != config.source_ref:
                db.execute(
                    "DELETE FROM managed_input_subscriptions WHERE task_id=? AND source_ref=?",
                    (run.task_id, config.source_ref),
                )
                row = None
            if row is None:
                first = self.media.source(self.principal, config.source_ref)["dataset_start"]
                if not self.binding.finite:
                    first = db.execute(
                        "SELECT COALESCE(MAX(sequence),-1)+1 FROM managed_input_entries WHERE source_ref=?",
                        (config.source_ref,),
                    ).fetchone()[0]
                origin = cursor["sequence"] + 1 if cursor else 0
                db.execute(
                    "INSERT INTO managed_input_subscriptions(task_id,source_ref,first_sequence,ledger_origin,segment_epoch) VALUES (?,?,?,?,?)",
                    (
                        run.task_id,
                        config.source_ref,
                        first,
                        origin,
                        run.generation
                        if cursor and cursor["source_ref"] != config.source_ref
                        else 0,
                    ),
                )
            else:
                first, origin = row["first_sequence"], row["ledger_origin"]
            row = db.execute(
                "SELECT segment_epoch FROM managed_input_subscriptions WHERE task_id=? AND source_ref=?",
                (run.task_id, config.source_ref),
            ).fetchone()
            self.segment_epoch = row["segment_epoch"]
        self.first, self.origin = first, origin
        self.after = first - 1
        self.sequence = origin
        self.skip_until = (
            cursor["sequence"] + 1
            if cursor and cursor["source_ref"] == config.source_ref
            else origin
        )
        if self.spec.capture is not None:
            from .continuous_capture import ContinuousCaptureController
            from .paired_recording import PairedRecordingController

            controller = (
                PairedRecordingController
                if self.spec.capture.driver == "paired_stream"
                else ContinuousCaptureController
                if self.spec.continuous_capture
                else CaptureController
            )
            self.capture = controller(self.media, self.spec, config, run, self.window_ms)
            await self.capture.start()
        elif not self.binding.finite:
            while not self.media.entries(
                self.principal, self.binding.source_ref, self.first - 1, limit=1
            ):
                if self.closed.is_set():
                    raise Conflict("input subscription closed before media arrived")
                if time.monotonic() - self.last_progress > self.silence_timeout:
                    raise TimeoutError("input has not produced its first actual media window")
                await asyncio.sleep(0.1)

    def _segment(self, raw):
        base = segment_ref(self.binding.source_ref, raw)
        return base + ("-restart-" + str(self.segment_epoch) if self.segment_epoch else "")

    async def _file_entries(self):
        while not self.closed.is_set():
            if self.capture and self.capture.task.done():
                self.capture.task.result()
            items = self.media.entries(self.principal, self.binding.source_ref, self.after, limit=1)
            if not items:
                if self.capture and self.capture.task.done() and self.capture.input_ended:
                    return
                source = self.media.source(self.principal, self.binding.source_ref)
                if source["sealed"]:
                    if not self.media.entries(
                        self.principal, self.binding.source_ref, self.first - 1, limit=1
                    ):
                        raise Conflict("finite input ended without media")
                    return
                if time.monotonic() - self.last_progress > self.silence_timeout:
                    raise Conflict("input stream timed out without new media")
                try:
                    await asyncio.wait_for(self.closed.wait(), 0.1)
                except TimeoutError:
                    pass
                continue
            item = items[0]
            if self.growing_end is not None and item["end_ms"] <= self.growing_end:
                if item.get("recording_state") != "growing":
                    self.after = item["sequence"]
                    self.growing_end = None
                else:
                    await asyncio.sleep(0.05)
                continue
            interrupted = item.get("recording_state") == "interrupted"
            if interrupted or item.get("availability", {}).get("state", "available") != "available":
                # Preserve the exact original slice positions across restart/expiry.
                yield InputInterruption(
                    event_id=("interrupted-" if interrupted else "expired-")
                    + digest(item["media_ref"])[:40],
                    sequence=0,
                    segment_id=self._segment(item["segment_id"]),
                    start_ms=item["start_ms"],
                    end_ms=item["end_ms"],
                    captured_at=item["captured_at"],
                    reason="interrupted_recording_unverified"
                    if interrupted
                    else item["availability"]["reason"] or "retention_expired",
                )
                self.last_progress = time.monotonic()
            elif item["kind"] == "gap":
                body = {k: item[k] for k in InputInterruption.model_fields if k in item}
                body.update(sequence=0, segment_id=self._segment(item["segment_id"]))
                yield InputInterruption.model_validate(body)
            else:
                stop = threading.Event()

                def reading():
                    if stop.is_set() or self.closed.is_set():
                        raise Conflict("input read cancelled")

                work = asyncio.create_task(
                    asyncio.to_thread(
                        self.media.window,
                        self.principal,
                        self.binding.source_ref,
                        item,
                        check_active=reading,
                    )
                )
                self.reads[work] = stop
                try:
                    original = await asyncio.shield(work)
                except RawRecordingExpired:
                    continue
                except asyncio.CancelledError:
                    stop.set()
                    while not work.done():
                        try:
                            await asyncio.shield(work)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    with suppress(Exception):
                        work.result()
                    raise
                finally:
                    if work.done():
                        self.reads.pop(work, None)
                original = original.model_copy(update={"end_ms": item["end_ms"]})
                if self.growing_end is not None:
                    original = original.model_copy(
                        update={
                            "start_ms": self.growing_end,
                            "recorded_start_at": original.recorded_start_at
                            + timedelta(milliseconds=self.growing_end - original.start_ms)
                            if original.recorded_start_at
                            else None,
                        }
                    )
                yield original.model_copy(update={"segment_id": self._segment(original.segment_id)})
                self.last_progress = time.monotonic()
            if item.get("recording_state") == "growing":
                self.growing_end = item["end_ms"]
            else:
                self.after = item["sequence"]
                self.growing_end = None

    async def entries(self):
        """Group a continuous timeline, not physical files, into analysis windows."""
        pending = None

        async def emit(event):
            event = event.model_copy(update={"sequence": self.sequence})
            self.sequence += 1
            if event.sequence < self.skip_until:
                return
            if self.pacing == "processing" and isinstance(event, InputWindow):
                event = event.model_copy(update={"captured_at": self.media.store.clock()})
                self.pending_sequence = event.sequence
                self.pending_sequences.add(event.sequence)
                self.ack.clear()
            self.last = event
            yield event
            if self.pacing == "processing" and isinstance(event, InputWindow):
                while len(self.pending_sequences) >= self.capacity and not self.closed.is_set():
                    self.ack.clear()
                    await self.ack.wait()
            self.last_progress = time.monotonic()

        async for raw in self._file_entries():
            if pending and (
                isinstance(raw, InputInterruption)
                or pending.segment_id != raw.segment_id
                or pending.end_ms != raw.start_ms
            ):
                async for event in emit(pending):
                    yield event
                pending = None
            if isinstance(raw, InputInterruption):
                for start in range(raw.start_ms, raw.end_ms, self.window_ms):
                    gap = raw.model_copy(
                        update={
                            "start_ms": start,
                            "end_ms": min(raw.end_ms, start + self.window_ms),
                            "event_id": "gap-slice-" + digest([raw.event_id, start])[:40],
                        }
                    )
                    async for event in emit(gap):
                        yield event
                continue
            start = raw.start_ms
            while start < raw.end_ms:
                if pending is None:
                    pending = raw.model_copy(
                        update={
                            "start_ms": start,
                            "end_ms": start,
                            "recorded_start_at": raw.recorded_start_at
                            + timedelta(milliseconds=start - raw.start_ms)
                            if raw.recorded_start_at
                            else None,
                            "media_parts": (raw.media_ref,),
                        }
                    )
                end = min(raw.end_ms, pending.start_ms + self.window_ms)
                parts = tuple(dict.fromkeys((*pending.media_parts, raw.media_ref)))
                pending = pending.model_copy(
                    update={
                        "end_ms": end,
                        "media_parts": parts if len(parts) > 1 else (raw.media_ref,),
                        "event_id": "input-window-" + digest([parts, pending.start_ms, end])[:40],
                    }
                )
                if self.on_progress:
                    self.on_progress(pending.model_copy(update={"sequence": self.sequence}))
                if pending.end_ms - pending.start_ms == self.window_ms:
                    async for event in emit(pending):
                        yield event
                    pending = None
                start = end
        if pending:
            async for event in emit(pending):
                yield event

    def acknowledge(self, batch):
        if self.pacing == "processing":
            if batch.sequence not in self.pending_sequences:
                raise Conflict("input pacing acknowledgement differs")
            self.pending_sequences.remove(batch.sequence)
            self.ack.set()

    def validate_payload(self, config, run, event):
        if self.last is None or canonical(event) != canonical(self.last):
            raise Conflict("input payload was not delivered by this subscription")
        return True

    async def _close(self):
        self.closed.set()
        self.ack.set()
        for flag in self.reads.values():
            flag.set()
        if self.reads:
            await asyncio.gather(*self.reads, return_exceptions=True)
        if self.capture:
            return await self.capture.close()
        if not self.binding.finite and self.opened_at is not None:
            while not self.media.client_stop_confirmed(
                self.principal, self.binding.source_ref, since=self.opened_at
            ):
                await asyncio.sleep(0.1)
        return True

    async def close(self):
        self.closed.set()
        self.ack.set()
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._close())
        return await asyncio.shield(self.cleanup)


class ManagedMediaPreparer:
    def __init__(
        self,
        media,
        principal,
        source_ref,
        cache_root,
        *,
        sampling=None,
        window_ms=5000,
        budget=None,
        video_reader_factory=None,
        frame_preparer_factory=None,
        memory_images=False,
    ):
        self.media, self.principal, self.source_ref = media, principal, source_ref
        self.cache_root = Path(cache_root).resolve()
        self.sampling = sampling or SamplingPolicy(include_audio=False)
        self.window_ms = window_ms
        self.budget = budget
        self.closed = False
        self.works = {}
        self.prepare_lock = asyncio.Lock()
        self.video_reader = None
        self.video_reader_factory = video_reader_factory
        self.frame_preparer_factory = frame_preparer_factory
        self.memory_images = memory_images
        self.stopped = threading.Event()
        self.active_check = lambda: None

    def _reader(self, raw, attempt_id):
        from .observation_video_cursor import ContinuousVideoReader
        from .recorded_video import validate_recording
        from .recorded_audio import GrowingRecording

        growing = raw.get("source_fingerprint_kind") == "recording_identity"
        guard = (lambda: validate_recording(raw, raw["path"])) if growing else None
        if self.sampling.include_image:
            if self.video_reader is not None and self.video_reader.source != raw["path"]:
                self.video_reader.close()
                self.video_reader = None
            if self.video_reader is None:

                def sealed():
                    with self.media.store.connect() as db:
                        row = db.execute(
                            "SELECT body FROM managed_input_entries WHERE id=?", (raw["media_ref"],)
                        ).fetchone()
                    return json.loads(row[0]).get("recording_state") != "growing"

                metrics = dict(
                    sync_calls=0, sync_seconds=0.0, max_sync_seconds=0.0, recording_bytes_read=0
                )
                opener = (
                    (
                        lambda: (
                            None
                            if sealed()
                            else GrowingRecording(
                                raw["path"],
                                sealed,
                                self.stopped,
                                metrics,
                                lambda: self.active_check(),
                            )
                        )
                    )
                    if growing
                    else None
                )
                factory = self.video_reader_factory or ContinuousVideoReader
                self.video_reader = factory(raw["path"], validate_source=guard, open_stream=opener)
        factory = self.frame_preparer_factory or FileReplayWindow
        return factory(
            raw["path"],
            self.source_ref,
            self.cache_root / attempt_id,
            self.sampling,
            video_reader=self.video_reader,
            source_identity=raw["source_fingerprint"] if growing else None,
            validate_source=guard,
            **({"memory_images": True} if self.memory_images else {}),
        )

    def _prepare_parts(self, batch, attempt_id, check):
        from .observation_media import _file_record

        parts = [
            self.media.resolve(self.principal, ref, check_active=check, allow_growing=True)
            for ref in batch.media_parts
        ]
        if batch.media_parts[0] != batch.media_ref or len(set(batch.media_parts)) != len(parts):
            raise Conflict("invalid original file sequence")
        cursor = batch.start_ms
        for index, raw in enumerate(parts):
            segment = segment_ref(self.source_ref, raw["segment_id"])
            if (
                raw["source_ref"] != self.source_ref
                or raw["start_ms"] >= batch.end_ms
                or raw["end_ms"] <= batch.start_ms
                or (index > 0 and raw["start_ms"] != cursor)
                or not (
                    batch.segment_id == segment
                    or batch.segment_id.startswith(segment + "-restart-")
                )
                or not raw["start_ms"] <= cursor < raw["end_ms"]
            ):
                raise Conflict("analysis files do not cover one continuous timeline")
            cursor = min(raw["end_ms"], batch.end_ms)
        if (
            cursor != batch.end_ms
            or tuple(parts[0]["source_fingerprint"]) != batch.source_fingerprint
        ):
            raise Conflict("analysis file coverage or identity differs")
        directory = self.cache_root / attempt_id / batch.batch_id
        if not self.memory_images:
            directory.mkdir(parents=True)
        frame_bytes = {}
        frames, sounds = [], []
        image_timings = FileReplayWindow._new_timings()
        started = time.monotonic()
        try:
            for index, raw in enumerate(parts):
                check()
                left, right = max(batch.start_ms, raw["start_ms"]), min(batch.end_ms, raw["end_ms"])
                offset = raw["start_ms"] - raw["file_start_ms"]
                reader = self._reader(raw, attempt_id)
                folder = directory / str(index)
                if not self.memory_images:
                    folder.mkdir()
                interval = self.sampling.frame_interval_ms
                target = (
                    batch.start_ms + ((left - batch.start_ms + interval - 1) // interval) * interval
                )
                if self.sampling.include_image and target < right:
                    for frame in reader.images(target - offset, right - offset, folder, check):
                        frame["file_sample_at_ms"] = frame["sample_at_ms"]
                        frame["sample_at_ms"] += offset
                        original = folder / frame["file"]
                        frame["file"] = str(index) + "-" + frame["file"]
                        if self.memory_images:
                            frame_bytes[frame["file"]] = reader.frame_bytes.pop(original.name)
                        else:
                            original.rename(directory / frame["file"])
                        frame["raw_media_ref"] = raw["media_ref"]
                        frames.append(frame)
                if not self.sampling.include_audio and not self.memory_images:
                    folder.rmdir()
                if self.sampling.include_audio:
                    sound = reader.audio(left - offset, right - offset, folder, check)
                    sounds.append(folder / sound["file"])
                from .recorded_video import validate_recording

                validate_recording(raw, raw["path"])
                for key, value in reader.timings.items():
                    image_timings[key] += value
            audio = None
            if sounds:
                path = directory / "audio.wav"
                with wave.open(str(path), "wb") as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(self.sampling.audio_rate)
                    for sound in sounds:
                        with wave.open(str(sound), "rb") as source:
                            output.writeframes(source.readframes(source.getnframes()))
                for sound in sounds:
                    sound.unlink()
                    sound.parent.rmdir()
                audio = {
                    **_file_record(path),
                    "start_ms": batch.start_ms,
                    "end_ms": batch.end_ms,
                    "sample_rate": self.sampling.audio_rate,
                    "samples": (batch.end_ms - batch.start_ms) * self.sampling.audio_rate // 1000,
                    "channels": 1,
                    "sample_width_bytes": 2,
                    "silence_padding_samples": 0,
                }
            manifest = {
                "schema": "cix.observation-media/1",
                "batch_id": batch.batch_id,
                "source_ref": self.source_ref,
                "source_fingerprint": list(batch.source_fingerprint),
                "start_ms": batch.start_ms,
                "end_ms": batch.end_ms,
                "simulated": batch.simulated,
                "source_kind": "managed_capture",
                "actual_media": True,
                "frames": frames,
                "audio": audio,
                "missing_modalities": [],
                "audio_input": "provided" if audio else "not_requested",
                "policy": asdict(self.sampling),
                "raw_media_ref": batch.media_ref,
                "raw_media_parts": list(batch.media_parts),
                "source_fingerprint_kind": parts[0].get(
                    "source_fingerprint_kind", "immutable_file"
                ),
                "clock_authority": parts[0]["clock_authority"],
                "location_ref": parts[0]["location_ref"],
                "preparation_seconds": time.monotonic() - started,
                "image_reader": "continuous_multi_file",
                "image_preparation_timing": image_timings,
                "image_coverage_check": "full_window"
                if self.sampling.validate_full_window
                else "sampled_frames_only",
            }
            if not self.memory_images:
                (directory / "manifest.json").write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2)
                )
            check()
            if self.memory_images:
                return {"directory": directory, "manifest": manifest,
                        "storage": "memory", "frame_bytes": frame_bytes}
            return {"directory": directory, "manifest": manifest}
        except BaseException:
            if not self.memory_images:
                shutil.rmtree(directory)
            raise

    def _prepare(self, batch, attempt_id, check):
        from pydantic import TypeAdapter

        from .observation_contracts import Reference

        TypeAdapter(Reference).validate_python(attempt_id)
        if batch.principal != self.principal or batch.source_ref != self.source_ref:
            raise Conflict("analysis task differs from the installed source owner")
        if len(batch.media_parts) > 1:
            return self._prepare_parts(batch, attempt_id, check)
        raw = self.media.resolve(
            self.principal, batch.media_ref, check_active=check, allow_growing=True
        )
        if (
            batch.principal != self.principal
            or batch.source_ref != self.source_ref
            or raw["source_ref"] != self.source_ref
            or not (
                batch.segment_id == segment_ref(self.source_ref, raw["segment_id"])
                or batch.segment_id.startswith(
                    segment_ref(self.source_ref, raw["segment_id"]) + "-restart-"
                )
            )
            or not raw["start_ms"] <= batch.start_ms < batch.end_ms <= raw["end_ms"]
            or tuple(raw["source_fingerprint"]) != batch.source_fingerprint
        ):
            raise Conflict("analysis differs from registered captured range")
        check()
        reader = self._reader(raw, attempt_id)
        local_start = raw["file_start_ms"] + batch.start_ms - raw["start_ms"]
        result = reader.prepare(
            local_start,
            local_start + batch.end_ms - batch.start_ms,
            batch_id=batch.batch_id,
            check_active=check,
        )
        manifest = result["manifest"]
        offset = batch.start_ms - local_start
        for frame in manifest["frames"]:
            frame["file_sample_at_ms"] = frame["sample_at_ms"]
            frame["sample_at_ms"] += offset
        if manifest["audio"]:
            manifest["audio"].update(start_ms=batch.start_ms, end_ms=batch.end_ms)
        manifest.update(
            start_ms=batch.start_ms,
            end_ms=batch.end_ms,
            simulated=batch.simulated,
            source_kind="managed_capture",
            raw_media_ref=batch.media_ref,
            source_fingerprint_kind=raw.get("source_fingerprint_kind", "immutable_file"),
            file_start_ms=local_start,
            recorded_start_at=(
                datetime.fromisoformat(raw["recorded_start_at"])
                + timedelta(milliseconds=batch.start_ms - raw["start_ms"])
            ).isoformat()
            if raw["recorded_start_at"]
            else None,
            clock_authority=raw["clock_authority"],
            location_ref=raw["location_ref"],
        )
        if not self.memory_images:
            (result["directory"] / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2)
            )
        check()
        return result

    async def prepare_batch(self, batch, *, attempt_id, check_active=lambda: None):
        async with self.prepare_lock:
            return await self._prepare_budgeted(
                batch, attempt_id=attempt_id, check_active=check_active
            )

    async def _prepare_budgeted(self, batch, *, attempt_id, check_active):
        if self.budget is not None and not self.memory_images:
            duration = batch.end_ms - batch.start_ms
            frames = (
                (duration + self.sampling.frame_interval_ms - 1) // self.sampling.frame_interval_ms
                if self.sampling.include_image
                else 0
            )
            native_audio = duration * 32 + frames * 64 if self.sampling.include_audio else 0
            bound = (
                frames * (self.sampling.image_long_edge**2 * 8 + 65536)
                + (duration * 64 if self.sampling.include_audio else 0)
                + 256 * 1024
            )
            path = self.cache_root / attempt_id / batch.batch_id
            from pydantic import TypeAdapter

            from .observation_contracts import Reference

            TypeAdapter(Reference).validate_python(attempt_id)
            async with self.budget.lock:
                self.budget.reserve(path, bound)
                try:
                    return await self._prepare_batch(
                        batch, attempt_id=attempt_id, check_active=check_active
                    )
                finally:
                    self.budget.settle(path, native_audio if path.exists() else 0)
        return await self._prepare_batch(batch, attempt_id=attempt_id, check_active=check_active)

    async def _prepare_batch(self, batch, *, attempt_id, check_active):
        return await self._run_work(
            lambda check: self._prepare(batch, attempt_id, check), check_active
        )

    async def advance_saved(self, event, limit, check_active):
        """Advance the same decoder through saved intervening frames, without JPEGs."""

        def advance(check):
            ref = event.media_parts[-1] if event.media_parts else event.media_ref
            with self.media.retention.lease(self.principal, ref):
                raw = self.media.resolve(
                    self.principal, ref, check_active=check, allow_growing=True
                )
                local_limit = raw["file_start_ms"] + min(limit, raw["end_ms"]) - raw["start_ms"]
                if local_limit <= raw["file_start_ms"]:
                    return None
                self._reader(raw, "stream-advance")
                return self.video_reader.advance(local_limit, check)

        async with self.prepare_lock:
            return await self._run_work(advance, check_active)

    async def _run_work(self, function, check_active):
        if self.closed:
            raise Conflict("managed media preparation has closed")
        cancelled = threading.Event()

        def check():
            if cancelled.is_set() or self.closed:
                raise Conflict("managed analysis preparation cancelled")
            check_active()

        self.active_check = check
        task = asyncio.create_task(asyncio.to_thread(function, check))
        self.works[task] = cancelled
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled.set()
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            with suppress(Exception):
                task.result()
            raise
        finally:
            if task.done():
                self.works.pop(task, None)

    async def close(self):
        self.closed = True
        self.stopped.set()
        for flag in self.works.values():
            flag.set()
        if self.works:
            tasks = list(self.works)
            await asyncio.wait(tasks)
            for task in tasks:
                self.works.pop(task, None)
        if self.video_reader:
            await asyncio.to_thread(self.video_reader.close)
        return not self.works
