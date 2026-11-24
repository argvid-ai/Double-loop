"""Driver boundary for phones, cameras, files and audio observation sources.

This module installs no capture driver, codec, audio model or HTTP upload route.
Operators bind a driver and a payload validator to the existing Gateway source
interface. Only registered observation media enters here; user speech continues
through the separate VoiceGateway. No media bytes are queued by this adapter.
"""

from __future__ import annotations

import asyncio
import shutil
from contextlib import suppress
from datetime import datetime
from typing import AsyncIterator, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from .contracts import Contract, canonical, digest
from .observation_contracts import (
    Counter,
    ObservationBatch,
    ObservationConfig,
    ObservationGap,
    ObservationRun,
    Reference,
    Timestamp,
)
from .store import Conflict

Modality = Literal["image", "audio"]


class InputBinding(Contract):
    """Installed source identity; discovering a device does not start a task."""

    source_ref: Reference
    device_ref: Reference
    kind: Literal["android", "iphone", "usb", "network", "video_file", "audio_file", "microphone"]
    modalities: tuple[Modality, ...] = Field(min_length=1, max_length=2)
    purpose: Literal["observation"] = "observation"
    finite: bool = Field(default=False, strict=True)
    simulated: bool = Field(strict=True)

    @field_validator("modalities")
    @classmethod
    def distinct(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("duplicate input modality")
        return value


class InputRange(Contract):
    event_id: Reference
    sequence: Counter
    segment_id: Reference
    segment_start_ms: Counter = 0
    start_ms: Counter
    end_ms: Counter
    captured_at: Timestamp

    @model_validator(mode="after")
    def interval(self):
        if not self.segment_start_ms <= self.start_ms < self.end_ms:
            raise ValueError("invalid input interval")
        return self


class InputWindow(InputRange):
    """Reference to a closed, registered media window, never a loose host path.

    recorded_start_at is the wall time of start_ms, when genuinely known; file
    playback time must not be substituted for recording time in a 24h query.
    The payload validator owns byte checks, raw-media retention and AV alignment.
    """

    media_ref: Reference
    media_parts: tuple[Reference, ...] = ()  # Ordered original files for one analysis window.
    modalities: tuple[Modality, ...] = Field(min_length=1, max_length=2)
    recorded_start_at: datetime | None = None
    received_monotonic: Timestamp | None = None
    source_fingerprint: tuple[Counter, Counter, Counter, Counter, Counter] | None = None

    _distinct = field_validator("modalities")(InputBinding.distinct.__func__)

    @field_validator("recorded_start_at")
    @classmethod
    def aware(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("recording wall time requires timezone")
        return value


class InputInterruption(InputRange):
    """A known missing interval. Unknown duration is a new continuous segment."""

    reason: str = Field(min_length=1, max_length=1000, pattern=r"\S")


class MediaInputDriver(Protocol):
    binding: InputBinding

    async def open(
        self, config: ObservationConfig, run: ObservationRun, cursor: dict | None
    ) -> None: ...
    def entries(self) -> AsyncIterator[InputWindow | InputInterruption]: ...
    async def close(self) -> bool: ...


class MediaInputSource:
    """Translate driver events into existing durable batches and explicit gaps.

    Start/resume opens one subscription; pause/stop cancels the Gateway producer
    and closes owned I/O. validate_payload returns True only after synchronous
    registered-manifest checks. Finite EOF can complete only after the Gateway checks
    coverage; live EOF remains an interruption. Resume cursors are driver input,
    not permission to replay old generations. The payload validator must reject
    missing/unregistered/changed media before anything is submitted as observed.
    """

    def __init__(
        self,
        ledger,
        driver: MediaInputDriver,
        *,
        required_modalities,
        validate_payload,
        preparer=None,
        prefetch_batches=0,
        sample_stream_capacity=0,
    ):
        self.ledger, self.driver = ledger, driver
        self.binding = InputBinding.model_validate(driver.binding.model_dump())
        self.required_modalities = frozenset(required_modalities)
        if not self.required_modalities or self.required_modalities - {"image", "audio"}:
            raise ValueError("declare the installed consumer media requirements")
        if not callable(validate_payload):
            raise ValueError("registered media payload validator is required")
        self.validate_payload = validate_payload
        self.preparer = preparer
        self.cache_root = preparer.cache_root if preparer else None
        self.sampling = preparer.sampling if preparer else None
        self.window_ms = preparer.window_ms if preparer else None
        self.source_ref = self.binding.source_ref
        self.simulated = self.binding.simulated
        self.complete_task_on_end = getattr(driver, "complete_task_on_end", self.binding.finite)
        self.processing_paced = getattr(driver, "pacing", None) == "processing"
        self.cleanup = None
        self.opened = self.closed = False
        from .observation_prefetch import ManagedPrefetch

        self.prefetch = ManagedPrefetch(self) if prefetch_batches else None
        self.sample_stream = None
        if sample_stream_capacity:
            from .observation_sample_stream import SavedSampleStream

            self.sample_stream = SavedSampleStream(self, sample_stream_capacity)
            driver.on_progress = self.sample_stream.progress

    async def open(self, config, run, cursor):
        if self.opened or self.closed:
            raise Conflict("input source is not fresh")
        if canonical(self.binding) != canonical(self.driver.binding):
            raise Conflict("installed input binding changed")
        if (
            config.source_ref != self.source_ref
            or config.device_ref != self.binding.device_ref
            or any(
                getattr(config, k) != getattr(run, k) for k in ("principal", "task_id", "version")
            )
        ):
            raise Conflict("input differs from task source/device binding")
        if self.required_modalities - set(self.binding.modalities):
            raise Conflict("input lacks required modalities for installed consumer")
        self.config, self.run = config, run
        await self.driver.open(config, run, cursor)
        self.complete_task_on_end = getattr(self.driver, "complete_task_on_end", self.binding.finite)
        self.opened = True
        self.ledger.store.audit(
            run.task_id,
            "observation.input.bound",
            {
                **run.model_dump(),
                "binding": self.binding.model_dump(mode="json"),
                "required_modalities": sorted(self.required_modalities),
                "user_command_authority": False,
                "simulated": self.simulated,
            },
        )

    async def entries(self):
        if not self.opened or self.closed:
            raise Conflict("input subscription is not open")
        async for event in self.driver.entries():
            if self.closed:
                raise Conflict("closed input cannot deliver media")
            if canonical(self.binding) != canonical(self.driver.binding):
                raise Conflict("installed input binding changed")
            if not isinstance(event, (InputWindow, InputInterruption)):
                raise TypeError("input driver must emit a window or explicit interruption")
            event = type(event).model_validate(event.model_dump())
            identity = (
                "input-" + digest([self.run.model_dump(), self.source_ref, event.event_id])[:40]
            )
            if self.sample_stream and isinstance(event, InputWindow):
                identity = self.sample_stream.identity(event)
            common = {
                **self.run.model_dump(),
                "source_ref": self.source_ref,
                **event.model_dump(include={"sequence", "segment_id", "start_ms", "end_ms"}),
            }
            if isinstance(event, InputInterruption):
                yield ObservationGap(**common, gap_id=identity, reason=event.reason)
                continue
            if set(event.modalities) - set(self.binding.modalities):
                raise Conflict("window claims uninstalled input modalities")
            # Synchronous validation is deliberate: it checks an already closed
            # local manifest, not unbounded remote acquisition or model work.
            if self.validate_payload(self.config, self.run, event) is not True:
                raise Conflict("registered input payload validation was not confirmed")
            self.ledger.store.audit(
                self.run.task_id,
                "observation.input.manifest",
                {
                    **self.run.model_dump(),
                    "source_ref": self.source_ref,
                    "event": event.model_dump(mode="json"),
                    "batch_id": identity,
                    "simulated": self.simulated,
                    "user_command_authority": False,
                },
            )
            yield ObservationBatch(
                **common,
                batch_id=identity,
                media_ref=event.media_ref,
                media_parts=event.media_parts,
                captured_at=event.captured_at,
                segment_start_ms=event.segment_start_ms,
                simulated=self.simulated,
                source_fingerprint=event.source_fingerprint,
                # Task-required absence blocks a definitive discard. An unused
                # modality is still declared in the manifest, but must not make
                # a visual-only task on a silent video impossible to complete.
                missing_modalities=tuple(
                    m
                    for m in ("image", "audio")
                    if m in self.required_modalities and m not in event.modalities
                ),
            )

    async def close(self):
        self.closed = True
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._close())
        return await asyncio.shield(self.cleanup)

    async def _close(self):
        if self.prefetch:
            await self.prefetch.discard()
        # A failed/cancelled open may still own hardware. Never infer release.
        try:
            driver_closed = await self.driver.close() is True
        finally:
            payload_closed = await self.preparer.close() if self.preparer else True
        return driver_closed and payload_closed is True

    async def prepare_batch(self, batch, *, attempt_id, check_active=lambda: None):
        if self.closed or self.preparer is None:
            raise Conflict("input media preparation is not installed or has closed")
        if self.prefetch:
            prepared = await self.prefetch.take(batch, check_active)
            if prepared is not None:
                return prepared
        return await self._prepare_batch(batch, attempt_id=attempt_id, check_active=check_active)

    async def _prepare_batch(self, batch, *, attempt_id, check_active=lambda: None):
        # Prefetch must hold the same raw-file leases as ordinary observation.
        from contextlib import ExitStack

        with ExitStack() as leases:
            for ref in batch.media_parts or (batch.media_ref,):
                leases.enter_context(self.preparer.media.retention.lease(batch.principal, ref))
            began = self.ledger.store.clock()
            result = await self.preparer.prepare_batch(
                batch, attempt_id=attempt_id, check_active=check_active
            )
            return {
                **result,
                "preparation_execution": {
                    "started_at": began,
                    "finished_at": self.ledger.store.clock(),
                    "worker_limit": 1,
                },
            }

    def prefetch_after(self, batch):
        return bool(self.prefetch and self.prefetch.start(batch))

    def check_prepared_source(self):
        if self.closed:
            raise Conflict("managed prefetch source closed")
        batch = self.prefetch.batch
        for ref in batch.media_parts or (batch.media_ref,):
            self.preparer.media.resolve(batch.principal, ref, allow_growing=True)

    async def next_preparable(self, batch):
        while not self.closed:
            with self.ledger.store.connect() as db:
                row = db.execute(
                    "SELECT kind,body FROM observation_media WHERE task_id=? AND sequence=?",
                    (batch.task_id, batch.sequence + 1),
                ).fetchone()
            if row:
                if row["kind"] != "batch":
                    raise Conflict("next input is an explicit gap, not an image")
                item = ObservationBatch.model_validate_json(row["body"])
                if (item.version, item.generation) != (batch.version, batch.generation):
                    raise Conflict("prefetch crossed task generation")
                return item
            await asyncio.sleep(0.05)
        raise Conflict("prefetch stopped before next capture")

    def discard_prepared(self, attempt, batch):
        path = self.cache_root / attempt / batch.batch_id
        if path.exists():
            shutil.rmtree(path)
        with suppress(OSError):
            path.parent.rmdir()
        if self.preparer.budget:
            with self.ledger.store.connect() as db:
                reserved = db.execute(
                    "SELECT 1 FROM observation_cache_reservations WHERE path=?",
                    (str(path.resolve()),),
                ).fetchone()
            if reserved:
                self.preparer.budget.settle(path, 0)

    def acknowledge(self, batch):
        callback = getattr(self.driver, "acknowledge", None)
        if callback:
            callback(batch)
