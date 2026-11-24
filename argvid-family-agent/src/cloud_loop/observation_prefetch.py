"""One unpublished replay window, prepared while the current model call runs.

This never appends capture entries, acknowledges input or chooses model actions.
Only the actual next batch can claim the prepared files. A task stop, retry or
jump discards this private slot after its worker has released the video reader.
"""

import asyncio
import time
import uuid
from contextlib import suppress

from .contracts import canonical


class ReplayPrefetch:
    def __init__(self, source):
        self.source = source
        self.batch = self.work = self.attempt = None

    @staticmethod
    def key(batch):
        return canonical(
            batch.model_dump(
                exclude={"captured_at", "camera_pose_revision", "camera_state_simulated"}
            )
        )

    async def discard(self):
        if self.work is None:
            return
        work, batch, attempt = self.work, self.batch, self.attempt
        self.work = self.batch = self.attempt = None
        work.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await work
        if batch is not None:
            self.source.discard_prepared(attempt, batch)

    async def take(self, batch, check_active):
        if self.work is None:
            return None
        if self.key(batch) != self.key(self.batch):
            await self.discard()
            return None
        started = time.monotonic()
        try:
            result = await asyncio.shield(self.work)
            check_active()
            self.source.check_prepared_source()
        except BaseException:
            await self.discard()
            raise
        self.work = self.batch = self.attempt = None
        return {
            **result,
            "prefetch": {"hit": True, "wait_seconds": time.monotonic() - started, "capacity": 1},
        }

    def start(self, batch):
        s = self.source
        if self.work is not None or s.closed.is_set() or batch.end_ms >= s.range_end:
            return False
        start, end = batch.end_ms, min(batch.end_ms + s.window_ms, s.range_end)
        following = batch.model_copy(
            update={
                "sequence": batch.sequence + 1,
                "batch_id": s.batch_id(batch.sequence + 1),
                "start_ms": start,
                "end_ms": end,
                "media_ref": s._ref(batch.segment_id, start, end),
            }
        )
        self.batch = following
        self.attempt = "prefetch-" + uuid.uuid4().hex
        self.work = asyncio.create_task(s._prepare_batch(following, attempt_id=self.attempt))
        # Retain failures for take()/discard(), without unhandled-task warnings.
        self.work.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        return True


class ManagedPrefetch(ReplayPrefetch):
    """Wait for one actual ledger entry; never predict a camera capture."""

    def start(self, batch):
        if self.work is not None or self.source.closed:
            return False
        self.expected = (batch.task_id, batch.version, batch.generation, batch.sequence + 1)
        self.ready = asyncio.Event()
        self.attempt = "prefetch-" + uuid.uuid4().hex

        async def prepare_next():
            try:
                self.batch = await self.source.next_preparable(batch)
                self.ready.set()
                return await self.source._prepare_batch(self.batch, attempt_id=self.attempt)
            finally:
                self.ready.set()

        self.work = asyncio.create_task(prepare_next())
        self.work.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        return True

    async def take(self, batch, check_active):
        if self.work is None:
            return None
        if self.expected != (batch.task_id, batch.version, batch.generation, batch.sequence):
            await self.discard()
            return None
        try:
            await self.ready.wait()
            if self.batch is None:
                await self.work
            return await super().take(batch, check_active)
        except BaseException:
            await self.discard()
            raise
