"""A paced Mock camera with real file bytes and the observation source contract.

It exposes captured window references at file-playback speed, independently of
model work. Analysis is prepared only when a consumer asks. Original files are
never copied into a rolling cache or removed; replay is not physical recording.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import threading
import time
from contextlib import suppress
from pathlib import Path

import av

from .contracts import ID, canonical
from .observation_contracts import ObservationBatch
from .observation_media import FileReplayWindow, SamplingPolicy, _fingerprint
from .store import Conflict
from .observation_video_cursor import ContinuousVideoReader
from .observation_prefetch import ReplayPrefetch


class FileReplaySource:
    simulated = True

    def __init__(
        self,
        source: Path,
        source_ref: str,
        cache_root: Path,
        *,
        window_ms=5000,
        sampling: SamplingPolicy = SamplingPolicy(),
        repeat=True,
        clock=time.time,
        pose_reader=None,
        start_ms=0,
        end_ms=None,
        pacing="realtime",
        complete_task_on_end=False,
        prefetch_batches=0,
        video_reader_factory=ContinuousVideoReader,
        frame_preparer_factory=FileReplayWindow,
        memory_images=False,
    ):
        if type(window_ms) is not int or not 100 <= window_ms <= 60000:
            raise ValueError("invalid replay window size")
        if type(repeat) is not bool:
            raise ValueError("repeat must be explicit boolean")
        if pacing not in {"realtime", "processing"}:
            raise ValueError("unsupported replay pacing")
        if type(complete_task_on_end) is not bool or (
            complete_task_on_end and (pacing != "processing" or repeat)
        ):
            raise ValueError("automatic completion is only for finite processing-paced replay")
        if (
            type(start_ms) is not int
            or start_ms < 0
            or (end_ms is not None and (type(end_ms) is not int or end_ms <= start_ms))
        ):
            raise ValueError("invalid replay source interval")
        if not re.fullmatch(ID, source_ref):
            raise ValueError("invalid source reference")
        if type(prefetch_batches) is not int or prefetch_batches not in {0, 1}:
            raise ValueError("replay prefetch capacity must be zero or one")
        self.prefetch = ReplayPrefetch(self) if prefetch_batches else None
        self.frame_preparer_factory = frame_preparer_factory
        self.memory_images = memory_images
        self.prepare_lock = asyncio.Lock()
        self.source = Path(source).resolve(strict=True)
        self.source_ref, self.cache_root = source_ref, Path(cache_root).resolve()
        self.window_ms, self.sampling, self.repeat, self.clock = window_ms, sampling, repeat, clock
        self.pose_reader = pose_reader
        self.start_ms, self.end_ms = start_ms, end_ms
        self.pacing = pacing
        self.acknowledged = asyncio.Event()
        self.pending_batch = None
        self.complete_task_on_end = complete_task_on_end
        self.fingerprint = _fingerprint(self.source)
        if self.source.is_relative_to(self.cache_root):
            raise ValueError("original is not disposable analysis cache")
        identity = [source_ref, self.fingerprint]
        if start_ms or end_ms is not None:
            identity.append({"start_ms": start_ms, "end_ms": end_ms})
        self.segment_prefix = (
            "replay-" + hashlib.sha256(canonical(identity).encode()).hexdigest()[:20]
        )
        self.closed = asyncio.Event()
        self.works = {}
        self.opened = False
        self.video_reader = video_reader_factory(self.source) if sampling.include_image else None

    async def open(self, config, run, cursor):
        if self.opened:
            raise Conflict("replay source already opened")
        if config.source_ref != self.source_ref:
            raise Conflict("replay source binding mismatch")
        if _fingerprint(self.source) != self.fingerprint:
            raise Conflict("registered replay source changed")
        with av.open(str(self.source)) as container:
            durations = [
                int(((stream.start_time or 0) + stream.duration) * stream.time_base * 1000)
                for stream in container.streams
                if stream.type in {"video", "audio"} and stream.duration is not None
            ]
            if not container.streams.video or not durations:
                raise ValueError("replay source requires video and a declared duration")
            # Video remains the source clock when sound ends early. Preparation
            # then explicitly reports missing audio instead of hiding that tail.
            video = container.streams.video[0]
            self.duration_ms = (
                int(((video.start_time or 0) + video.duration) * video.time_base * 1000)
                if video.duration is not None
                else max(durations)
            )
        if self.duration_ms <= 0:
            raise ValueError("replay duration is empty")
        if self.start_ms >= self.duration_ms or (
            self.end_ms is not None and self.end_ms > self.duration_ms
        ):
            raise Conflict("configured replay interval exceeds source video")
        self.range_end = self.duration_ms if self.end_ms is None else self.end_ms
        self.run, self.config = run, config
        self.sequence = 0
        self.position_ms = self.start_ms
        self.cycle = 0
        if cursor:
            prefix = self.segment_prefix + "-"
            if cursor["source_ref"] == self.source_ref and not cursor["segment_id"].startswith(
                prefix
            ):
                raise Conflict("replay identity changed; register an explicit new source binding")
            if cursor["source_ref"] != self.source_ref:
                # A newly applied source starts a new continuous segment while
                # the task's ledger sequence still advances.
                self.sequence = cursor["sequence"] + 1
                self.cycle = run.generation
            else:
                self.sequence = cursor["sequence"] + 1
                self.position_ms = cursor["end_ms"]
                self.cycle = int(cursor["segment_id"][len(prefix) :])
                if not self.start_ms <= self.position_ms <= self.range_end:
                    raise Conflict("replay checkpoint lies outside source interval")
        self.opened = True

    def _ref(self, segment, start, end):
        return (
            "replay-media-"
            + hashlib.sha256(
                canonical(
                    {
                        "source": self.source_ref,
                        "fingerprint": self.fingerprint,
                        "segment": segment,
                        "start": start,
                        "end": end,
                    }
                ).encode()
            ).hexdigest()[:40]
        )

    def batch_id(self, sequence):
        return (
            "observation-batch-"
            + hashlib.sha256(canonical([self.run.model_dump(), sequence]).encode()).hexdigest()[:32]
        )

    async def entries(self):
        if not self.opened:
            raise Conflict("replay source is not open")
        origin = time.monotonic()
        epoch = self.clock()
        elapsed = 0.0
        while not self.closed.is_set():
            if self.position_ms >= self.range_end:
                if not self.repeat:
                    return
                self.cycle += 1
                self.position_ms = self.start_ms
            start = self.position_ms
            end = min(start + self.window_ms, self.range_end)
            elapsed += (end - start) / 1000
            delay = max(0, origin + elapsed - time.monotonic()) if self.pacing == "realtime" else 0
            if delay:
                try:
                    await asyncio.wait_for(self.closed.wait(), delay)
                except TimeoutError:
                    pass
            if self.closed.is_set():
                return
            if _fingerprint(self.source) != self.fingerprint:
                raise Conflict("replay source changed while streaming")
            segment = self.segment_prefix + "-" + str(self.cycle)
            ident = self.batch_id(self.sequence)
            captured = epoch + elapsed if self.pacing == "realtime" else self.clock()
            if self.pacing == "processing":
                self.pending_batch = ident
                self.acknowledged.clear()
            latest_sample = (
                start
                + ((end - start - 1) // self.sampling.frame_interval_ms)
                * self.sampling.frame_interval_ms
            )
            pose = (
                self.pose_reader(epoch + elapsed - (end - latest_sample) / 1000)
                if self.pose_reader
                else None
            )
            yield ObservationBatch(
                **self.run.model_dump(),
                source_ref=self.source_ref,
                batch_id=ident,
                segment_id=segment,
                sequence=self.sequence,
                start_ms=start,
                end_ms=end,
                media_ref=self._ref(segment, start, end),
                captured_at=captured,
                simulated=True,
                segment_start_ms=self.start_ms,
                source_fingerprint=self.fingerprint,
                camera_pose_revision=pose["revision"] if pose else None,
                camera_state_simulated=pose["simulated"] if pose else None,
            )
            self.sequence += 1
            self.position_ms = end
            if self.pacing == "processing":
                await self.acknowledged.wait()

    def acknowledge(self, batch):
        if self.pacing == "processing":
            if batch.batch_id != self.pending_batch:
                raise Conflict("replay acknowledgement differs from current batch")
            self.acknowledged.set()

    async def prepare_batch(self, batch, *, attempt_id, check_active=lambda: None):
        if self.prefetch:
            result = await self.prefetch.take(batch, check_active)
            if result is not None:
                return result
        return await self._prepare_batch(batch, attempt_id=attempt_id, check_active=check_active)

    def check_prepared_source(self):
        if _fingerprint(self.source) != self.fingerprint:
            raise Conflict("replay source changed after prefetch")

    def prefetch_after(self, batch):
        return bool(self.prefetch and self.prefetch.start(batch))

    def discard_prepared(self, attempt, batch):
        # This directory was never published/registered and belongs to this slot.
        path = self.cache_root / attempt / batch.batch_id
        if path.exists():
            shutil.rmtree(path)
        with suppress(OSError):
            path.parent.rmdir()

    async def _prepare_batch(self, batch, *, attempt_id, check_active=lambda: None):
        async with self.prepare_lock:
            began = self.clock()
            result = await self._prepare_locked(
                batch, attempt_id=attempt_id, check_active=check_active
            )
            return {
                **result,
                "preparation_execution": {
                    "started_at": began,
                    "finished_at": self.clock(),
                    "worker_limit": 1,
                },
            }

    async def _prepare_locked(self, batch, *, attempt_id, check_active=lambda: None):
        """Same input payload used by real inference; no recognition is performed."""
        if not re.fullmatch(ID, attempt_id):
            raise ValueError("invalid analysis attempt identity")
        if (
            batch.source_ref != self.source_ref
            or not batch.segment_id.startswith(self.segment_prefix + "-")
            or batch.start_ms < self.start_ms
            or (self.end_ms is not None and batch.end_ms > self.end_ms)
            or batch.media_ref != self._ref(batch.segment_id, batch.start_ms, batch.end_ms)
        ):
            raise Conflict("batch is not a captured range from this source")
        stop = threading.Event()

        def check():
            if stop.is_set() or self.closed.is_set():
                raise Conflict("analysis preparation interrupted")
            check_active()

        preparer = self.frame_preparer_factory(
            self.source,
            self.source_ref,
            self.cache_root / attempt_id,
            self.sampling,
            video_reader=self.video_reader,
            **({"memory_images": True} if self.memory_images else {}),
        )
        if preparer.fingerprint != self.fingerprint:
            raise Conflict("replay source changed before analysis")
        check()
        work = asyncio.create_task(
            asyncio.to_thread(
                preparer.prepare,
                batch.start_ms,
                batch.end_ms,
                batch_id=batch.batch_id,
                check_active=check,
            )
        )
        self.works[work] = stop
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            stop.set()
            with suppress(Exception):
                await asyncio.shield(work)
            raise
        finally:
            if work.done():
                self.works.pop(work, None)

    async def close(self):
        self.closed.set()
        self.acknowledged.set()
        if self.prefetch:
            await self.prefetch.discard()
        for stop in list(self.works.values()):
            stop.set()
        if self.works:
            tasks = list(self.works)
            await asyncio.wait(tasks)  # Gateway applies the outer bounded deadline.
            for task in tasks:
                if not task.cancelled():
                    task.exception()
                self.works.pop(task, None)
        if self.video_reader is not None:
            await asyncio.to_thread(self.video_reader.close)
        return not self.works
