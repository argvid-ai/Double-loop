"""Local simulated device. All effects are guarded by current persisted facts."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import suppress
from pathlib import Path

from .capture_sources import CaptureSource, PreparedClipSource
from .contracts import CapturePackage, ExecutionReceipt, Scene, canonical, utcnow
from .media import _publish_materialized, check_clip, file_hash, media_facts, processor_versions
from .store import Conflict


class ReplayDevice:
    def __init__(self, store, source: CaptureSource | None = None):
        self.store = store
        self.source = source or PreparedClipSource(store.root)
        self.lock = asyncio.Lock()
        self.before_commit = None

    async def checked_io(self, task_id, revision, function, *args):
        stopping = threading.Event()
        last_guard = -float("inf")

        def check_active():
            nonlocal last_guard
            if stopping.is_set():
                raise Conflict("capture processing interrupted")
            # Decoders/hashers may check every frame/chunk. Bound database
            # polling while retaining the unconditional transaction guard at commit.
            now = time.monotonic()
            if now - last_guard >= 0.05:
                self.guard(task_id, revision)
                last_guard = now

        worker = asyncio.create_task(asyncio.to_thread(function, *args, check_active=check_active))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Cancelling an asyncio waiter alone does not terminate its thread.
            # Keep the worker reachable and let its provider stop owned I/O.
            stopping.set()
            with suppress(Exception):
                await asyncio.shield(worker)
            raise

    def guard(self, task_id, revision):
        task = self.store.assert_active(task_id)
        with self.store.connect() as db:
            self.guard_parent(db, task["context"])
        scene = self.store.scene()
        if (
            task["context"]["invocation"]["arguments"]["constraints"]["subject_id"]
            != scene.subject_id
        ):
            raise Conflict("requested subject is not present in the replay scene")
        if scene.revision != revision:
            raise Conflict("SCENE_CHANGED: read the current facts before trying again")
        if not scene.allowed:
            raise Conflict(
                "ADMISSION_DENIED: user presence, visibility and device permission are required"
            )
        return task, scene

    def move(self, task_id, revision):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            scene = self.guard_transaction(db, task_id, revision)
            task_context = json.loads(
                db.execute("SELECT context FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
            )
            if task_context["invocation"]["arguments"]["constraints"].get("no_motion"):
                raise Conflict("user forbids motion")
            old = db.execute(
                "SELECT result FROM actions WHERE id=?", (f"move:{task_id}",)
            ).fetchone()
            if old:
                if scene.viewpoint != "front-mid":
                    raise Conflict(
                        "previous motion receipt no longer matches the current viewpoint"
                    )
                return json.loads(old[0])
            failure = db.execute("SELECT value FROM metadata WHERE key='motion_outcome'").fetchone()
            if failure and json.loads(failure[0]) != "succeeded":
                raise Conflict("SIMULATED_MOTION_" + json.loads(failure[0]).upper())
            started = utcnow()
            scene.viewpoint = "front-mid"
            scene.revision += 1
            scene.observed_at = utcnow()
            db.execute("UPDATE metadata SET value=? WHERE key='scene'", (canonical(scene),))
            receipt = ExecutionReceipt(
                action_id=f"move:{task_id}",
                status="SUCCEEDED",
                started_at=started,
                completed_at=utcnow(),
                final_viewpoint_id="front-mid",
                scene_revision=scene.revision,
            )
            result = receipt.model_dump(mode="json")
            db.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?)",
                (receipt.action_id, task_id, "move", canonical(result), time.time()),
            )
            self.store._audit(db, None, "scene.changed", scene.model_dump(mode="json"))
            self.store._audit(db, task_id, "tool.move", result)
        return result

    @staticmethod
    def guard_parent(db, context):
        if context.get("owner") == "harness":
            row = db.execute(
                "SELECT version,status FROM business_tasks WHERE id=?",
                (context["business_task_id"],),
            ).fetchone()
            if (
                row is None
                or row["version"] != context["business_version"]
                or row["status"] in {"paused", "cancelled", "completed"}
            ):
                raise Conflict("parent business task no longer permits execution")
            return
        if context.get("owner") != "conversation":
            return
        row = db.execute(
            "SELECT intent FROM conversations WHERE id=?", (context["conversation_id"],)
        ).fetchone()
        intent = json.loads(row[0]) if row else {}
        if (
            intent.get("version") != context["intent_version"]
            or intent.get("paused")
            or intent.get("pending_question")
            or intent.get("status") in {"completed", "cancelled", "unsupported"}
        ):
            raise Conflict("parent conversation no longer permits execution")

    @staticmethod
    def guard_transaction(db, task_id, revision):
        task = db.execute("SELECT status,context FROM tasks WHERE id=?", (task_id,)).fetchone()
        if (
            not task
            or task["status"] in {"completed", "unsupported", "cancelled", "expired"}
            or json.loads(task["context"]).get("paused")
            or json.loads(task["context"])["expires"] <= time.time()
        ):
            raise Conflict("task cancelled, finished or expired")
        ReplayDevice.guard_parent(db, json.loads(task["context"]))
        scene = Scene.model_validate_json(
            db.execute("SELECT value FROM metadata WHERE key='scene'").fetchone()[0]
        )
        if (
            json.loads(task["context"])["invocation"]["arguments"]["constraints"]["subject_id"]
            != scene.subject_id
        ):
            raise Conflict("requested subject is not present in the replay scene")
        if scene.revision != revision or not scene.allowed:
            raise Conflict("scene changed or execution no longer allowed")
        return scene

    async def capture(self, task_id, revision):
        task, scene = self.guard(task_id, revision)
        previous = self.store.action(f"capture:{task_id}")
        if previous:
            asset = self.store.asset(previous["asset"]["asset_id"])
            if (
                await self.checked_io(task_id, revision, file_hash, Path(asset["path"]))
                != previous["asset"]["sha256"]
            ):
                raise Conflict("previous capture file was changed")
            await self.checked_io(task_id, revision, check_clip, Path(asset["path"]), 6)
            return previous
        if scene.viewpoint != "front-mid":
            raise Conflict("move to registered front-mid viewpoint first")
        directory = self.store.root / "assets"
        directory.mkdir(parents=True, exist_ok=True)
        asset_id = f"capture-{task_id}"
        path = directory / f"{asset_id}.mp4"
        info = await self.checked_io(task_id, revision, self.source.recover, path, scene.subject_id)
        if info is None:
            # A source can recover a verified idempotent operation. Otherwise
            # rebuild an uncommitted local file; never infer success from existence.
            temporary = path.with_suffix(".partial.mp4")
            if temporary.exists():
                temporary.unlink()
            info = await self.checked_io(
                task_id, revision, self.source.materialize, temporary, scene.subject_id
            )
            await self.checked_io(task_id, revision, check_clip, temporary, 6)
            self.guard(task_id, revision)
            await self.checked_io(task_id, revision, _publish_materialized, temporary, path)
        facts = await self.checked_io(task_id, revision, media_facts, path, asset_id, "edge")
        self.guard(task_id, revision)
        receipt = self.store.action(f"move:{task_id}") or self.store.action(f"position:{task_id}")
        if not receipt:
            raise Conflict("verified position or motion receipt missing")
        package = CapturePackage(
            package_id=f"pkg-{task_id}",
            task_id=task_id,
            subject_id=task["context"]["invocation"]["arguments"]["constraints"]["subject_id"],
            captured_at=utcnow(),
            asset=facts,
            execution_receipt=receipt,
            qc={
                "passed": True,
                "technical": "full_decode_dimensions_duration_frames_passed",
                "semantic_source": "manual_annotated_replay",
                "focus_score": None,
                "simulated": True,
            },
            provenance={
                **info["provenance"],
                "selection": info["selection"],
                "capture_mode": "replay",
                "capture_operation": info["operation"],
                "processors": processor_versions(),
            },
        )
        body = package.model_dump(mode="json")
        if self.before_commit is not None:
            await self.before_commit(task_id)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.guard_transaction(db, task_id, revision)
            db.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?)",
                (asset_id, task_id, str(path.resolve()), canonical(body), "closeup"),
            )
            db.execute(
                "INSERT INTO actions VALUES (?,?,?,?,?)",
                (f"capture:{task_id}", task_id, "capture", canonical(body), time.time()),
            )
            self.store._audit(db, task_id, "tool.capture", body)
        (directory / f"{asset_id}.json").write_text(json.dumps(body, ensure_ascii=False, indent=2))
        return body

    async def execute(self, task_id, name, args):
        async with self.lock:
            if name == "move_to_viewpoint":
                return self.move(task_id, args["expected_revision"])
            if name == "capture_workflow":
                task = self.store.task(task_id)
                if task["context"]["invocation"]["arguments"]["constraints"].get("no_motion"):
                    with self.store.connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        scene = self.guard_transaction(db, task_id, args["expected_revision"])
                        if scene.viewpoint != "front-mid":
                            raise Conflict(
                                "stationary capture unavailable at the current viewpoint; motion forbidden"
                            )
                        receipt = ExecutionReceipt(
                            action_id=f"position:{task_id}",
                            status="SUCCEEDED",
                            started_at=utcnow(),
                            completed_at=utcnow(),
                            final_viewpoint_id="front-mid",
                            scene_revision=scene.revision,
                        ).model_dump(mode="json")
                        db.execute(
                            "INSERT OR IGNORE INTO actions VALUES (?,?,?,?,?)",
                            (
                                receipt["action_id"],
                                task_id,
                                "stationary_position",
                                canonical(receipt),
                                time.time(),
                            ),
                        )
                        self.store._audit(
                            db,
                            task_id,
                            "capture.stationary",
                            {"motion_executed": False, "simulated": True},
                        )
                    return await self.capture(task_id, args["expected_revision"])
                receipt = self.store.action("move:" + task_id)
                if (
                    receipt
                    and self.store.task(task_id)["context"].get("owner") == "conversation"
                    and self.store.scene().viewpoint == receipt["final_viewpoint_id"]
                ):
                    # Resume the remaining step of this same durable operation;
                    # current guards still run before/after capture.
                    self.store.audit(
                        task_id,
                        "capture.resume.after_move",
                        {
                            "old_revision": args["expected_revision"],
                            "current_revision": self.store.scene().revision,
                        },
                    )
                else:
                    self.move(task_id, args["expected_revision"])
                return await self.capture(task_id, self.store.scene().revision)
            if name == "camera_capture":
                return await self.capture(task_id, args["expected_revision"])
            raise ValueError("unknown replay action")
