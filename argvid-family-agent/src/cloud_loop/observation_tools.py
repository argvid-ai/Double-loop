"""Five observation capabilities. Device simulation is explicit and persistent.

No model loop lives here. NanoBot supplies invocation context and executes these
handlers; Gateway run/attempt ownership and input timestamps constrain effects.
"""

from __future__ import annotations

import json
import math
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract, canonical
from .observation_contracts import Counter, ObservationRun, Reference
from .observation_decisions import DecisionContext, MediaDecision
from .store import Conflict


class MoveCamera(Contract):
    pan: float = Field(strict=True, allow_inf_nan=False)
    tilt: float = Field(strict=True, allow_inf_nan=False)
    expected_pose_revision: Counter


class ManualCameraControl(Contract):
    request_id: Reference
    device_ref: Reference
    action: Literal["takeover", "move", "stop", "release"]
    expected_pose_revision: Counter
    pan: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    tilt: float | None = Field(default=None, strict=True, allow_inf_nan=False)

    @model_validator(mode="after")
    def angles(self):
        if (
            self.action == "move"
            and (self.pan is None or self.tilt is None)
            or self.action != "move"
            and (self.pan is not None or self.tilt is not None)
        ):
            raise ValueError("angles are required only for a manual move")
        return self


class NoArguments(Contract):
    pass


class MarkMedia(Contract):
    decision: Literal["keep", "discard", "uncertain"]
    reason: str = Field(min_length=1, max_length=1000)
    start_ms: Counter | None = None
    end_ms: Counter | None = None
    expected_revision: Counter = 0
    resolve_id: Reference | None = None


class ReadMedia(Contract):
    batch_id: Reference


OBSERVATION_TOOLS = {
    "move_camera": (MoveCamera, "根据当前观测与姿态版本调整机位；Mock不改变回放画面。"),
    "stop_motion": (NoArguments, "停止本任务控制的云台，返回实际驱动或明确的Mock回执。"),
    "mark_media": (MarkMedia, "判断已观察的影音时间段：保留、不保留或不确定；缺省范围为本次输入。"),
    "read_media": (ReadMedia, "回看本任务已采集的批次；历史内容不提供新的机位控制权限。"),
    "get_edge_state": (NoArguments, "按需读取观察状态、未处理范围和模拟云台状态。"),
}


class MockObservationGimbal:
    simulated = True
    affects_video = False

    def __init__(self, ledger):
        self.ledger, self.store = ledger, ledger.store
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS observation_gimbals (
                    id TEXT PRIMARY KEY,principal TEXT NOT NULL,spec TEXT NOT NULL,pan REAL NOT NULL,tilt REAL NOT NULL,
                    revision INTEGER NOT NULL,manual INTEGER NOT NULL,lease TEXT
                );
                CREATE TABLE IF NOT EXISTS observation_pose_history (
                    device TEXT NOT NULL,revision INTEGER NOT NULL,at REAL NOT NULL,state TEXT NOT NULL,
                    PRIMARY KEY(device,revision)
                );
                CREATE TABLE IF NOT EXISTS observation_motion_receipts (
                    principal TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL,receipt TEXT NOT NULL,
                    PRIMARY KEY(principal,id)
                );
            """)

    def register(
        self, principal, device, *, pan_limit=90, tilt_limit=45, max_step=15, max_age_seconds=5
    ):
        from .observation_archive import ObservationArchive

        ObservationArchive._id(principal)
        ObservationArchive._id(device)
        if any(
            type(x) not in {int, float} or not math.isfinite(x) or x <= 0
            for x in (pan_limit, tilt_limit, max_step, max_age_seconds)
        ):
            raise ValueError("invalid gimbal limits")
        spec = canonical(
            dict(
                pan_limit=pan_limit,
                tilt_limit=tilt_limit,
                max_step=max_step,
                max_age_seconds=max_age_seconds,
            )
        )
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM observation_gimbals WHERE id=?", (device,)).fetchone()
            if old and (old["principal"] != principal or old["spec"] != spec):
                raise Conflict("gimbal binding changed")
            db.execute(
                "INSERT OR IGNORE INTO observation_gimbals VALUES (?,?,?,0,0,0,0,NULL)",
                (device, principal, spec),
            )
            self._remember(db, device)

    def _state(self, db, principal, device):
        row = db.execute(
            "SELECT * FROM observation_gimbals WHERE id=? AND principal=?", (device, principal)
        ).fetchone()
        if row is None:
            raise KeyError(device)
        return {
            **dict(row),
            "spec": json.loads(row["spec"]),
            "lease": json.loads(row["lease"]) if row["lease"] else None,
            "simulated": True,
            "affects_video": False,
            "physical_hardware_connected": False,
        }

    def state(self, principal, device):
        with self.store.connect() as db:
            return self._state(db, principal, device)

    def _remember(self, db, device):
        row = db.execute(
            "SELECT principal FROM observation_gimbals WHERE id=?", (device,)
        ).fetchone()
        state = self._state(db, row["principal"], device)
        db.execute(
            "INSERT OR IGNORE INTO observation_pose_history VALUES (?,?,?,?)",
            (device, state["revision"], self.store.clock(), canonical(state)),
        )

    def at(self, principal, device, when):
        with self.store.connect() as db:
            self._state(db, principal, device)
            row = db.execute(
                "SELECT state FROM observation_pose_history WHERE device=? AND at<=? ORDER BY revision DESC LIMIT 1",
                (device, when),
            ).fetchone()
            return json.loads(row["state"]) if row else None

    def claim(self, run, *, allow_manual=False):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.ledger._task(db, run.principal, run.task_id)
            self.ledger._guard(task, run)
            if task["status"] not in {"starting", "running"}:
                raise Conflict("task cannot acquire gimbal")
            device = json.loads(task["config"])["ptz_scope_ref"]
            if not device:
                raise Conflict("task has no gimbal binding")
            state = self._state(db, run.principal, device)
            lease = {key: getattr(run, key) for key in ("task_id", "version", "generation")}
            if state["lease"] == lease:
                return state
            if state["lease"]:
                old = state["lease"]
                receipt = db.execute(
                    """SELECT payload FROM audit WHERE task_id=? AND kind='observation.resources.closed'
                    AND json_extract(payload,'$.generation')=? ORDER BY seq DESC LIMIT 1""",
                    (old["task_id"], old["generation"]),
                ).fetchone()
                if not receipt or json.loads(receipt["payload"]).get("confirmed") is not True:
                    raise Conflict("previous gimbal lease has not been released")
            if state["manual"]:
                if allow_manual:
                    return state
                raise Conflict("gimbal is under manual control")
            db.execute(
                "UPDATE observation_gimbals SET lease=?,revision=revision+1 WHERE id=?",
                (canonical(lease), device),
            )
            self._remember(db, device)
            return self._state(db, run.principal, device)

    def execute(self, context, operation, args, request_id, *, is_current):
        if operation not in {"move_camera", "stop_motion"}:
            raise ValueError("unsupported gimbal operation")
        wire = {
            "context": context.model_dump(mode="json"),
            "operation": operation,
            "arguments": args,
        }
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM observation_motion_receipts WHERE principal=? AND id=?",
                (context.principal, request_id),
            ).fetchone()
            if old:
                if old["body"] != canonical(wire):
                    raise Conflict("motion request identity collision")
                return {**json.loads(old["receipt"]), "duplicate": True}
            task = self.ledger._task(db, context.principal, context.task_id)
            self.ledger._guard(task, context, running=True)
            if context.mode != "live" or not is_current():
                raise Conflict("motion requires a current live observation")
            cfg = json.loads(task["config"])
            if context.session_ref != cfg["session_ref"]:
                raise Conflict("motion session binding mismatch")
            device = cfg["ptz_scope_ref"]
            if not device:
                raise Conflict("task has no gimbal binding")
            state = self._state(db, context.principal, device)
            lease = {key: getattr(context, key) for key in ("task_id", "version", "generation")}
            if state["lease"] != lease or state["manual"]:
                raise Conflict("gimbal lease or manual-control state changed")
            if operation == "move_camera":
                if set(args) != {"pan", "tilt", "expected_pose_revision"}:
                    raise ValueError("invalid move arguments")
                if type(args["expected_pose_revision"]) is not int:
                    raise ValueError("pose revision must be an integer")
                if any(
                    type(args[k]) not in {int, float} or not math.isfinite(args[k])
                    for k in ("pan", "tilt")
                ):
                    raise ValueError("angles must be finite numbers")
                batch = self.ledger._batch(db, context.task_id, context.batch_id)
                body = json.loads(batch["body"])
                age = self.store.clock() - body["captured_at"]
                if (
                    body["version"] != context.version
                    or body["generation"] != context.generation
                    or batch["gap_reason"] is not None
                    or "image" in body["missing_modalities"]
                    or not 0 <= age <= state["spec"]["max_age_seconds"]
                ):
                    raise Conflict("observation is not fresh motion evidence")
                if (
                    body.get("camera_pose_revision") != state["revision"]
                    or args["expected_pose_revision"] != state["revision"]
                    or body.get("camera_state_simulated") is not True
                ):
                    raise Conflict("observed camera pose is no longer current")
                if (
                    abs(args["pan"]) > state["spec"]["pan_limit"]
                    or abs(args["tilt"]) > state["spec"]["tilt_limit"]
                    or abs(args["pan"] - state["pan"]) > state["spec"]["max_step"]
                    or abs(args["tilt"] - state["tilt"]) > state["spec"]["max_step"]
                ):
                    raise Conflict("gimbal travel or step limit exceeded")
                db.execute(
                    "UPDATE observation_gimbals SET pan=?,tilt=?,revision=revision+1 WHERE id=?",
                    (args["pan"], args["tilt"], device),
                )
            else:
                if args:
                    raise ValueError("stop takes no model arguments")
                db.execute(
                    "UPDATE observation_gimbals SET revision=revision+1 WHERE id=?", (device,)
                )
            self._remember(db, device)
            receipt = {
                "operation": operation,
                "confirmed": True,
                "simulated": True,
                "affects_video": False,
                "state": self._state(db, context.principal, device),
                "duplicate": False,
            }
            db.execute(
                "INSERT INTO observation_motion_receipts VALUES (?,?,?,?)",
                (context.principal, request_id, canonical(wire), canonical(receipt)),
            )
            self.store._audit(
                db,
                context.task_id,
                "observation.motion",
                {"request_id": request_id, **wire, "receipt": receipt},
            )
            return receipt

    def stop_run(self, config, run):
        if not config.ptz_scope_ref:
            return {"confirmed": True, "simulated": True, "required": False}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db, run.principal, config.ptz_scope_ref)
            lease = {key: getattr(run, key) for key in ("task_id", "version", "generation")}
            if state["lease"] not in (None, lease):
                return {"confirmed": False, "simulated": True, "reason": "lease_changed"}
            if state["lease"] is None:
                return {
                    "confirmed": True,
                    "simulated": True,
                    "already_released": True,
                    "affects_video": False,
                }
            db.execute(
                "UPDATE observation_gimbals SET lease=NULL,revision=revision+1 WHERE id=?",
                (config.ptz_scope_ref,),
            )
            self._remember(db, config.ptz_scope_ref)
            return {"confirmed": True, "simulated": True, "affects_video": False}

    def manual_control(self, principal, device, enabled):
        """Local operator control revokes model ownership; not a model tool."""
        if type(enabled) is not bool:
            raise ValueError("manual control requires a boolean")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._state(db, principal, device)
            db.execute(
                "UPDATE observation_gimbals SET manual=?,lease=NULL,revision=revision+1 WHERE id=?",
                (int(enabled), device),
            )
            self._remember(db, device)
            return self._state(db, principal, device)

    def manual_command(self, principal, command):
        """An authenticated local operator, never a fabricated model invocation."""
        command = ManualCameraControl.model_validate(command.model_dump())
        wire = {
            "source": "local_operator",
            "device_ref": command.device_ref,
            **command.model_dump(exclude={"request_id"}),
        }
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = self._state(db, principal, command.device_ref)
            old = db.execute(
                "SELECT * FROM observation_motion_receipts WHERE principal=? AND id=?",
                (principal, command.request_id),
            ).fetchone()
            if old:
                if old["body"] != canonical(wire):
                    raise Conflict("manual control identity collision")
                return {**json.loads(old["receipt"]), "duplicate": True, "current_state": state}
            if command.expected_pose_revision != state["revision"]:
                raise Conflict("camera state changed; refresh before manual control")
            previous_lease = state["lease"]
            lease = None
            if command.action == "move":
                if not state["manual"]:
                    raise Conflict("manual takeover is required before moving")
                if (
                    abs(command.pan) > state["spec"]["pan_limit"]
                    or abs(command.tilt) > state["spec"]["tilt_limit"]
                    or abs(command.pan - state["pan"]) > state["spec"]["max_step"]
                    or abs(command.tilt - state["tilt"]) > state["spec"]["max_step"]
                ):
                    raise Conflict("manual movement exceeds gimbal limits")
                db.execute(
                    "UPDATE observation_gimbals SET pan=?,tilt=? WHERE id=?",
                    (command.pan, command.tilt, command.device_ref),
                )
            if command.action == "release":
                if not state["manual"]:
                    raise Conflict("camera is not under manual control")
                tasks = list(
                    db.execute(
                        """SELECT task_id,version,generation FROM observation_tasks WHERE principal=?
                    AND status IN ('starting','running') AND json_extract(config,'$.ptz_scope_ref')=?
                    AND (json_extract(config,'$.expires_at') IS NULL OR json_extract(config,'$.expires_at')>?)""",
                        (principal, command.device_ref, self.store.clock()),
                    )
                )
                if len(tasks) > 1:
                    raise Conflict("multiple tasks claim this manual camera")
                if tasks:
                    lease = dict(tasks[0])
            db.execute(
                "UPDATE observation_gimbals SET manual=?,lease=?,revision=revision+1 WHERE id=?",
                (
                    int(command.action != "release"),
                    canonical(lease) if lease else None,
                    command.device_ref,
                ),
            )
            self._remember(db, command.device_ref)
            state = self._state(db, principal, command.device_ref)
            receipt = {
                "operation": command.action,
                "confirmed": True,
                "simulated": True,
                "affects_video": False,
                "duplicate": False,
                "state": state,
                "current_state": state,
            }
            db.execute(
                "INSERT INTO observation_motion_receipts VALUES (?,?,?,?)",
                (principal, command.request_id, canonical(wire), canonical(receipt)),
            )
            self.store._audit(
                db,
                (lease or previous_lease or {}).get("task_id"),
                "observation.manual_motion",
                {
                    "request_id": command.request_id,
                    **wire,
                    "previous_lease": previous_lease,
                    "receipt": receipt,
                },
            )
            return receipt

    def manual_receipt(self, principal, device, request_id):
        with self.store.connect() as db:
            state = self._state(db, principal, device)
            row = db.execute(
                "SELECT body,receipt FROM observation_motion_receipts WHERE principal=? AND id=?",
                (principal, request_id),
            ).fetchone()
            if row is None:
                raise KeyError(request_id)
            body = json.loads(row["body"])
            if body.get("source") != "local_operator" or body.get("device_ref") != device:
                raise KeyError(request_id)
            return {**json.loads(row["receipt"]), "current_state": state}


class ObservationTools:
    names = ("move_camera", "stop_motion", "mark_media", "read_media", "get_edge_state")

    def __init__(self, ledger, decisions, gimbal, media_reader):
        self.ledger, self.decisions, self.gimbal, self.media_reader = (
            ledger,
            decisions,
            gimbal,
            media_reader,
        )

    def bind(self, run):
        self.decisions.freeze_policy(run)
        cfg = self.ledger.get(run.principal, run.task_id)["config"]
        # Manual motion ownership must not prevent recording/classification or
        # reopening the live model after a historical backfill.
        return self.gimbal.claim(run, allow_manual=True) if cfg["ptz_scope_ref"] else None

    @staticmethod
    def contracts(mode="live"):
        return {
            name: contract
            for name, contract in OBSERVATION_TOOLS.items()
            if mode != "backfill" or name not in {"move_camera", "stop_motion"}
        }

    async def execute(self, context, name, args, call_id, *, is_current):
        context = DecisionContext.model_validate(context.model_dump())
        if name not in self.names:
            raise Conflict("observation tool is not registered")
        args = OBSERVATION_TOOLS[name][0].model_validate(args).model_dump(exclude_none=True)
        run = ObservationRun(
            **{
                key: getattr(context, key)
                for key in ("principal", "task_id", "version", "generation")
            }
        )
        if not self.ledger.run_is_current(run) or not is_current():
            raise Conflict("observation invocation ended")
        if context.mode == "backfill" and name in {"move_camera", "stop_motion"}:
            raise Conflict("historical context has no motion tools")
        if name in {"move_camera", "stop_motion"}:
            return self.gimbal.execute(context, name, args, call_id, is_current=is_current)
        if name == "mark_media":
            if set(args) - {
                "decision",
                "reason",
                "start_ms",
                "end_ms",
                "expected_revision",
                "resolve_id",
            }:
                raise ValueError("invalid media mark arguments")
            with self.ledger.store.connect() as db:
                batch = self.ledger._batch(db, context.task_id, context.batch_id)
            decision = MediaDecision(
                request_id=call_id,
                start_ms=args.get("start_ms", batch["start_ms"]),
                end_ms=args.get("end_ms", batch["end_ms"]),
                decision=args["decision"],
                reason=args["reason"],
                expected_revision=args.get("expected_revision", 0),
                resolve_id=args.get("resolve_id"),
            )
            return self.decisions.record(context, decision, is_current=is_current)
        if name == "read_media":
            if set(args) != {"batch_id"}:
                raise ValueError("read_media requires a captured batch reference")
            with self.ledger.store.connect() as db:
                batch = self.ledger._batch(db, context.task_id, args["batch_id"])
            if self.media_reader is None:
                raise Conflict("media reader is unavailable")
            return await self.media_reader(json.loads(batch["body"]), context)
        if args:
            raise ValueError("get_edge_state takes no model arguments")
        state = self.ledger.get(context.principal, context.task_id)
        cfg = state["config"]
        with self.ledger.store.connect() as db:
            decisions = [
                dict(row)
                for row in db.execute(
                    """SELECT id,batch_id,start_ms,end_ms,decision,revision,event_id,version
                FROM observation_decisions WHERE task_id=? ORDER BY updated DESC,rowid DESC LIMIT 50""",
                    (context.task_id,),
                )
            ]
        return {
            "task": state,
            "gimbal": self.gimbal.state(context.principal, cfg["ptz_scope_ref"])
            if cfg["ptz_scope_ref"]
            else None,
            "decisions": decisions,
            "recent_media": self.ledger.entries(
                context.principal,
                context.task_id,
                after_sequence=max(
                    -1, (state["ledger"]["latest_ingested"] or {"sequence": 0})["sequence"] - 10
                ),
            ),
            "physical_camera_connected": False,
        }
