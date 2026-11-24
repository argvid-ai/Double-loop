"""Bounded early samples from saved bytes; final ledger ranges remain authoritative.

Only independent, single-image windows use this adapter. A pending sample may
be scored before its window ends, but cannot become a marker or consumed receipt
until the normal source collector has published that exact window.
"""

from .contracts import digest
from .observation_contracts import ObservationBatch


class SavedSampleStream:
    def __init__(self, source, capacity):
        self.source = source
        self.limit = 2 * capacity + 3
        self.pending = {}
        self.frontier = None
        self.after = -1
        self.advanced = None
        self.notify = lambda: None

    def identity(self, event):
        s = self.source
        return (
            "input-"
            + digest(
                [s.run.model_dump(), s.source_ref, event.segment_id, event.sequence, event.start_ms]
            )[:40]
        )

    def progress(self, event):
        s = self.source
        if event.sequence < s.driver.skip_until:
            return
        self.frontier = event
        # This is a notification cache, not the authoritative media queue. If
        # full, the durable final batch still provides ordered recovery.
        if event.sequence > self.after and (
            event.sequence in self.pending or len(self.pending) < self.limit
        ):
            self.pending[event.sequence] = ObservationBatch(
                **s.run.model_dump(),
                batch_id=self.identity(event),
                source_ref=s.source_ref,
                sequence=event.sequence,
                segment_id=event.segment_id,
                segment_start_ms=event.segment_start_ms,
                start_ms=event.start_ms,
                end_ms=event.end_ms,
                media_ref=event.media_ref,
                media_parts=event.media_parts,
                captured_at=event.captured_at,
                source_fingerprint=event.source_fingerprint,
                simulated=s.simulated,
            )
        self.notify()

    def candidate(self, after):
        self.after = after
        self.pending = {seq: b for seq, b in self.pending.items() if seq > after}
        return self.pending[min(self.pending)] if self.pending else None

    async def advance(self, after, check):
        event = self.frontier
        if event is None:
            return False
        # Never discard the next unsampled image. Decode intervening frames as
        # bytes arrive, retaining the frame that covers this boundary.
        limit = min(
            event.end_ms,
            event.start_ms + self.source.window_ms if event.sequence <= after else event.start_ms,
        )
        key = (event.media_parts[-1] if event.media_parts else event.media_ref, limit)
        if (limit <= event.start_ms and event.sequence > after) or key == self.advanced:
            return False
        timings = await self.source.preparer.advance_saved(event, limit, check)
        self.advanced = key
        return {"saved_end_ms": event.end_ms, "decoded_to_ms": limit, "timings": timings}
