"""Continuous observation scheduling, separate from NanoBot's agent/tool loop.

Adapters own capture, NanoBot execution, and model cancellation. The Gateway
owns one live invocation per task and a durable media frontier. No media bytes
are queued in memory, no end-of-turn finishes the task, and no receipt implies
that a clip was saved or a physical device moved.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import math
import time
import uuid
import traceback
from pathlib import Path
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Protocol

from .observation_contracts import (
    DeferredBatch,
    ObservationBatch,
    ObservationCommand,
    ObservationConfig,
    ObservationGap,
    ObservationRun,
    SeenBatch,
)
from .observation_store import ObservationStore
from .store import Conflict
from .raw_retention import RawRecordingExpired

# Strong ownership prevents an abandoned instance's lock being garbage-collected
# while one of its cancellation-resistant callbacks still runs in this process.
_active_gateways = {}


class ObservationSource(Protocol):
    source_ref: str
    simulated: bool

    async def open(
        self, config: ObservationConfig, run: ObservationRun, cursor: dict | None
    ) -> None: ...
    def entries(self) -> AsyncIterator[ObservationBatch | ObservationGap]: ...
    async def close(self) -> bool: ...


class ObservationRunner(Protocol):
    """Implemented by the native NanoBot adapter, never a second agent loop.

    observe must persist actual model evidence before returning SeenBatch.
    Tool execution checks is_current() immediately before side effects.
    abort/close return True only after owned inference resources are reclaimed.
    """

    inference_simulated: bool

    async def open(self, config: ObservationConfig, run: ObservationRun) -> None: ...
    async def observe(
        self,
        batch: ObservationBatch,
        gaps: list[dict],
        *,
        attempt_id: str,
        is_current: Callable[[], bool],
    ) -> SeenBatch: ...
    async def abort(self) -> bool: ...
    async def close(self) -> bool: ...


class AdapterTimeout(TimeoutError):
    def __init__(self, operation):
        self.operation = operation
        super().__init__("observation adapter timed out")


@dataclass(frozen=True)
class GatewayPolicy:
    inference_timeout: float = 10
    latest_after_seconds: float = 5
    opening_timeout: float = 60
    close_timeout: float = 3
    consecutive_failures: int = 3
    backfill_retry_seconds: float = 30
    analysis_restart_limit: int = 3
    analysis_restart_seconds: float = 5

    def __post_init__(self):
        for value in (
            self.inference_timeout,
            self.latest_after_seconds,
            self.opening_timeout,
            self.close_timeout,
            self.backfill_retry_seconds,
            self.analysis_restart_seconds,
        ):
            if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
                raise ValueError("Gateway time limits must be finite and positive")
        if type(self.consecutive_failures) is not int or not 1 <= self.consecutive_failures <= 10:
            raise ValueError("invalid consecutive failure limit")
        if type(self.analysis_restart_limit) is not int or not 0 <= self.analysis_restart_limit <= 10:
            raise ValueError("invalid analysis restart limit")


@dataclass
class _Owned:
    config: ObservationConfig
    run: ObservationRun
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    source: ObservationSource | None = None
    runner: ObservationRunner | None = None
    supervisor: asyncio.Task | None = None
    cleanup: asyncio.Task | None = None
    children: set = field(default_factory=set)
    attempt: str | None = None
    shutdown_confirmed: bool = False
    source_exhausted: bool = False
    analysis_task: asyncio.Task | None = None
    analysis_factory: object | None = None
    analysis_paused: bool = False
    recovering: bool = False
    next_backlog_check: float = 0
    backlog_since: float | None = None
    backlog_reported: bool = False
    analysis_restarts: int = 0
    unavailable_batches: set[str] = field(default_factory=set)


class ObservationGateway:
    def __init__(
        self,
        ledger: ObservationStore,
        source_factory,
        runner_factory,
        stop_motion,
        *,
        policy: GatewayPolicy = GatewayPolicy(),
        recover_resources=None,
        backfill_factory=None,
    ):
        self.ledger, self.store = ledger, ledger.store
        self.source_factory, self.runner_factory = source_factory, runner_factory
        self.stop_motion, self.policy = stop_motion, policy
        self.recover_resources = recover_resources
        self.backfill_factory = backfill_factory
        self.owned = {}
        self.recoveries = {}
        self.control_lock = asyncio.Lock()
        self.closing = False
        self.lock_released = False
        self.close_result = None
        self.instance_key = str(self.store.path.resolve())
        if self.instance_key in _active_gateways:
            raise Conflict("another observation Gateway still owns this state directory")
        self.owner_lock = (self.store.root / "observation-worker.lock").open("a")
        try:
            fcntl.flock(self.owner_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.owner_lock.close()
            raise Conflict("another observation Gateway still owns this state directory") from None
        _active_gateways[self.instance_key] = self

    def _audit(self, owner, kind, **payload):
        if kind == "observation.consumed" and owner.analysis_restarts:
            from .harness_health import RuntimeHealth

            owner.analysis_restarts = 0
            RuntimeHealth(self.store).record(
                owner.run.principal, "visual_worker", "healthy",
                task_id=owner.run.task_id, version=owner.run.version,
                generation=owner.run.generation, simulated=owner.source.simulated,
            )
        self.store.audit(
            owner.run.task_id,
            kind,
            {
                **owner.run.model_dump(),
                "session_ref": owner.config.session_ref,
                "source_simulated": getattr(owner.source, "simulated", None),
                "inference_simulated": getattr(owner.runner, "inference_simulated", None),
                "occurred_at": self.store.clock(),
                **payload,
            },
        )
        if kind in {"observation.failed", "observation.started"}:
            from .harness_health import RuntimeHealth

            with self.store.connect() as db:
                current = db.execute(
                    "SELECT version,generation,status FROM observation_tasks WHERE task_id=? AND principal=?",
                    (owner.run.task_id, owner.run.principal),
                ).fetchone()
            if current is None or (current["version"], current["generation"]) != (
                owner.run.version,
                owner.run.generation,
            ):
                return
            if kind == "observation.failed" and current["status"] != "paused":
                return  # a late exception must not replace a stop/new run with a fault
            health = RuntimeHealth(self.store)
            if kind == "observation.started":
                from .contracts import digest

                ident = (
                    "health-"
                    + digest([owner.run.principal, "observation_task", owner.run.task_id])[:32]
                )
                with self.store.connect() as db:
                    if (
                        db.execute("SELECT 1 FROM runtime_health WHERE id=?", (ident,)).fetchone()
                        is None
                    ):
                        return  # no need to persist healthy for every new task
            health.record(
                owner.run.principal,
                "observation_task",
                "task_failed" if kind == "observation.failed" else "healthy",
                task_id=owner.run.task_id,
                version=owner.run.version,
                generation=owner.run.generation,
                details={"error_type": payload.get("error_type")}
                if kind == "observation.failed"
                else {},
                simulated=getattr(owner.source, "simulated", False),
            )

    async def _report_backlog(self, owner):
        settings = getattr(self, "health_settings", None)
        if settings is None or getattr(getattr(owner.source, "binding", None), "finite", True):
            return
        now = self.store.clock()
        if now < owner.next_backlog_check:
            return
        owner.next_backlog_check = now + settings.health_backlog_check_seconds
        state = await asyncio.to_thread(self.ledger.get, owner.run.principal, owner.run.task_id)
        lag = state["ledger"]["unseen_media_ms"]
        from .harness_health import RuntimeHealth

        if lag >= settings.health_backlog_ms and not owner.analysis_paused:
            if owner.backlog_since is None:
                owner.backlog_since = now
            if (
                not owner.backlog_reported
                and now - owner.backlog_since >= settings.health_backlog_sustain_seconds
            ):
                RuntimeHealth(self.store).record(
                    owner.run.principal,
                    "visual_analysis",
                    "analysis_backlog",
                    task_id=owner.run.task_id,
                    version=owner.run.version,
                    generation=owner.run.generation,
                    details={"unseen_media_ms": lag, "sustained": True},
                    simulated=owner.source.simulated,
                )
                owner.backlog_reported = True
        else:
            owner.backlog_since = None
            if owner.backlog_reported:
                RuntimeHealth(self.store).record(
                    owner.run.principal,
                    "visual_analysis",
                    "healthy",
                    task_id=owner.run.task_id,
                    version=owner.run.version,
                    generation=owner.run.generation,
                    simulated=owner.source.simulated,
                )
                owner.backlog_reported = False

    def _released(self, task, *, capture_only=False):
        if task["generation"] == 0:
            return True
        with self.store.connect() as db:
            row = db.execute(
                """SELECT payload FROM audit WHERE task_id=?
                AND kind='observation.resources.closed' AND json_extract(payload,'$.generation')=?
                ORDER BY seq DESC LIMIT 1""",
                (task["task_id"], task["generation"]),
            ).fetchone()
        receipt = json.loads(row["payload"]) if row else {}
        if receipt.get("confirmed") is True:
            return True
        cfg = json.loads(task["config"]) if isinstance(task["config"], str) else task["config"]
        return bool(
            capture_only and cfg.get("output_mode") == "markers"
            and not cfg.get("ptz_scope_ref")
            and not cfg.get("skills")
            and not set(cfg.get("tools", ())) - {"mark_media", "read_media", "get_edge_state"}
            and all(receipt.get("confirmations", {}).get(k) is True for k in ("source", "motion"))
        )

    def _recovery_busy(self, task_id):
        probe = self.recoveries.get(task_id)
        return bool(probe and (probe.recovering or any(not task.done() for task in probe.children)))

    async def apply(self, config, *, request_id, expected_version=0):
        """Do not replace resource bindings while an old run is still closing."""
        async with self.control_lock:
            if self.closing:
                raise Conflict("observation Gateway is closing")
            try:
                old = self.ledger.get(config.principal, config.task_id)
            except KeyError:
                old = None
            with self.store.connect() as db:
                known = db.execute(
                    "SELECT 1 FROM observation_commands WHERE principal=? AND request_id=?",
                    (config.principal, request_id),
                ).fetchone()
            if known:
                return self.ledger.apply(
                    config, request_id=request_id, expected_version=expected_version
                )
            old_config = (
                ObservationConfig.model_validate(old["config"]).model_dump(mode="json")
                if old
                else None
            )
            if self._recovery_busy(config.task_id) and (
                not old or old_config != config.model_dump(mode="json")
            ):
                raise Conflict("resource recovery is still active")
            if old and not self._released(old) and old_config != config.model_dump(mode="json"):
                raise Conflict("configuration update requires confirmed resource shutdown")
            return self.ledger.apply(
                config, request_id=request_id, expected_version=expected_version
            )

    def _current(self, owner, attempt=None):
        return (
            not self.closing
            and (attempt is None or owner.attempt == attempt)
            and self.ledger.run_is_current(owner.run)
        )

    def _child(self, owner, coroutine):
        task = asyncio.create_task(coroutine)
        owner.children.add(task)

        def finished(value):
            owner.children.discard(value)
            if not value.cancelled():
                value.exception()  # retrieve eventual orphan errors

        task.add_done_callback(finished)
        return task

    async def _bounded(self, owner, coroutine, seconds):
        task = self._child(owner, coroutine)
        try:
            done, _ = await asyncio.wait({task}, timeout=seconds)
            if not done:
                task.cancel()
                raise AdapterTimeout(task)
            return task.result()
        except BaseException:
            if not task.done():
                task.cancel()
            raise

    def status(self, principal, task_id):
        task = self.ledger.get(principal, task_id)
        owner = self.owned.get(task_id)
        with self.store.connect() as db:
            row = db.execute(
                """SELECT payload FROM audit WHERE task_id=?
                AND kind='observation.resources.closed' AND json_extract(payload,'$.generation')=?
                ORDER BY seq DESC LIMIT 1""",
                (task_id, task["generation"]),
            ).fetchone()
        shutdown = json.loads(row["payload"]) if row else None
        return {
            "task": task,
            "worker_active": bool(owner and owner.supervisor and not owner.supervisor.done()),
            "source_simulated": getattr(owner.source, "simulated", None)
            if owner
            else (shutdown or {}).get("source_simulated"),
            "inference_simulated": getattr(owner.runner, "inference_simulated", None)
            if owner
            else (shutdown or {}).get("inference_simulated"),
            "recovery_pending": self._recovery_busy(task_id),
            "shutdown": shutdown,
        }

    async def control(self, command: ObservationCommand, *, _shutdown=False):
        """Start/stop are asynchronous; status distinguishes acceptance/readiness."""
        command = ObservationCommand.model_validate(command.model_dump())
        if command.action not in {"start", "resume", "pause", "stop"}:
            raise Conflict("internal lifecycle acknowledgement is not a user command")
        async with self.control_lock:
            if self.closing and not _shutdown:
                raise Conflict("observation Gateway is closing")
            before = self.ledger.get(command.principal, command.task_id)
            owner = self.owned.get(command.task_id)
            with self.store.connect() as db:
                known = db.execute(
                    "SELECT 1 FROM observation_commands WHERE principal=? AND request_id=?",
                    (command.principal, command.request_id),
                ).fetchone()
            would_start = (
                (command.action == "start" and before["status"] == "accepted")
                or (command.action == "resume" and before["status"] == "paused")
            ) and not known
            if would_start:
                if self._recovery_busy(command.task_id):
                    raise Conflict("resource recovery is still active")
                if (
                    owner
                    and before["status"] in {"accepted", "paused"}
                    and not owner.shutdown_confirmed
                    and not (
                        owner.cleanup and owner.cleanup.done()
                        and not any(not t.done() for t in owner.children)
                        and self._released(before, capture_only=True)
                    )
                ):
                    raise Conflict("previous observation resources have not confirmed shutdown")
                if not self._released(before, capture_only=True):
                    raise Conflict("previous observation requires confirmed resource recovery")
                config = ObservationConfig.model_validate(before["config"])
                if config.ptz_scope_ref:
                    with self.store.connect() as db:
                        others = [
                            dict(row)
                            for row in db.execute(
                                """SELECT * FROM observation_tasks
                            WHERE task_id!=? AND generation>0 AND json_extract(config,'$.device_ref')=?
                            AND json_extract(config,'$.ptz_scope_ref')=?""",
                                (command.task_id, config.device_ref, config.ptz_scope_ref),
                            )
                        ]
                    for other in others:
                        if not self._released(other):
                            raise Conflict("gimbal scope is owned by another observation")
            result = self.ledger.control(command)
            if command.action in {"start", "resume"} and result["changed"]:
                task = result["task"]
                owner = _Owned(
                    ObservationConfig.model_validate(task["config"]),
                    ObservationRun(
                        principal=command.principal,
                        task_id=command.task_id,
                        version=task["version"],
                        generation=task["generation"],
                    ),
                )
                self.owned[command.task_id] = owner
                self._audit(owner, "observation.start.accepted")
                owner.supervisor = asyncio.create_task(self._run(owner))
            elif (
                command.action in {"pause", "stop"}
                and owner
                and result["task"]["status"] in {"paused", "stopped"}
                and owner.run.generation == command.generation == result["task"]["generation"]
                and owner.run.version == command.version == result["task"]["version"]
            ):
                owner.attempt = None  # fence tools before awaiting model/driver cancellation
                if owner.supervisor and not owner.supervisor.done():
                    owner.supervisor.cancel()
                self._ensure_cleanup(owner, retry=True)
            elif (
                command.action in {"start", "resume"}
                and owner is None
                and before["status"] in {"starting", "running"}
            ):
                raise Conflict("interrupted observation requires explicit startup recovery")
            return {**result, **{"runtime": self.status(command.principal, command.task_id)}}

    async def recover(self, principal, task_id):
        """Called by a host after acquiring its exclusive worker lock.

        Former running/starting tasks become paused. Adapters must verify old
        process/device cleanup before a new resume; stopped tasks stay stopped.
        """
        async with self.control_lock:
            if self.closing:
                raise Conflict("observation Gateway is closing")
            if task_id in self.owned:
                raise Conflict("cannot recover a locally owned run")
            if self._recovery_busy(task_id):
                raise Conflict("resource recovery is still active")
            task = self.ledger.get(principal, task_id)
            if task["status"] in {"starting", "running"}:
                self.ledger.control(
                    ObservationCommand(
                        principal=principal,
                        task_id=task_id,
                        version=task["version"],
                        generation=task["generation"],
                        request_id="recover-" + uuid.uuid4().hex,
                        action="recover",
                        reason="host restarted; resource recovery required before resume",
                    )
                )
            if not task["generation"] or self._released(task) or not self.recover_resources:
                return self.status(principal, task_id)
            run = ObservationRun(
                principal=principal,
                task_id=task_id,
                version=task["version"],
                generation=task["generation"],
            )
            probe = _Owned(ObservationConfig.model_validate(task["config"]), run, recovering=True)
            self.recoveries[task_id] = probe
        confirmed = False
        result = {}
        try:
            result = await self._bounded(
                probe, self.recover_resources(probe.config, run), self.policy.close_timeout
            )
            confirmed = (
                isinstance(result, dict)
                and all(result.get(key) is True for key in ("source", "model", "motion"))
                and all(
                    type(result.get(key)) is bool
                    for key in ("source_simulated", "inference_simulated", "motion_simulated")
                )
            )
        except Exception:
            confirmed = False
        finally:
            # Retain timed-out cancellation-resistant operations. Neither a
            # second recovery nor resume/rebind may outrun their completion.
            probe.recovering = False
            confirmed = confirmed and not any(not child.done() for child in probe.children)
            self._audit(
                probe,
                "observation.resources.closed",
                confirmed=confirmed,
                confirmations=result,
                recovered=True,
            )
        return self.status(principal, task_id)

    async def control_analysis(self, command):
        """Pause/retry just the visual consumer; preserve its recording producer."""
        from .harness_health import RuntimeHealth

        async with self.control_lock:
            task = self.ledger.get(command.principal, command.task_id)
            owner = self.owned.get(command.task_id)
            if (
                not owner
                or task["status"] != "running"
                or (task["version"], task["generation"]) != (command.version, command.generation)
                or not owner.analysis_factory
            ):
                raise Conflict("analysis control requires the current continuous recording run")
            with self.store.connect() as db:
                old = db.execute(
                    "SELECT payload FROM audit WHERE task_id=? AND kind='observation.analysis.control' AND json_extract(payload,'$.request_id')=?",
                    (command.task_id, command.request_id),
                ).fetchone()
            if old:
                body = json.loads(old[0])
                if body["command"] != command.model_dump(mode="json"):
                    raise Conflict("analysis command identity reused")
                return {
                    **body["receipt"],
                    "duplicate": True,
                    "currently_paused": owner.analysis_paused,
                }
            owner.attempt = None
            if owner.analysis_task and not owner.analysis_task.done():
                owner.analysis_task.cancel()
                _, pending = await asyncio.wait(
                    {owner.analysis_task}, timeout=self.policy.close_timeout
                )
                if pending:
                    raise Conflict("analysis cancellation pending; recording continues")
                await asyncio.gather(owner.analysis_task, return_exceptions=True)
            if (
                await self._bounded(owner, owner.runner.abort(), self.policy.close_timeout)
                is not True
                or await self._bounded(owner, owner.runner.close(), self.policy.close_timeout)
                is not True
            ):
                raise Conflict("old analysis resource release not confirmed; recording continues")
            owner.analysis_paused = command.action == "pause"
            if command.action == "resume":
                owner.runner = self.runner_factory(owner.config)
                owner.analysis_task = self._child(owner, owner.analysis_factory())
            RuntimeHealth(self.store).record(
                command.principal,
                "visual_worker",
                "paused" if owner.analysis_paused else "restarting",
                task_id=command.task_id,
                version=command.version,
                generation=command.generation,
                simulated=owner.source.simulated,
            )
            receipt = {
                "accepted": True,
                "scope": "analysis",
                "action": command.action,
                "task_id": command.task_id,
                "generation": command.generation,
                "recording_continues": True,
                "analysis_ready": False,
                "duplicate": False,
            }
            self._audit(
                owner,
                "observation.analysis.control",
                request_id=command.request_id,
                command=command.model_dump(mode="json"),
                receipt=receipt,
            )
            return receipt

    async def wait_ready(self, principal, task_id, timeout=10):
        self.ledger.get(principal, task_id)
        owner = self.owned.get(task_id)
        if owner:
            await asyncio.wait_for(owner.ready.wait(), timeout)
        return self.status(principal, task_id)

    async def recheck_source_release(self, principal, source_ref):
        """A source stop ACK may retry cleanup, never activate a task or model."""
        with self.store.connect() as db:
            tasks = [
                dict(row)
                for row in db.execute(
                    """SELECT * FROM observation_tasks WHERE principal=?
                AND status IN ('paused','stopped') AND json_extract(config,'$.source_ref')=?""",
                    (principal, source_ref),
                )
            ]
        for task in tasks:
            if self._released(task):
                continue
            owner = self.owned.get(task["task_id"])
            if owner is not None:
                self._ensure_cleanup(owner, retry=True)
            elif not self._recovery_busy(task["task_id"]):
                await self.recover(principal, task["task_id"])

    async def _run(self, owner):
        try:
            owner.source = self.source_factory(owner.config)
            owner.runner = self.runner_factory(owner.config)
            if owner.source.source_ref != owner.config.source_ref:
                raise Conflict("source adapter differs from task binding")
            if type(owner.source.simulated) is not bool:
                raise Conflict("source must declare its simulation boundary")
            if type(owner.runner.inference_simulated) is not bool:
                raise Conflict("runner must declare whether inference is simulated")
            cursor = self.ledger.get(owner.run.principal, owner.run.task_id)["ledger"][
                "latest_ingested"
            ]
            await self._bounded(
                owner,
                owner.source.open(owner.config, owner.run, cursor),
                self.policy.opening_timeout,
            )
            live_capture = bool(getattr(getattr(owner.source, "driver", None), "capture", None))

            async def open_runner():
                await self._bounded(
                    owner, owner.runner.open(owner.config, owner.run), self.policy.opening_timeout
                )
                from .harness_health import RuntimeHealth

                RuntimeHealth(self.store).record(
                    owner.run.principal,
                    "visual_worker",
                    "analysis_recovering" if owner.analysis_restarts else "healthy",
                    task_id=owner.run.task_id,
                    version=owner.run.version,
                    generation=owner.run.generation,
                    simulated=owner.source.simulated,
                )

            if not live_capture:
                await open_runner()

            self.ledger.control(
                ObservationCommand(
                    **owner.run.model_dump(),
                    action="confirm_started",
                    request_id="started-" + uuid.uuid4().hex,
                )
            )
            self._audit(
                owner, "observation.started", inference_simulated=owner.runner.inference_simulated
            )
            producer = self._child(owner, self._collect(owner))

            async def consume():
                if not live_capture:
                    return await self._consume(owner)
                from .observation_analysis_recovery import consume_with_recovery

                return await consume_with_recovery(self, owner, open_runner)

            consumer = self._child(owner, consume())
            if live_capture:
                owner.analysis_task = consumer
                owner.analysis_factory = consume
            owner.ready.set()
            if live_capture:
                await producer
                if not getattr(owner.source, "complete_task_on_end", False):
                    if getattr(owner.source.driver.capture, "pause_on_end", False):
                        owner.source_exhausted = True
                        owner.changed.set()
                        await consumer
                        pending = self.ledger.get(owner.run.principal, owner.run.task_id)["ledger"][
                            "unseen_count"
                        ]
                        self.ledger.control(
                            ObservationCommand(
                                **owner.run.model_dump(),
                                action="pause",
                                request_id="paired-ended-" + uuid.uuid4().hex,
                                reason="analysis_incomplete_after_input"
                                if pending
                                else "paired_input_disconnected",
                            )
                        )
                        return
                    raise RuntimeError("continuous recording input ended")
            done, _ = await asyncio.wait({producer, consumer}, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if producer in done and not consumer.done():
                # A finite replay still gets its final available window judged.
                owner.source_exhausted = True
                owner.changed.set()
                await consumer
            if producer in done and getattr(owner.source, "complete_task_on_end", False):
                task = self.ledger.get(owner.run.principal, owner.run.task_id)
                timestamp_gaps = False
                if live_capture and task["ledger"]["unresolved_gap_count"]:
                    with self.store.connect() as db:
                        gaps = db.execute(
                            "SELECT gap_reason,end_ms-start_ms AS span FROM observation_media WHERE task_id=? AND kind='gap'",
                            (owner.run.task_id,),
                        ).fetchall()
                    timestamp_gaps = bool(gaps) and all(
                        r["gap_reason"] == "container_timestamp_gap" and r["span"] <= 1
                        for r in gaps
                    )
                if task["ledger"]["unseen_media_ms"] or (
                    task["ledger"]["unresolved_gap_count"] and not timestamp_gaps
                ):
                    # Ending an authorized capture interval is not a claim that
                    # old interrupted runs were fully recorded or analysed.
                    # Strict finite-file replay still requires complete coverage.
                    if (
                        live_capture
                        and owner.config.capture_until is not None
                        and self.store.clock() >= owner.config.capture_until
                        and self.ledger.unseen_in_run(owner.run) == 0
                    ):
                        self.ledger.control(
                            ObservationCommand(
                                **owner.run.model_dump(), action="stop",
                                request_id="capture-ended-" + uuid.uuid4().hex,
                                reason="capture_deadline_ended_with_gaps",
                            )
                        )
                        self._audit(
                            owner, "observation.capture.ended",
                            current_run_drained=True, coverage_complete=False,
                            unseen_media_ms=task["ledger"]["unseen_media_ms"],
                            missing_media_ms=task["ledger"]["missing_media_ms"],
                            unresolved_gap_count=task["ledger"]["unresolved_gap_count"],
                        )
                        return
                    raise RuntimeError("finite replay ended before complete media coverage")
                self.ledger.control(
                    ObservationCommand(
                        **owner.run.model_dump(),
                        action="stop",
                        request_id="replay-ended-" + uuid.uuid4().hex,
                        reason="configured_replay_completed_with_timestamp_gaps"
                        if timestamp_gaps
                        else "capture_deadline_drained"
                        if owner.config.capture_until is not None
                        else "configured_replay_completed",
                    )
                )
                self._audit(
                    owner,
                    "observation.replay.completed",
                    media_entries=task["ledger"]["entry_count"],
                    missing_media_ms=task["ledger"]["missing_media_ms"],
                    coverage_complete=not task["ledger"]["unresolved_gap_count"],
                )
                return
            raise RuntimeError("observation input ended; continuous task did not complete")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            task = self.ledger.get(owner.run.principal, owner.run.task_id)
            if (
                task["status"] in {"starting", "running"}
                and task["generation"] == owner.run.generation
                and task["version"] == owner.run.version
            ):
                self.ledger.control(
                    ObservationCommand(
                        **owner.run.model_dump(),
                        action="stop" if task["deadline_reached"] else "fail",
                        request_id="failed-" + uuid.uuid4().hex,
                        reason="deadline_reached"
                        if task["deadline_reached"]
                        else type(exc).__name__,
                    )
                )
            self._audit(owner, "observation.failed", error_type=type(exc).__name__,
                        error_origin=[
                            {"module": Path(frame.filename).name, "function": frame.name,
                             "line": frame.lineno}
                            for frame in traceback.extract_tb(exc.__traceback__)
                        ])
        finally:
            owner.attempt = None
            owner.ready.set()
            self._ensure_cleanup(owner)

    async def _collect(self, owner):
        stream = getattr(owner.source, "sample_stream", None)
        if stream:
            stream.notify = owner.changed.set
        async for entry in owner.source.entries():
            if not self._current(owner):
                raise Conflict("subscription no longer owns current run")
            if not isinstance(entry, (ObservationBatch, ObservationGap)):
                raise TypeError("source must emit a batch or an explicit gap")
            if (
                any(getattr(entry, key) != value for key, value in owner.run.model_dump().items())
                or entry.source_ref != owner.config.source_ref
            ):
                raise Conflict("captured entry differs from subscription binding")
            if isinstance(entry, ObservationBatch):
                if entry.simulated != owner.source.simulated:
                    raise Conflict("capture simulation label mismatch")
                from .sqlite_writes import busy
                while self._current(owner):
                    try:
                        from .observation_staged import joined_thread
                        await joined_thread(lambda: self.ledger.append_batch(entry), owner.children)
                        break
                    except Exception as exc:
                        if not busy(exc):
                            raise
                        await asyncio.sleep(.1)
                else:
                    return
                if entry.batch_id in owner.unavailable_batches:
                    from .observation_analysis_recovery import defer_unavailable
                    defer_unavailable(self, owner, entry.batch_id)
            elif isinstance(entry, ObservationGap):
                self.ledger.record_gap(entry)
            self._audit(owner, "observation.input", entry=entry.model_dump(mode="json"))
            owner.changed.set()
            await self._report_backlog(owner)

    async def _consume(self, owner):
        if getattr(owner.runner, "pipeline_capacity", 0):
            from .observation_staged import consume_staged

            return await consume_staged(self, owner)
        failures = 0
        while self._current(owner):
            owner.changed.clear()
            selected = self.ledger.next_live(
                owner.run,
                latest_after_seconds=self.policy.latest_after_seconds,
                preserve_order=bool(
                    getattr(owner.source, "processing_paced", False)
                    or getattr(owner.source, "pacing", None) == "processing"
                ),
            )
            if selected is None:
                historical = (
                    self.ledger.next_backfill(owner.run, source_exhausted=owner.source_exhausted)
                    if self.backfill_factory
                    else None
                )
                if historical is not None:
                    await self._backfill(owner, ObservationBatch.model_validate(historical))
                    continue
                if owner.source_exhausted:
                    return
                # A short wait also checks expiration when the source is silent.
                try:
                    await asyncio.wait_for(owner.changed.wait(), 0.25)
                except TimeoutError:
                    pass
                continue
            batch = ObservationBatch.model_validate(selected["batch"])
            attempt = "observation-" + uuid.uuid4().hex
            owner.attempt = attempt
            started = self.store.clock()
            started_monotonic = time.monotonic()
            self._audit(
                owner,
                "observation.dispatch",
                batch_id=batch.batch_id,
                attempt_id=attempt,
                captured_at=batch.captured_at,
                queue_seconds=started - batch.captured_at,
                gaps=selected["gaps"],
                inference_simulated=owner.runner.inference_simulated,
            )
            try:
                notices = selected.get("backfill_results", [])
                kwargs = {"backfill_results": notices} if notices else {}
                receipt = await self._bounded(
                    owner,
                    owner.runner.observe(
                        batch,
                        selected["gaps"],
                        attempt_id=attempt,
                        is_current=lambda expected=attempt: self._current(owner, expected),
                        **kwargs,
                    ),
                    self.policy.inference_timeout,
                )
                receipt = SeenBatch.model_validate(receipt.model_dump())
                if (
                    any(
                        getattr(receipt, key) != value
                        for key, value in owner.run.model_dump().items()
                    )
                    or receipt.batch_id != batch.batch_id
                    or receipt.session_ref != owner.config.session_ref
                    or receipt.mode != "live"
                    or receipt.inference_simulated != owner.runner.inference_simulated
                ):
                    raise Conflict("model consumption receipt differs from dispatched input")
                if not self._current(owner, attempt):
                    raise Conflict("late observation result")
                self.ledger.mark_seen(receipt, backfill_result_ids=[r["id"] for r in notices])
                acknowledge = getattr(owner.source, "acknowledge", None)
                if acknowledge:
                    acknowledge(batch)
                self._audit(
                    owner,
                    "observation.consumed",
                    receipt=receipt.model_dump(),
                    processing_seconds=time.monotonic() - started_monotonic,
                    wall_processing_seconds=self.store.clock() - started,
                    end_to_end_seconds=self.store.clock() - batch.captured_at,
                )
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                owner.attempt = None
                if not self._current(owner):
                    raise
                self.ledger.defer_batch(
                    DeferredBatch(
                        **owner.run.model_dump(),
                        batch_id=batch.batch_id,
                        request_id="deferred-" + uuid.uuid4().hex,
                        reason="unanalysed_expired"
                        if isinstance(exc, RawRecordingExpired)
                        else "inference_failed:" + type(exc).__name__,
                    )
                )
                self._audit(
                    owner,
                    "observation.deferred",
                    batch_id=batch.batch_id,
                    error_type=type(exc).__name__,
                )
                if (
                    await self._bounded(owner, owner.runner.abort(), self.policy.close_timeout)
                    is not True
                ):
                    raise RuntimeError("inference cancellation not confirmed")
                if isinstance(exc, AdapterTimeout) and not exc.operation.done():
                    _, pending = await asyncio.wait(
                        {exc.operation}, timeout=self.policy.close_timeout
                    )
                    if pending:
                        raise RuntimeError("old inference invocation is still active")
                failures += 0 if isinstance(exc, RawRecordingExpired) else 1
                acknowledge = getattr(owner.source, "acknowledge", None)
                if acknowledge:
                    acknowledge(batch)  # Attempt ended; deferred debt remains explicit.
                if failures >= self.policy.consecutive_failures:
                    raise RuntimeError("consecutive observation failures")
            finally:
                owner.attempt = None

    async def _backfill(self, owner, batch):
        """Lend this task's single inference slot; fresh input preempts old work.

        Closing the live adapter persists its native history. Restoring it opens
        the same live session, never inserts historical AV as new live footage.
        Resource reclamation is required before either context can be replaced.
        """
        attempt = "backfill-" + uuid.uuid4().hex
        owner.attempt = attempt

        async def work():
            if (
                await self._bounded(owner, owner.runner.close(), self.policy.close_timeout)
                is not True
            ):
                raise RuntimeError("live model did not release its backfill slot")
            if not self._current(owner, attempt):
                raise Conflict("backfill superseded before open")
            owner.runner = self.backfill_factory(owner.config, batch)
            await self._bounded(
                owner, owner.runner.open(owner.config, owner.run), self.policy.opening_timeout
            )
            if self.ledger.has_live_pending(owner.run):
                raise Conflict("fresh input preempts historical work")
            return await owner.runner.observe(
                batch,
                [],
                attempt_id=attempt,
                is_current=lambda: (
                    self._current(owner, attempt) and not self.ledger.has_live_pending(owner.run)
                ),
            )

        existing_children = set(owner.children)
        operation = self._child(owner, work())
        wake = None
        started = time.monotonic()
        reason = None
        self._audit(
            owner,
            "observation.backfill.dispatched",
            batch_id=batch.batch_id,
            attempt_id=attempt,
            instruction_version=batch.version,
            source_ref=batch.source_ref,
        )
        try:
            while not operation.done():
                if self.ledger.has_live_pending(owner.run):
                    reason = "fresh_input"
                    break
                remaining = self.policy.inference_timeout - (time.monotonic() - started)
                if remaining <= 0:
                    reason = "timeout"
                    break
                owner.changed.clear()
                wake = asyncio.create_task(owner.changed.wait())
                await asyncio.wait(
                    {operation, wake},
                    timeout=min(remaining, 0.25),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                wake.cancel()
                await asyncio.gather(wake, return_exceptions=True)
                wake = None
            if reason is not None:
                owner.attempt = None
                operation.cancel()
                if (
                    await self._bounded(owner, owner.runner.abort(), self.policy.close_timeout)
                    is not True
                ):
                    raise RuntimeError("historical inference cancellation not confirmed")
                _, pending = await asyncio.wait({operation}, timeout=self.policy.close_timeout)
                if pending:
                    raise RuntimeError("historical invocation remains active")
                self.ledger.retry_backfill(
                    owner.run, batch.batch_id, reason, self.policy.backfill_retry_seconds
                )
                self._audit(
                    owner, "observation.backfill.deferred", batch_id=batch.batch_id, reason=reason
                )
            else:
                receipt = SeenBatch.model_validate(operation.result().model_dump())
                if (
                    any(
                        getattr(receipt, key) != value
                        for key, value in owner.run.model_dump().items()
                    )
                    or receipt.batch_id != batch.batch_id
                    or receipt.mode != "backfill"
                    or receipt.session_ref
                    != owner.config.session_ref + f":history:v{batch.version}"
                    or receipt.inference_simulated != owner.runner.inference_simulated
                ):
                    raise Conflict("historical receipt differs from dispatched input")
                if not self._current(owner, attempt):
                    raise Conflict("late historical result")
                self.ledger.mark_seen(receipt)
                self._audit(
                    owner,
                    "observation.backfill.consumed",
                    receipt=receipt.model_dump(),
                    processing_seconds=time.monotonic() - started,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # An unconfirmed resource release is a task failure, never a reason
            # to create another model alongside the old computation.
            if not operation.done():
                raise
            if self._current(owner):
                self.ledger.retry_backfill(
                    owner.run,
                    batch.batch_id,
                    type(exc).__name__,
                    self.policy.backfill_retry_seconds,
                )
                self._audit(
                    owner,
                    "observation.backfill.failed",
                    batch_id=batch.batch_id,
                    error_type=type(exc).__name__,
                )
        finally:
            if wake:
                wake.cancel()
            owner.attempt = None
            if not operation.done():
                operation.cancel()
        if not self._current(owner):
            return
        if await self._bounded(owner, owner.runner.close(), self.policy.close_timeout) is not True:
            raise RuntimeError("historical resources did not release the live slot")
        # The timeout wrapper can leave a cancellation-resistant open child.
        pending = {child for child in owner.children - existing_children if not child.done()}
        if pending:
            _, pending = await asyncio.wait(pending, timeout=self.policy.close_timeout)
        if pending:
            raise RuntimeError("historical callbacks have not ended")
        # A cancellation-resistant open can finish after the first close and
        # reacquire resources. Only a post-drain close can confirm ownership is
        # actually relinquished before constructing a replacement.
        if await self._bounded(owner, owner.runner.close(), self.policy.close_timeout) is not True:
            raise RuntimeError("late historical resources were not reclaimed")
        if not self._current(owner):
            return
        owner.runner = self.runner_factory(owner.config)
        await self._bounded(
            owner, owner.runner.open(owner.config, owner.run), self.policy.opening_timeout
        )

    def _ensure_cleanup(self, owner, *, retry=False):
        if owner.cleanup is None or (
            retry and owner.cleanup.done() and not owner.shutdown_confirmed
        ):
            owner.cleanup = asyncio.create_task(self._cleanup(owner))
        return owner.cleanup

    async def _cleanup(self, owner):
        owner.attempt = None
        owner.ready.set()
        for child in list(owner.children):
            child.cancel()
        results = {}

        async def close_one(name, operation):
            try:
                results[name] = (
                    await self._bounded(owner, operation, self.policy.close_timeout) is True
                )
            except (Exception, asyncio.CancelledError):
                results[name] = False
                adapter = owner.source if name == "source" else owner.runner
                draining = getattr(adapter, "cleanup", None)
                if isinstance(draining, asyncio.Task) and not draining.done():

                    def released_later(_):
                        if self.closing or owner.shutdown_confirmed:
                            return
                        current = owner.cleanup
                        if current is not None and not current.done():
                            current.add_done_callback(
                                lambda _: self._ensure_cleanup(owner, retry=True)
                            )
                        else:
                            self._ensure_cleanup(owner, retry=True)

                    draining.add_done_callback(released_later)

        # Stop motion independently of inference; cancelling an LLM is not a driver ACK.
        try:
            motion = await self._bounded(
                owner, self.stop_motion(owner.config, owner.run), self.policy.close_timeout
            )
            results["motion"] = (
                isinstance(motion, dict)
                and motion.get("confirmed") is True
                and type(motion.get("simulated")) is bool
            )
        except (Exception, asyncio.CancelledError):
            motion = None
            results["motion"] = False
        closers = []
        if owner.source:
            closers.append(close_one("source", owner.source.close()))
        if owner.runner:
            closers.append(close_one("model", owner.runner.close()))
        if closers:
            await asyncio.gather(*closers)
        remaining = {child for child in owner.children if not child.done()}
        if remaining:
            _, pending = await asyncio.wait(remaining, timeout=self.policy.close_timeout)
        else:
            pending = set()
        owner.shutdown_confirmed = all(results.values()) and not pending
        self._audit(
            owner,
            "observation.resources.closed",
            confirmed=owner.shutdown_confirmed,
            confirmations=results,
            motion_receipt=motion,
            pending_operations=len(pending),
        )

    async def close(self):
        """Orderly service shutdown pauses owned work; it never completes it."""
        if self.lock_released:
            return dict(self.close_result)
        self.closing = True
        for probe in self.recoveries.values():
            for child in list(probe.children):
                child.cancel()
        for owner in list(self.owned.values()):
            task = self.ledger.get(owner.run.principal, owner.run.task_id)
            if task["status"] in {"starting", "running"}:
                await self.control(
                    ObservationCommand(
                        **owner.run.model_dump(),
                        action="pause",
                        request_id="shutdown-" + uuid.uuid4().hex,
                        reason="service_shutdown",
                    ),
                    _shutdown=True,
                )
            else:
                if owner.supervisor and not owner.supervisor.done():
                    owner.supervisor.cancel()
                self._ensure_cleanup(owner, retry=True)
        if self.owned:
            await asyncio.gather(*(owner.cleanup for owner in self.owned.values() if owner.cleanup))
        pending = {
            child
            for owner in [*self.owned.values(), *self.recoveries.values()]
            for child in owner.children
            if not child.done()
        }
        if pending:
            _, pending = await asyncio.wait(pending, timeout=self.policy.close_timeout)
        # The parent recovery coroutine can still be committing its outcome.
        busy = any(probe.recovering for probe in self.recoveries.values())
        if not pending and not busy:
            fcntl.flock(self.owner_lock, fcntl.LOCK_UN)
            self.owner_lock.close()
            self.lock_released = True
            if _active_gateways.get(self.instance_key) is self:
                del _active_gateways[self.instance_key]
        confirmed = (
            not pending
            and not busy
            and all(owner.shutdown_confirmed for owner in self.owned.values())
            and all(
                self._released(self.ledger.get(probe.run.principal, probe.run.task_id))
                for probe in self.recoveries.values()
            )
        )
        self.close_result = {
            "confirmed": confirmed,
            "lock_released": self.lock_released,
            "pending_callbacks": len(pending),
            "recovery_committing": busy,
        }
        return dict(self.close_result)
