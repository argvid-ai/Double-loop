"""Bounded candidate probe of the installed observation pipeline, not a bare LLM.

This produces measurement evidence, never model-quality or whole-stage approval.
The CLI uses the same registry/Gateway/NanoBot/retention installation as the node.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .contracts import canonical
from .harness_observation import ControlObservation, ObservationEnvelope, QueryObservation
from .observation_contracts import ObservationCommand
from .observation_measurement import ProbeMeasurements


@dataclass(frozen=True)
class ProbeLimits:
    windows: int = 3
    window_ms: int = 5000
    startup_seconds: float = 120
    final_window_seconds: float = 5
    storage_drain_seconds: float = 30
    sample_interval_seconds: float = 5
    sample_max_bytes: int = 64 * 1024**2

    def __post_init__(self):
        if type(self.windows) is not int or not 1 <= self.windows <= 20000:
            raise ValueError("invalid probe window count")
        if type(self.window_ms) is not int or not 100 <= self.window_ms <= 60000:
            raise ValueError("invalid input window")
        if (
            type(self.sample_max_bytes) is not int
            or not 1024 <= self.sample_max_bytes <= 256 * 1024**2
        ):
            raise ValueError("invalid evidence budget")
        if (
            type(self.sample_interval_seconds) not in {int, float}
            or not math.isfinite(self.sample_interval_seconds)
            or not 0.05 <= self.sample_interval_seconds <= 60
        ):
            raise ValueError("invalid sample interval")
        for value in (self.startup_seconds, self.final_window_seconds, self.storage_drain_seconds):
            if type(value) not in {float, int} or not math.isfinite(value) or not 0 < value <= 600:
                raise ValueError("invalid probe time bound")


def percentile(values, percent):
    return sorted(values)[max(0, math.ceil(len(values) * percent / 100) - 1)] if values else None


def summarize(store, task_id, limits):
    """Only real committed receipts count; latest progress cannot hide holes."""
    with store.connect() as db:
        rows = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM observation_media WHERE task_id=? ORDER BY sequence", (task_id,)
            )
        ]
        consumed = [
            json.loads(r[0])
            for r in db.execute(
                "SELECT payload FROM audit WHERE task_id=? AND kind='observation.consumed' ORDER BY seq",
                (task_id,),
            )
        ]
        faults = [
            r[0]
            for r in db.execute(
                "SELECT kind FROM audit WHERE task_id=? AND kind IN ('observation.failed','observation.deferred','observation.backfill.dispatched')",
                (task_id,),
            )
        ]
        saved = db.execute(
            "SELECT COUNT(*) FROM observation_exports WHERE task_id=? AND status='saved'",
            (task_id,),
        ).fetchone()[0]
    batches = [r for r in rows if r["kind"] == "batch"]
    live = [r for r in batches if r["seen"] and json.loads(r["seen"])["mode"] == "live"]
    unique = {r["receipt"]["batch_id"]: r for r in consumed}
    latencies = [r["end_to_end_seconds"] for r in unique.values()]
    services = [r["processing_seconds"] for r in unique.values()]
    # Q is captured by the dispatch itself, rather than inferred by subtracting
    # monotonic service time from a wall-clock timestamp.
    with store.connect() as db:
        queue = [
            json.loads(r[0])["queue_seconds"]
            for r in db.execute(
                "SELECT payload FROM audit WHERE task_id=? AND kind='observation.dispatch' ORDER BY seq",
                (task_id,),
            )
        ]
    fixed_windows = bool(batches) and all(
        json.loads(r["body"])["end_ms"] - json.loads(r["body"])["start_ms"] == limits.window_ms
        for r in batches
    )
    no_debt = (
        len(live) == len(batches) == limits.windows
        and not faults
        and not any(r["gap_reason"] for r in rows)
    )
    actual_inference = bool(live) and all(
        json.loads(r["seen"])["inference_simulated"] is False for r in live
    )
    timings_complete = (
        bool(live)
        and len(unique) == len(live) == len(consumed) == len(queue)
        and set(unique) == {r["entry_id"] for r in live}
        and all(math.isfinite(x) and x >= 0 for x in [*latencies, *services, *queue])
    )
    latency_ok = (
        bool(latencies)
        and sum(latencies) / len(latencies) <= 3.5
        and percentile(latencies, 95) <= 4
        and percentile(latencies, 99) < 5
    )
    queue_ok = bool(queue) and percentile(queue, 95) <= 1 and max(queue) <= 5
    captured = [json.loads(r["body"])["captured_at"] for r in batches]
    duration_covered = (
        bool(captured)
        and max(captured) - min(captured) >= (limits.windows - 1) * limits.window_ms / 1000 - 0.1
    )
    return {
        "requested_windows": limits.windows,
        "captured_windows": len(batches),
        "live_consumed_windows": len(live),
        "unseen_windows": sum(r["seen"] is None for r in batches),
        "gap_entries": sum(r["kind"] == "gap" for r in rows),
        "fault_events": faults,
        "saved_files": saved,
        "actual_edge_inference": actual_inference,
        "end_to_end_seconds": {
            "mean": sum(latencies) / len(latencies) if latencies else None,
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "max": max(latencies) if latencies else None,
        },
        "service_seconds": {
            "mean": sum(services) / len(services) if services else None,
            "p95": percentile(services, 95),
        },
        "queue_seconds": {"p95": percentile(queue, 95), "max": max(queue) if queue else None},
        "checks": {
            "fixed_windows": fixed_windows,
            "no_dropped_or_unresolved_input": no_debt,
            "timings_complete": timings_complete,
            "latency_thresholds": latency_ok,
            "queue_thresholds": queue_ok,
            "capture_elapsed_matches_media": duration_covered,
            "at_least_360_five_second_windows": limits.window_ms == 5000 and limits.windows >= 360,
        },
        "model_admitted": False,
        "quality_validated": False,
        "full_throughput_acceptance": False,
        "remaining_acceptance": "semantic/audio ablations, event labels, sampling/load identity, per-5-minute backlog/resource trends, two-machine integration and24h are separate required evidence",
    }


async def run_pipeline_probe(bridge, configuration, output: Path, limits: ProbeLimits):
    """Caller must provide a fresh installed node and explicitly authorized config."""
    installed = bridge.installation
    store = bridge.store
    gateway = installed.gateway
    task_id = configuration.config.task_id
    principal = "local-user"
    output = Path(output)
    result = {
        "status": "running",
        "limits": asdict(limits),
        "gateway_policy": asdict(gateway.policy),
        "whole_stage_accepted": False,
    }

    def save():
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")

    try:
        output.mkdir(parents=True, exist_ok=False)
        save()
    except BaseException:
        await bridge.close()
        raise
    started = time.monotonic()
    retaining = None
    measurements = ProbeMeasurements(
        bridge,
        task_id,
        output,
        interval=limits.sample_interval_seconds,
        max_bytes=limits.sample_max_bytes,
    )
    measurements.start()

    async def retain():
        while True:
            await bridge.retention_once()
            await asyncio.sleep(0.05)

    try:
        receipt = await bridge.receive(
            ObservationEnvelope(
                request_id="probe-config",
                principal=principal,
                operation="configure",
                configuration=configuration,
            )
        )
        task = receipt["result"]["task"]
        await bridge.receive(
            ObservationEnvelope(
                request_id="probe-start",
                principal=principal,
                operation="control",
                control=ControlObservation(
                    task_id=task_id,
                    version=task["version"],
                    generation=task["generation"],
                    action="start",
                ),
            )
        )
        async with asyncio.timeout(limits.startup_seconds):
            while True:
                measurements.check()
                state = gateway.status(principal, task_id)
                if state["task"]["status"] == "running":
                    break
                if state["task"]["status"] != "starting":
                    raise RuntimeError("candidate failed during startup")
                await asyncio.sleep(0.05)
        result["startup_seconds"] = time.monotonic() - started
        save()
        measurements.set_phase("running")
        retaining = asyncio.create_task(retain())
        deadline = (
            time.monotonic()
            + limits.windows * limits.window_ms / 1000
            + limits.final_window_seconds
        )
        reason = "window_deadline"
        while time.monotonic() < deadline:
            measurements.check()
            if retaining.done():
                await retaining
            state = bridge.local_query(principal, QueryObservation(task_id=task_id))["tasks"][0]
            ledger = state["task"]["ledger"]
            if state["task"]["status"] != "running":
                reason = "pipeline_fault"
                break
            if ledger["seen_count"] >= limits.windows:
                reason = "requested_windows_consumed"
                break
            if ledger["unseen_media_ms"] > limits.window_ms * 2:
                reason = "backlog_early_stop"
                break
            await asyncio.sleep(0.05)
        measurements.set_phase("draining")
        result["stop_reason"] = reason
        task = gateway.status(principal, task_id)["task"]
        await gateway.control(
            ObservationCommand(
                principal=principal,
                task_id=task_id,
                version=task["version"],
                generation=task["generation"],
                action="stop",
                request_id="probe-stop",
            )
        )
        result["measurement_finished_seconds"] = time.monotonic() - started
        # Only file finishing is allowed after stop, not an unlimited model drain.
        drain_started = time.monotonic()
        end = drain_started + limits.storage_drain_seconds
        while time.monotonic() < end:
            measurements.check()
            if retaining.done():
                await retaining
            counts = installed.retention.status(task_id)["counts"] if installed.retention else {}
            if not any(value for key, value in counts.items() if key != "saved"):
                break
            await asyncio.sleep(0.1)
        final_storage = (
            installed.retention.status(task_id)
            if installed.retention
            else {"counts": {}, "errors": []}
        )
        storage_complete = not any(
            value for key, value in final_storage["counts"].items() if key != "saved"
        )
        result["storage_drain"] = {
            **final_storage,
            "complete": storage_complete,
            "timed_out": not storage_complete,
            "elapsed_seconds": time.monotonic() - drain_started,
        }
        result["status"] = (
            ("measurement_finished" if storage_complete else "storage_incomplete")
            if reason == "requested_windows_consumed"
            else "measurement_failed"
        )
    except BaseException as exc:
        result.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:

        async def cleanup():
            measurements.set_phase("closing")
            # Revoke observation authority before draining diagnostic reads. A slow
            # collector must not keep capture/inference running after cancellation.
            try:
                current = gateway.status(principal, task_id)["task"]
                await gateway.control(
                    ObservationCommand(
                        principal=principal,
                        task_id=task_id,
                        version=current["version"],
                        generation=current["generation"],
                        action="stop",
                        request_id="probe-finally-stop",
                    ),
                    _shutdown=True,
                )
            except KeyError:
                pass  # configuration failed before task creation
            except Exception as exc:
                result["final_stop_error_type"] = type(exc).__name__
            if retaining:
                retaining.cancel()
                await asyncio.gather(retaining, return_exceptions=True)
            closing = asyncio.create_task(bridge.close())
            measurement_cancelled = False
            try:
                result["resource_measurements"] = await measurements.close()
            except asyncio.CancelledError:
                measurement_cancelled = True
            except Exception as exc:
                result.update(
                    status="measurement_evidence_failed", measurement_error_type=type(exc).__name__
                )
            finally:
                result["resource_measurements"] = measurements.report()
            try:
                while not closing.done():
                    try:
                        await asyncio.shield(closing)
                    except asyncio.CancelledError:
                        measurement_cancelled = True
                result["closure"] = closing.result()
            finally:
                result["elapsed_seconds"] = time.monotonic() - started
                result["metrics"] = summarize(store, task_id, limits)
                if installed.retention:
                    result["storage_after_close"] = installed.retention.status(task_id)
                with store.connect() as db:
                    with (output / "audit.jsonl").open("w") as target:
                        for row in db.execute("SELECT * FROM audit ORDER BY seq"):
                            target.write(
                                canonical({**dict(row), "payload": json.loads(row["payload"])})
                                + "\n"
                            )
                if (result.get("closure") or {}).get("confirmed") is not True:
                    result["status"] = "unconfirmed_shutdown"
                save()
            if measurement_cancelled:
                raise asyncio.CancelledError

        cleanup_task = asyncio.create_task(cleanup())
        cancelled = False
        while not cleanup_task.done():
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                cancelled = True
        cleanup_task.result()
        if cancelled:
            raise asyncio.CancelledError
    return result
