"""Persistent default-task policy, using the existing observation lifecycle.

The host owns one clock loop. This policy does not create user messages, models,
new observation identities, or a second scheduler service.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, model_validator

from .contracts import Contract, canonical, digest
from .observation_contracts import ObservationCommand, ObservationConfig, Reference
from .store import Conflict


class ObservationHours(Contract):
    timezone: str = Field(min_length=1, max_length=80)
    weekdays: tuple[int, ...] = Field(default=(0, 1, 2, 3, 4, 5, 6), min_length=1, max_length=7)
    start: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")

    @model_validator(mode="after")
    def valid(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            raise ValueError("unknown schedule timezone") from None
        if len(set(self.weekdays)) != len(self.weekdays) or any(
            type(day) is not int or not 0 <= day <= 6 for day in self.weekdays
        ):
            raise ValueError("weekdays must be distinct integers from Monday=0 to Sunday=6")
        if self.start == self.end:
            raise ValueError("empty hours; omit hours for continuous operation")
        return self

    def includes(self, timestamp):
        now = datetime.fromtimestamp(timestamp, ZoneInfo(self.timezone))
        minute = now.strftime("%H:%M")
        if self.start < self.end:
            return now.weekday() in self.weekdays and self.start <= minute < self.end
        # An overnight interval belongs to its start day.
        return (now.weekday() in self.weekdays and minute >= self.start) or (
            (now.weekday() - 1) % 7 in self.weekdays and minute < self.end
        )


class ConfigureDefaultObservation(Contract):
    task_id: Reference
    task_version: int = Field(strict=True, ge=1)
    expected_revision: int = Field(strict=True, ge=0)
    enabled: bool
    hours: ObservationHours | None = None


class DefaultObservationRequest(Contract):
    request_id: Reference
    configuration: ConfigureDefaultObservation


class ReadDefaultObservation(Contract):
    pass


class DefaultObservation:
    def __init__(self, host):
        self.host, self.store = host, host.store
        self.lock = asyncio.Lock()
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS observation_defaults (
                    principal TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    body TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS observation_default_receipts (
                    principal TEXT NOT NULL,request_id TEXT NOT NULL,body TEXT NOT NULL,
                    receipt TEXT NOT NULL,PRIMARY KEY(principal,request_id));
            """)

    def read(self, principal):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM observation_defaults WHERE principal=?", (principal,)
            ).fetchone()
        if not row:
            return {"revision": 0, "configuration": None, "os_boot_verified": False}
        config = ConfigureDefaultObservation.model_validate_json(row["body"])
        runtime = self.host.observations._installed().gateway.status(principal, config.task_id)
        waiting_for_input = self._waiting_for_input(principal, runtime["task"])
        proof = self.store.meta("observation_boot_verification:" + principal, {})
        verified = bool(
            proof.get("passed")
            and proof.get("task_id") == config.task_id
            and proof.get("task_version") == config.task_version
            and proof.get("source_digest")
            and proof.get("source_digest") == getattr(self.host, "source_digest", None)
        )
        return {
            "revision": row["revision"],
            "configuration": config.model_dump(mode="json"),
            "runtime": runtime,
            "waiting_for_input": waiting_for_input,
            "os_boot_verified": verified,
            "last_boot_test": proof if verified else None,
        }

    def _waiting_for_input(self, principal, task):
        if task["status"] not in {"accepted", "paused"}:
            return False
        source_ref = task["config"]["source_ref"]
        try:
            source = self.host.input_media.source(principal, source_ref)
        except KeyError:
            return False
        return bool(
            source["spec"].get("wait_for_client", False)
            and not self.host.input_media.client_connected(principal, source_ref)
        )

    def _probe(self, principal):
        """Read only the current policy/task snapshot, not the UI's full report.

        One joined SELECT keeps policy and task consistent without a read BEGIN
        (which the shared connection wrapper would serialize with writers).
        This synchronous probe runs in a worker thread, never on the frame loop.
        """
        with self.store.connect() as db:
            row = db.execute(
                """SELECT d.revision,d.body,t.task_id,t.version,t.generation,
                    t.status,t.config,t.reason,t.last_control
                FROM observation_defaults d LEFT JOIN observation_tasks t
                  ON t.principal=d.principal AND t.task_id=json_extract(d.body,'$.task_id')
                WHERE d.principal=?""", (principal,),
            ).fetchone()
        if row is None:
            return None
        cfg = ConfigureDefaultObservation.model_validate_json(row["body"])
        if row["task_id"] is None:
            raise KeyError(cfg.task_id)
        task = {key: row[key] for key in (
            "task_id", "version", "generation", "status", "reason",
        )}
        task["config"] = json.loads(row["config"])
        task["last_control"] = json.loads(row["last_control"]) if row["last_control"] else {}
        retry_key = "default_observation_retry:" + digest([principal, cfg.task_id, cfg.task_version])[:32]
        return {
            "revision": row["revision"], "configuration": cfg, "task": task,
            "waiting_for_input": self._waiting_for_input(principal, task),
            "retry_key": retry_key,
            "attempt": self.store.meta(retry_key, {"attempts": 0, "next_try": 0}),
        }

    async def apply(self, principal, request):
        request = DefaultObservationRequest.model_validate(request.model_dump())
        async with self.lock, self.host.observations.command_lock:
            installed = self.host.observations._installed()
            cfg = request.configuration
            with self.store.connect() as db:
                previous = db.execute(
                    "SELECT * FROM observation_default_receipts WHERE principal=? AND request_id=?",
                    (principal, request.request_id),
                ).fetchone()
            if previous:
                if previous["body"] != canonical(request):
                    raise Conflict("default policy request identity conflict")
                return {**json.loads(previous["receipt"]), "current": self.read(principal)}
            task = installed.gateway.status(principal, cfg.task_id)["task"]
            if task["version"] != cfg.task_version or (cfg.enabled and task["status"] == "stopped"):
                raise Conflict("default policy requires the current, non-stopped task version")
            if cfg.enabled:
                result = installed.validate(ObservationConfig.model_validate(task["config"]))
                if inspect.isawaitable(result):
                    await result
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT * FROM observation_defaults WHERE principal=?", (principal,)
                ).fetchone()
                revision = row["revision"] if row else 0
                if revision != cfg.expected_revision:
                    raise Conflict("default policy revision changed; inspect before applying")
                if row and json.loads(row["body"])["task_id"] != cfg.task_id:
                    old = installed.gateway.status(principal, json.loads(row["body"])["task_id"])[
                        "task"
                    ]
                    if old["status"] in {"running", "starting"}:
                        raise Conflict("disable the previous default task before replacing it")
                revision += 1
                receipt = {
                    "request_id": request.request_id,
                    "applied": True,
                    "revision": revision,
                    "task_id": cfg.task_id,
                    "task_version": cfg.task_version,
                    "running_confirmed": False,
                    "trigger": "configuration",
                }
                db.execute(
                    "INSERT OR REPLACE INTO observation_defaults VALUES (?,?,?,?)",
                    (principal, revision, canonical(cfg), self.store.clock()),
                )
                db.execute(
                    "INSERT INTO observation_default_receipts VALUES (?,?,?,?)",
                    (principal, request.request_id, canonical(request), canonical(receipt)),
                )
                self.store._audit(db, None, "observation.default.applied", receipt)
            try:
                await self._tick(principal, explicit=True)
            except (Conflict, ValueError, RuntimeError) as exc:
                # Saving a policy and starting a device are distinct outcomes.
                return {
                    **receipt,
                    "execution_error": type(exc).__name__,
                    "current": self.read(principal),
                }
            return {**receipt, "current": self.read(principal)}

    async def tick(self):
        if self.host.side != "edge" or self.host.observations.installation is None:
            return
        async with self.lock, self.host.observations.command_lock:
            await self._tick("local-user")

    async def _tick(self, principal, *, explicit=False):
        state = await asyncio.to_thread(self._probe, principal)
        if state is None:
            return
        cfg = state["configuration"]
        task = state["task"]
        if task["version"] != cfg.task_version or task["status"] == "stopped":
            return
        retry_key, attempt = state["retry_key"], state["attempt"]
        if task["status"] == "running" and attempt["attempts"]:
            attempt = {"attempts": 0, "next_try": 0}
            self.store.set_meta(retry_key, attempt)
        eligible = cfg.enabled and (cfg.hours is None or cfg.hours.includes(self.store.clock()))
        if eligible and state.get("waiting_for_input"):
            return
        last = task["last_control"]
        reason = last.get("reason")
        # Faults are program acknowledgements, not a new owner instruction.
        # Only the current enabled default may recover indefinitely. A finite
        # task still retains its deadline and explicit pauses/stops stay closed.
        failed = last.get("action") == "fail"
        unbounded = task["config"].get("expires_at") is None
        resumable = explicit or reason in {
            "default_disabled",
            "paired_input_disconnected",
            "PairedInputDisconnected",
            "default_schedule_closed",
            "service_shutdown",
            "host restarted; resource recovery required before resume",
        } or (unbounded and failed)
        if eligible and (
            task["status"] == "accepted" or (task["status"] == "paused" and resumable)
        ):
            action = "start" if task["status"] == "accepted" else "resume"
            reason = "default_schedule_open"
        elif not eligible and task["status"] in {"running", "starting"}:
            action = "pause"
            reason = "default_schedule_closed" if cfg.enabled else "default_disabled"
        else:
            return
        installed = self.host.observations._installed()
        if action in {"start", "resume"}:
            restarted = task.get("reason") in {
                "service_shutdown", "host restarted; resource recovery required before resume"
            } and attempt.get("reason") != task.get("reason")
            if not explicit and not restarted and self.store.clock() < attempt["next_try"]:
                return
            # Persist before probing hardware, including failures before a new
            # generation can be allocated. There is no retry-count exhaustion.
            self.store.set_meta(retry_key, {
                "attempts": attempt["attempts"] + 1,
                "next_try": self.store.clock() + min(300, 30 * 2 ** min(attempt["attempts"], 4)),
                "reason": task.get("reason"),
            })
            gateway = installed.gateway
            if not gateway._released(task, capture_only=True):
                owner = gateway.owned.get(cfg.task_id)
                if owner:
                    await gateway._ensure_cleanup(owner, retry=True)
                else:
                    await gateway.recover(principal, cfg.task_id)
                if not gateway._released(task, capture_only=True):
                    raise Conflict("default capture resources are still releasing")
        if action in {"start", "resume"} and installed.validate_start:
            result = installed.validate_start(ObservationConfig.model_validate(task["config"]))
            if inspect.isawaitable(result):
                await result
        command = ObservationCommand(
            principal=principal,
            task_id=cfg.task_id,
            version=task["version"],
            generation=task["generation"],
            action=action,
            reason=reason,
            request_id="default-"
            + digest(
                [
                    state["revision"],
                    task["task_id"],
                    task["version"],
                    task["generation"],
                    action,
                    reason,
                ]
            )[:40],
        )
        await installed.gateway.control(command)
