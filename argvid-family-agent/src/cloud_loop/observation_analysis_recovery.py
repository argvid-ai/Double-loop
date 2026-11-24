"""Recover only a failed visual consumer; its capture producer keeps ownership."""

import asyncio

from .harness_health import RuntimeHealth


def defer_unavailable(gateway, owner, batch_id):
    """A failed early frame is only deferred once its recorded window exists."""
    from .observation_contracts import DeferredBatch

    with gateway.store.connect() as db:
        row = db.execute(
            "SELECT entry_id FROM observation_media WHERE task_id=? AND entry_id=?",
            (owner.run.task_id, batch_id),
        ).fetchone()
    if row:
        gateway.ledger.defer_batch(DeferredBatch(
            **owner.run.model_dump(), batch_id=batch_id,
            request_id="unavailable-" + batch_id, reason="media_unavailable",
        ))
        gateway._audit(owner, "observation.media.unavailable", batch_id=batch_id,
                       analysed=False, following_media_continues=True)
        owner.unavailable_batches.discard(batch_id)


async def consume_with_recovery(gateway, owner, open_runner):
    health = RuntimeHealth(gateway.store)

    def report(code, **details):
        health.record(
            owner.run.principal, "visual_worker", code,
            task_id=owner.run.task_id, version=owner.run.version,
            generation=owner.run.generation, simulated=owner.source.simulated,
            details={"recording_continues": True, **details},
        )

    while gateway._current(owner) and not owner.analysis_paused:
        baseline = set(owner.children)
        try:
            await open_runner()
            await gateway._consume(owner)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            owner.attempt = None  # Fence late writes before releasing the old runner.
            report("analysis_failed", error_type=type(exc).__name__)
            gateway._audit(
                owner, "observation.analysis.unavailable",
                error_type=type(exc).__name__, recording_continues=True,
            )
        if not gateway._current(owner) or owner.analysis_paused:
            return
        if gateway.policy.analysis_restart_limit == 0:
            break
        owner.analysis_restarts += 1
        delay = min(
            gateway.policy.backfill_retry_seconds,
            gateway.policy.analysis_restart_seconds * 2 ** min(owner.analysis_restarts - 1, 6),
        ) if owner.analysis_restarts <= gateway.policy.analysis_restart_limit else gateway.policy.backfill_retry_seconds
        await asyncio.sleep(delay)
        if not gateway._current(owner) or owner.analysis_paused:
            return
        while gateway._current(owner) and not owner.analysis_paused:
            try:
                aborted = await gateway._bounded(
                    owner, owner.runner.abort(), gateway.policy.close_timeout
                )
                closed = await gateway._bounded(
                    owner, owner.runner.close(), gateway.policy.close_timeout
                )
                pending = {child for child in owner.children - baseline if not child.done()}
                if pending:
                    _, pending = await asyncio.wait(pending, timeout=gateway.policy.close_timeout)
                if aborted is not True or closed is not True or pending:
                    raise RuntimeError("old analysis resources not released")
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report("analysis_recovery_waiting", error_type=type(exc).__name__,
                       retry_seconds=delay)
                # Recheck resource release later, without opening another model.
                # A finite capture must also be able to end with recorded debt.
                if owner.source_exhausted:
                    return
                await asyncio.sleep(delay)
        if not gateway._current(owner) or owner.analysis_paused:
            return
        for batch_id in tuple(owner.unavailable_batches):
            defer_unavailable(gateway, owner, batch_id)
        owner.runner = gateway.runner_factory(owner.config)
        report("analysis_recovering", attempt=owner.analysis_restarts)
        gateway._audit(
            owner, "observation.analysis.restarting",
            attempt=owner.analysis_restarts, recording_continues=True,
        )
    # Explicitly disabled automatic retry keeps capture independent.
    while gateway._current(owner) and not owner.source_exhausted:
        await asyncio.sleep(0.25)
