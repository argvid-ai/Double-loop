"""Bounded FIFO stages for independent native visual judgments.

Each queue is bounded. A receipt remains a completed side effect, never an
admission acknowledgement. Capture and registered file inputs share this path;
chat-dependent judgments retain their sequential context adapter.
"""

import asyncio
import time
import uuid
from dataclasses import dataclass
from contextlib import ExitStack

from .observation_contracts import ObservationBatch, SeenBatch
from .semantic_observation_runtime import SemanticObservationRunner
from .store import Conflict


async def joined_thread(function, workers=None):
    """Cancellation must fence effects and join the worker, not abandon writes."""
    task = asyncio.create_task(asyncio.to_thread(function))
    if workers is not None:
        workers.add(task)
        task.add_done_callback(workers.discard)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        raise


@dataclass
class StageItem:
    batch: ObservationBatch
    attempt: str
    active: dict
    admitted_at: float
    prepared: dict | None = None
    current: dict | None = None
    selected: dict | None = None
    preparation_seconds: float = 0
    raw_leases: ExitStack | None = None
    final_pending: bool = False


class StagedObservationRunner(SemanticObservationRunner):
    def __init__(self, *args, pipeline_capacity, pipeline_trace=False, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.native_visual or self.mode != "live":
            raise ValueError("staged runner requires independent native live judgments")
        self.pipeline_capacity = pipeline_capacity
        self.pipeline_trace = pipeline_trace
        self.trace_timings = pipeline_trace
        self.stage_workers = set()
        self.stage_items = {}

    async def prepare(self, item):
        self.stage_items[item.attempt] = item
        self.active = item.active
        self._check()
        media = getattr(getattr(self.source, "preparer", None), "media", None)
        if media:
            # The original may be needed when a later keep decision commits.
            # Hold it across both queues, not just while decoding its JPEG.
            item.raw_leases = ExitStack()
            for ref in sorted(set(item.batch.media_parts or (item.batch.media_ref,))):
                item.raw_leases.enter_context(media.retention.lease(item.batch.principal, ref))
        start = time.monotonic()
        item.prepared = await self.source.prepare_batch(
            item.batch, attempt_id=item.attempt, check_active=self._check_preparation
        )
        item.preparation_seconds = time.monotonic() - start

        def register():
            self._check()
            self._validate_assets()
            block = self.payloads.register(
                self.run.principal,
                self.run.task_id,
                item.batch,
                item.prepared,
                owner_session=self.session_ref + ":" + item.attempt,
            )
            item.current = {"role": "user", "content": [block]}

        await joined_thread(register, self.stage_workers)
        return item

    async def infer(self, item):
        self.active = item.active
        self._check()
        item.selected = await self.scorer.score([item.current], self.task_rule)
        return item

    async def commit(self, item):
        self.active = item.active

        def save():
            # Only this worker owns native session history; preparation/inference
            # consume immutable batch messages and never mutate that history.
            self.groups = []
            receipt = asyncio.run(
                self._commit_batch(
                    item.batch,
                    item.selected,
                    item.current,
                    item.prepared,
                    item.preparation_seconds,
                    False,
                    item.attempt,
                    item.active["is_current"],
                )
            )
            return receipt

        return await joined_thread(save, self.stage_workers)

    def bind_final(self, item, final):
        """Bind the completed recorded range without rewriting the earlier model input."""
        early = item.batch
        if (
            any(
                getattr(early, k) != getattr(final, k)
                for k in (
                    "task_id",
                    "version",
                    "generation",
                    "source_ref",
                    "sequence",
                    "segment_id",
                    "batch_id",
                    "start_ms",
                    "media_ref",
                )
            )
            or final.end_ms < early.end_ms
        ):
            raise Conflict("completed window differs from its early sample")
        media = self.source.preparer.media
        old = set(early.media_parts or (early.media_ref,))
        for ref in set(final.media_parts or (final.media_ref,)) - old:
            item.raw_leases.enter_context(media.retention.lease(final.principal, ref))
        self.active = item.active
        self.audit(
            "observation.sample.bound",
            {
                "sample_input_range_ms": [early.start_ms, early.end_ms],
                "marked_range_ms": [final.start_ms, final.end_ms],
                "sample_positions_ms": [
                    f["sample_at_ms"] for f in item.prepared["manifest"]["frames"]
                ],
                "recording_confirmed": True,
            },
        )
        item.batch = final

    async def _close(self):
        # Closing fences new writes via self.closed. Join outstanding DB workers
        # before releasing their payload pins or reporting resources reclaimed.
        while self.stage_workers:
            await asyncio.gather(
                *(asyncio.shield(t) for t in tuple(self.stage_workers)), return_exceptions=True
            )
        for item in list(self.stage_items.values()):
            self.unpin(item)
        return await super()._close()

    def unpin(self, item):
        try:
            self.payloads.pin_session(
                self.run.principal,
                self.run.task_id,
                self.session_ref + ":" + item.attempt,
                [],
            )
        finally:
            if item.raw_leases is not None:
                item.raw_leases.close()
                item.raw_leases = None
            self.stage_items.pop(item.attempt, None)


async def consume_staged(gateway, owner):
    """A single preparation worker, inference worker and ordered commit worker."""
    runner = owner.runner
    existing_children = set(owner.children)
    capacity = runner.pipeline_capacity
    prepared, inferred = asyncio.Queue(capacity), asyncio.Queue(capacity)
    # Include active workers as well as queued references in the admission bound.
    slots = asyncio.Semaphore(2 * capacity + 3)
    items = {}
    stopped = False
    end = object()
    sample_stream = getattr(owner.source, "sample_stream", None)

    def current():
        return not stopped and owner.attempt == token and gateway._current(owner)

    token = "pipeline-" + uuid.uuid4().hex
    owner.attempt = token

    async def stage(name, item, operation):
        began = gateway.store.clock()
        monotonic = time.monotonic()
        try:
            result = await gateway._bounded(owner, operation, gateway.policy.inference_timeout)
        except BaseException as exc:
            if (name == "prepare" and isinstance(exc, (FileNotFoundError, ValueError))
                    and not isinstance(exc, Conflict)):
                owner.unavailable_batches.add(item.batch.batch_id)
            gateway._audit(
                owner,
                "observation.pipeline.stage",
                stage=name,
                batch_id=item.batch.batch_id,
                start=began,
                end=gateway.store.clock(),
                seconds=time.monotonic() - monotonic,
                status="failed_or_cancelled",
                error_type=type(exc).__name__,
            )
            raise
        if runner.pipeline_trace:
            gateway._audit(
                owner,
                "observation.pipeline.stage",
                stage=name,
                batch_id=item.batch.batch_id,
                start=began,
                end=gateway.store.clock(),
                seconds=time.monotonic() - monotonic,
                status="completed",
                range_ms=[item.batch.start_ms, item.batch.end_ms],
                prepared_queue=prepared.qsize(),
                result_queue=inferred.qsize(),
                in_flight=len(items),
                queue_capacity=capacity,
                in_flight_limit=2 * capacity + 3,
            )
        return result

    async def produce():
        after = -1
        while current():
            await slots.acquire()
            owner.changed.clear()
            selected = gateway.ledger.next_live(
                owner.run,
                latest_after_seconds=gateway.policy.latest_after_seconds,
                preserve_order=True,
                after_sequence=after,
            )
            early = sample_stream.candidate(after) if sample_stream else None
            if early:
                with gateway.store.connect() as db:
                    deferred = db.execute(
                        "SELECT gap_reason FROM observation_media WHERE task_id=? AND entry_id=?",
                        (owner.run.task_id, early.batch_id),
                    ).fetchone()
                if (deferred and deferred[0]) or early.batch_id in owner.unavailable_batches:
                    after = early.sequence
                    sample_stream.after = after
                    slots.release()
                    continue
            final_pending = bool(
                early and (selected is None or early.sequence < selected["batch"]["sequence"])
            )
            if final_pending:
                selected = {"batch": early.model_dump(), "gaps": [], "backfill_results": []}
            if selected is None:
                slots.release()
                if owner.source_exhausted:
                    await prepared.put(end)
                    return
                if sample_stream:
                    began = gateway.store.clock()

                    def check():
                        if stopped or owner.attempt != token:
                            raise Conflict("stream decoding was cancelled")

                    advance = await sample_stream.advance(after, check)
                    if advance:
                        if runner.pipeline_trace:
                            gateway._audit(
                                owner,
                                "observation.decode.advance",
                                **advance,
                                start=began,
                                end=gateway.store.clock(),
                                source_ref=owner.config.source_ref,
                            )
                        continue
                    await owner.changed.wait()
                    continue
                try:
                    await asyncio.wait_for(owner.changed.wait(), 0.25)
                except TimeoutError:
                    pass
                continue
            if selected.get("backfill_results"):
                raise Conflict("staged input requires pending historical results to be resolved")
            batch = ObservationBatch.model_validate(selected["batch"])
            attempt = "stage-" + uuid.uuid4().hex
            item = StageItem(
                batch,
                attempt,
                dict(
                    batch_id=batch.batch_id,
                    is_current=current,
                    preparation_checks=0,
                    asset_checks=0,
                    asset_check_seconds=0.0,
                ),
                gateway.store.clock(),
                final_pending=final_pending,
            )
            items[batch.batch_id] = item
            gateway._audit(
                owner,
                "observation.dispatch",
                batch_id=batch.batch_id,
                attempt_id=attempt,
                captured_at=batch.captured_at,
                queue_seconds=item.admitted_at - batch.captured_at,
                gaps=selected["gaps"],
                input_range_ms=[batch.start_ms, batch.end_ms],
                window_pending=final_pending,
            )
            after = batch.sequence
            if sample_stream:
                sample_stream.after = after
            # Admit the next reference while this image is being prepared. This
            # releases source pacing only: durable consumption still waits for commit.
            owner.source.acknowledge(batch)
            await stage("prepare", item, runner.prepare(item))
            await prepared.put(item)

    async def infer():
        while current():
            item = await prepared.get()
            if item is end:
                await inferred.put(end)
                return
            await stage("infer", item, runner.infer(item))
            await inferred.put(item)

    async def commit():
        while current():
            item = await inferred.get()
            if item is end:
                return
            if item.final_pending:
                began = gateway.store.clock()
                while current():
                    owner.changed.clear()
                    with gateway.store.connect() as db:
                        row = db.execute(
                            "SELECT body FROM observation_media WHERE task_id=? AND entry_id=? AND kind='batch'",
                            (owner.run.task_id, item.batch.batch_id),
                        ).fetchone()
                    if row:
                        import json

                        runner.bind_final(item, ObservationBatch.model_validate(json.loads(row[0])))
                        break
                    if owner.source_exhausted:
                        raise Conflict("sample has no confirmed final recorded window")
                    await owner.changed.wait()
                if not current():
                    raise Conflict("recorded window wait cancelled")
                if runner.pipeline_trace:
                    gateway._audit(
                        owner,
                        "observation.pipeline.wait",
                        stage="commit",
                        batch_id=item.batch.batch_id,
                        start=began,
                        end=gateway.store.clock(),
                        reason="recorded_window_end",
                    )
            receipt = SeenBatch.model_validate(
                (await stage("commit", item, runner.commit(item))).model_dump()
            )
            if (
                not current()
                or receipt.batch_id != item.batch.batch_id
                or receipt.session_ref != owner.config.session_ref
                or receipt.mode != "live"
                or receipt.inference_simulated != runner.inference_simulated
                or any(getattr(receipt, k) != v for k, v in owner.run.model_dump().items())
            ):
                raise Conflict("staged consumption receipt differs from input")
            gateway.ledger.mark_seen(receipt)
            runner.unpin(item)
            gateway._audit(
                owner,
                "observation.consumed",
                receipt=receipt.model_dump(),
                processing_seconds=gateway.store.clock() - item.admitted_at,
                end_to_end_seconds=gateway.store.clock() - item.batch.captured_at,
            )
            items.pop(item.batch.batch_id)
            slots.release()

    tasks = [gateway._child(owner, worker()) for worker in (produce, infer, commit)]
    try:
        await asyncio.gather(*tasks)
    finally:
        stopped = True  # fence all effects before joining worker threads
        if owner.attempt == token:
            owner.attempt = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # _bounded can time out before a cancellation-resistant thread completes.
        # Its child remains owned by Gateway cleanup; do not unpin that input early.
        if not any(
            not child.done() for child in owner.children - existing_children if child not in tasks
        ):
            for item in items.values():
                runner.unpin(item)
