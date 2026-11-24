"""Resume opted-in bounded captures through the existing node clock and gateway.

Old tasks remain opt-out. Manual pauses, expired deadlines and uncertain resource
release never become permission to open a camera.
"""

import asyncio
import inspect

from .contracts import digest
from .observation_contracts import ObservationCommand, ObservationConfig


class CaptureRecovery:
    def __init__(self, host):
        self.host, self.store = host, host.store
        self.lock = asyncio.Lock()

    async def tick(self):
        installed = self.host.observations.installation
        if self.host.side != "edge" or installed is None:
            return
        async with self.lock, self.host.observations.command_lock:
            rows = await asyncio.to_thread(self._probe)
            for row in rows:
                await self._resume(installed, row)

    def _probe(self):
        """Current recovery eligibility only; command history is not a queue."""
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT * FROM observation_tasks WHERE status='paused'
                AND (json_extract(last_control,'$.reason') IN ('service_shutdown',
                    'host restarted; resource recovery required before resume')
                    OR json_extract(last_control,'$.action')='fail')
                AND json_extract(config,'$.resume_after_restart')=1
                AND json_extract(config,'$.capture_until')>?""",
                (self.store.clock(),),
            ).fetchall()
        return [dict(row) for row in rows]

    async def _resume(self, installed, row):
        cfg = ObservationConfig.model_validate_json(row["config"])
        key = "capture_recovery:" + digest([cfg.task_id, cfg.version])[:32]
        attempt = self.store.meta(key, {"attempts": 0, "next_try": 0})
        if self.store.clock() < attempt["next_try"]:
            return
        gateway = installed.gateway
        current = gateway.status(cfg.principal, cfg.task_id)
        if current["recovery_pending"]:
            return
        if not gateway._released(current["task"], capture_only=True):
            owner = gateway.owned.get(cfg.task_id)
            if owner:
                await gateway._ensure_cleanup(owner, retry=True)
            else:
                await gateway.recover(cfg.principal, cfg.task_id)
            current = gateway.status(cfg.principal, cfg.task_id)
            if not gateway._released(current["task"], capture_only=True):
                return
        task = current["task"]
        if (
            task["status"] != "paused" or task["version"] != cfg.version
            or self.store.clock() >= cfg.capture_until
        ):
            return
        attempt = {
            "attempts": attempt["attempts"] + 1,
            "next_try": self.store.clock() + min(300, 30 * 2 ** min(attempt["attempts"], 4)),
        }
        self.store.set_meta(key, attempt)
        try:
            if installed.validate_start:
                result = installed.validate_start(cfg)
                if inspect.isawaitable(result):
                    await result
            result = await gateway.control(ObservationCommand(
                principal=cfg.principal, task_id=cfg.task_id, version=cfg.version,
                generation=task["generation"], action="resume",
                reason="bounded_capture_restart",
                request_id="capture-recovery-" + digest(
                    [cfg.task_id, cfg.version, task["generation"]]
                )[:32],
            ))
            self.store.audit(cfg.task_id, "observation.capture.resume_requested", {
                "generation": result["task"]["generation"],
                "capture_until": cfg.capture_until, "old_deadline_preserved": True,
                "recording_confirmed": False,
            })
            self.store.set_meta(key, {"attempts": 0, "next_try": 0})
        except Exception as exc:
            self.host.health.record(
                cfg.principal, "capture_recovery", "resume_failed",
                task_id=cfg.task_id, version=cfg.version, generation=task["generation"],
                details={"error_type": type(exc).__name__, **attempt},
            )
