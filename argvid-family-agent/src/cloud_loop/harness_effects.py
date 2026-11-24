"""Asynchronous media tools using the existing guarded replay and file processors."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from contextlib import ExitStack, suppress
from pathlib import Path

from .capture_sources import VideoFileSource
from .contracts import Constraints, DeliverableManifest, canonical
from .media import (
    SOURCE_BOUND_RENDER,
    check_clip,
    file_hash,
    media_facts,
    processor_versions,
    render,
)
from .replay import ReplayDevice
from .store import Conflict


class CaptureConditionsUnavailable(Conflict):
    """Expected loss of capture permission; retain the authorized plan for a later wake."""


class HarnessEffects:
    def __init__(self, host):
        self.host, self.store = host, host.store
        self.device = (
            ReplayDevice(self.store, VideoFileSource(self.store.root))
            if host.side == "edge"
            else None
        )
        if self.device:
            self.device.before_commit = self.before_commit

    async def before_commit(self, job_id):
        if not self.host.settings.test_controls:
            return
        if not self.store.meta("test_commit_hold", False):
            return
        self.store.set_meta(
            "test_barrier_reached", {"job_id": job_id, "phase": "before_asset_commit"}
        )
        while self.store.meta("test_commit_hold", False):
            with self.store.connect() as db:
                job = db.execute("SELECT * FROM harness_jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise Conflict("test barrier has no job")
            self.store.guard_job(dict(job))
            await asyncio.sleep(0.05)

    def prior_capture(self, business, exclude_job=None):
        """An already committed step survives permission-only pause/resume.

        Match the same business identity and unchanged plan. A new user task,
        changed goal or changed constraints cannot silently reuse this receipt.
        Bytes are rechecked before a result is published.
        """
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT a.result,t.prompt,t.context,j.id FROM actions a
                JOIN harness_jobs j ON j.id=a.task_id JOIN tasks t ON t.id=j.id
                WHERE j.task_id=? AND a.kind='capture' ORDER BY a.created DESC""",
                (business["id"],),
            ).fetchall()
        for row in rows:
            if row["id"] == exclude_job:
                continue
            context = json.loads(row["context"])
            constraints = context["invocation"]["arguments"]["constraints"]
            if (
                row["prompt"] == business["goal"]
                and constraints.get("no_motion", False) == business["constraints"]["no_motion"]
            ):
                return {"job_id": row["id"], "package": json.loads(row["result"])}
        return None

    def prepare_task(self, job):
        business = self.store.guard_job(job)
        context = {
            "owner": "harness",
            "business_task_id": business["id"],
            "business_version": business["version"],
            "expires": time.time() + self.host.profile.max_task_seconds,
            "profile_digest": self.host.profile.fingerprint,
            "mode": "deferred",
            "user_context": {"subject_id": "pet-cat-mimi"},
            "invocation": {
                "arguments": {
                    "constraints": Constraints(
                        no_motion=business["constraints"]["no_motion"]
                    ).model_dump()
                }
            },
            "simulated": True,
        }
        if business["constraints"].get("media_recipe"):
            context["user_context"] = {"media_recipe": business["constraints"]["media_recipe"]}
            context["invocation"] = {"arguments": {"constraints": business["constraints"]}}
        with self.store.connect() as db:
            plan = db.execute(
                "SELECT plan FROM harness_job_plans WHERE job_id=?", (job["id"],)
            ).fetchone()
            if plan:
                context["user_context"] = {"saved_goal_constraints": business["constraints"]}
                context["invocation"] = {
                    "arguments": {
                        "constraints": business["constraints"],
                        "job_plan": json.loads(plan["plan"]),
                    }
                }
            deadline = db.execute(
                "SELECT MIN(expires) FROM peer_task_bindings WHERE task_id=?", (business["id"],)
            ).fetchone()[0]
            if deadline is not None:
                context["expires"] = min(context["expires"], deadline)
            db.execute(
                "INSERT OR IGNORE INTO tasks VALUES (?,?,?,?,NULL,NULL,?)",
                (job["id"], business["goal"], canonical(context), "running", time.time()),
            )
        return business

    async def checked_io(self, job, function, *args):
        stopped = threading.Event()
        next_guard = 0.0

        def check():
            nonlocal next_guard
            if stopped.is_set():
                raise Conflict("processing interrupted")
            now = time.monotonic()
            if now >= next_guard:
                self.store.guard_job(job)
                next_guard = now + 0.5

        worker = asyncio.create_task(asyncio.to_thread(function, *args, check_active=check))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            stopped.set()
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.shield(worker)
            raise

    async def run(self, job):
        business = self.prepare_task(job)
        with self.store.connect() as db:
            has_plan = db.execute(
                "SELECT plan FROM harness_job_plans WHERE job_id=?", (job["id"],)
            ).fetchone()
        if has_plan and json.loads(has_plan["plan"]).get("kind") == "remotion_project":
            return await self.run_project(job, business, json.loads(has_plan["plan"]))
        if business["constraints"].get("media_recipe") or has_plan:
            return await self.run_recipe(job, business)
        if not self.host.settings.legacy_media_compatibility:
            raise Conflict("legacy media path is disabled; explicit job plan required")
        if job["kind"] == "capture":
            prior = self.prior_capture(business, job["id"])
            if prior:
                package = prior["package"]
                asset = self.store.asset(package["asset"]["asset_id"])
                checked = await self.checked_io(job, check_clip, Path(asset["path"]), 6)
                if checked["sha256"] != package["asset"]["sha256"]:
                    raise Conflict("committed reusable capture changed")
                self.store.audit(
                    job["id"],
                    "capture.reused",
                    {"source_job": prior["job_id"], "asset_id": asset["id"], "new_capture": False},
                )
                return {
                    "verified": True,
                    "asset_id": asset["id"],
                    "sha256": checked["sha256"],
                    "package": package,
                    "simulated": True,
                    "reused_from_job": prior["job_id"],
                }
            scene = self.store.scene()
            if not scene.allowed:
                raise Conflict("fresh scene permission is required")
            package = await self.device.execute(
                job["id"], "capture_workflow", {"expected_revision": scene.revision}
            )
            return {
                "verified": True,
                "asset_id": package["asset"]["asset_id"],
                "sha256": package["asset"]["sha256"],
                "package": package,
                "simulated": True,
            }
        if job["kind"] != "compose":
            raise Conflict("unsupported media operation")
        assets = self.assets(business["id"], business["principal"])
        wide = next((a for a in assets if a["kind"] == "wide"), None)
        close = next((a for a in assets if a["kind"] == "closeup"), None)
        if not wide or not close:
            raise Conflict("verified wide and close-up assets are required")
        selected = [self.store.asset(a["id"]) for a in (wide, close)]
        for asset in selected:
            if (
                await self.checked_io(job, file_hash, Path(asset["path"]))
                != asset["info"]["asset"]["sha256"]
            ):
                raise Conflict("source asset checksum mismatch")
        asset_id = f"story-{job['id']}"
        path = self.store.root / "assets" / f"{asset_id}.mp4"
        try:
            prior = self.store.asset(asset_id)
        except KeyError:
            prior = None
        if prior:
            if (
                await self.checked_io(job, file_hash, Path(prior["path"]))
                != prior["info"]["asset"]["sha256"]
            ):
                raise Conflict("registered deliverable changed")
            await self.checked_io(job, check_clip, Path(prior["path"]), 12)
            return {
                "verified": True,
                "asset_id": asset_id,
                "sha256": prior["info"]["asset"]["sha256"],
                "simulated": True,
            }
        await self.checked_io(
            job, render, Path(selected[0]["path"]), Path(selected[1]["path"]), path
        )
        facts = await self.checked_io(job, media_facts, path, asset_id, "cloud")
        manifest = DeliverableManifest(
            task_id=job["id"],
            asset=facts,
            timeline=[
                {
                    "asset_id": a["id"],
                    "sha256": a["info"]["asset"]["sha256"],
                    "start_ms": i * 6000,
                    "duration_ms": 6000,
                }
                for i, a in enumerate(selected)
            ],
            provenance={
                **selected[0]["info"]["provenance"],
                "processors": facts.get("processors")
                if self.host.settings.media_execution == "peer"
                else processor_versions(),
                "materialization": SOURCE_BOUND_RENDER,
            },
            qc={
                "passed": True,
                "technical": "decoded twelve-second portrait file",
                "simulated": True,
            },
        )
        body = manifest.model_dump(mode="json")
        await self.before_commit(job["id"])
        # Publication and business version check share one SQLite transaction.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT version,status FROM business_tasks WHERE id=?", (business["id"],)
            ).fetchone()
            if row["version"] != job["version"] or row["status"] in {
                "paused",
                "cancelled",
                "completed",
            }:
                raise Conflict("render became stale before publication")
            db.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?)",
                (asset_id, job["id"], str(path.resolve()), canonical(body), "deliverable"),
            )
        path.with_suffix(".json").write_text(json.dumps(body, ensure_ascii=False, indent=2))
        return {"verified": True, "asset_id": asset_id, "sha256": facts.sha256, "simulated": True}

    async def run_recipe(self, job, business):
        from .edge_render import ensure_local_asset
        from .harness_media_contracts import MediaRecipe

        with self.store.connect() as db:
            row = db.execute(
                "SELECT plan FROM harness_job_plans WHERE job_id=?", (job["id"],)
            ).fetchone()
        plan = json.loads(row["plan"]) if row else None
        recipe = MediaRecipe.model_validate(
            plan["recipe"] if plan else business["constraints"]["media_recipe"]
        )
        with ExitStack() as leases:
            leases.enter_context(
                self.host.media_storage.reserve(
                    "assets", self.host.settings.observation_input_file_bytes
                )
            )
            available = {
                a["id"] for a in self.composition_assets(business["id"], business["principal"])
            }
            for asset_id in dict.fromkeys(s.asset_id for s in recipe.segments if s.asset_id):
                if asset_id not in available:
                    raise Conflict("recipe asset not available to current matter")
                path = await ensure_local_asset(self.host, self.store.asset(asset_id))
                leases.enter_context(self.host.media_storage.pin(path))
            result = await self._run_recipe(job, business, plan, recipe)
            path = self.store.asset(result["asset_id"])["path"]
            self.host.media_storage.register(path, "assets", owner=business["id"])
            return result

    async def _run_recipe(self, job, business, plan, recipe):
        from .harness_media_render import check_recipe_output, render_recipe

        inputs = []
        accessible = {
            a["id"] for a in self.composition_assets(business["id"], business["principal"])
        }
        for segment in recipe.segments:
            if segment.source_id:
                source = self.host.sources.get(
                    business["principal"], segment.source_id, include_path=True
                )
                inputs.append(
                    {
                        "path": source["path"],
                        "facts": source["facts"],
                        "simulated": source.get("simulated", True),
                        "source": {
                            k: v for k, v in source.items() if k not in {"path", "playback"}
                        },
                    }
                )
            else:
                if segment.asset_id not in accessible:
                    raise Conflict("recipe asset not available to current matter")
                asset = self.store.asset(segment.asset_id)
                input_simulated = asset["info"].get(
                    "simulated", asset["info"]["asset"].get("simulated", True)
                )
                inputs.append(
                    {
                        "path": asset["path"],
                        "facts": asset["info"]["asset"],
                        "simulated": input_simulated,
                        "source": {
                            "asset_id": asset["id"],
                            "provenance": asset["info"].get("provenance"),
                            "simulated": input_simulated,
                        },
                    }
                )

        def guard():
            self.store.guard_job(job)
            if job["kind"] != "capture":
                return
            scene = self.store.scene()
            if not scene.allowed or (
                business["constraints"]["no_motion"] and scene.viewpoint != "front-mid"
            ):
                raise CaptureConditionsUnavailable(
                    "fresh scene permission and permitted viewpoint required during capture"
                )

        self.store.guard_job(job)
        asset_id = "media-" + job["id"]
        destination = self.store.root / "assets" / (asset_id + ".mp4")
        try:
            existing = self.store.asset(asset_id)
        except KeyError:
            existing = None
        if existing:
            if existing["info"].get("recipe") != recipe.model_dump(mode="json"):
                raise Conflict("committed recipe changed")
            checked = await self.checked_io(
                job, check_recipe_output, Path(existing["path"]), recipe
            )
            if checked["sha256"] != existing["info"]["asset"]["sha256"]:
                raise Conflict("committed recipe output changed")
            return {
                "verified": True,
                "asset_id": asset_id,
                "sha256": checked["sha256"],
                "media_manifest": existing["info"],
                "simulated": existing["info"].get("simulated", True),
            }
        guard()
        simulated = any(item["simulated"] is not False for item in inputs)
        stopped = threading.Event()

        def check():
            if stopped.is_set():
                raise Conflict("render cancelled")
            guard()

        renderer, renderer_args = (
            render_recipe,
            {"threads": self.host.settings.media_render_threads},
        )
        if recipe.renderer == "remotion":
            from .harness_remotion import render_remotion

            renderer, renderer_args = render_remotion, {"settings": self.host.settings}
        if self.host.settings.media_execution == "peer":
            from .edge_render import render_on_peer

            if self.host.side != "cloud" or job["kind"] != "compose":
                raise Conflict("peer media execution requires cloud composition")
            operation = render_on_peer(self.host, job, inputs, recipe, destination, check)
        else:
            operation = asyncio.to_thread(
                renderer, inputs, recipe, destination, check_active=check, **renderer_args
            )
        worker = asyncio.create_task(operation)
        try:
            facts = await asyncio.shield(worker)
        except asyncio.CancelledError:
            stopped.set()
            with suppress(Exception):
                await asyncio.shield(worker)
            raise
        manifest = {
            "schema_version": "cix.media-manifest/0.4",
            "task_id": business["id"],
            "task_version": business["version"],
            "asset": {
                **facts,
                "asset_id": asset_id,
                "uri": self.host.side + "-asset://" + asset_id,
                "simulated": simulated,
            },
            "recipe": recipe.model_dump(mode="json"),
            "requested_recipe": plan["requested_recipe"]
            if plan
            else business["constraints"]["media_recipe"],
            "selection_receipt": plan,
            "provenance": {
                "sources": [x["source"] for x in inputs],
                "processors": processor_versions(),
                "materialization": "parameterized_file_replay"
                if simulated
                else "composition_from_recorded_media",
            },
            "qc": {"technical_passed": True, "visual_quality_assessed": False},
            "simulated": simulated,
        }
        return await self.commit_render(job, business, destination, manifest)

    async def run_project(self, job, business, plan):
        from .harness_media_project import project_archive_path, render_project
        from .edge_render import ensure_local_asset

        try:
            existing = self.store.asset("media-" + job["id"])
        except KeyError:
            existing = None
        if existing:
            manifest = existing["info"]
            if manifest.get("project") != plan["project"]:
                raise Conflict("committed project changed")
            checked = await self.checked_io(job, file_hash, Path(existing["path"]))
            if checked != manifest["asset"]["sha256"]:
                raise Conflict("committed project output changed")
            self.host.media_storage.register(existing["path"], "edge-renders", owner=business["id"])
            return {
                "verified": True,
                "asset_id": existing["id"],
                "sha256": checked,
                "media_manifest": manifest,
                "simulated": manifest["simulated"],
            }
        inputs = [self.store.asset(item["asset_id"]) for item in plan["project"]["inputs"]]
        with ExitStack() as leases:
            leases.enter_context(
                self.host.media_storage.pin(project_archive_path(self.host, plan["project"]))
            )
            for asset in inputs:
                path = await ensure_local_asset(self.host, asset)
                leases.enter_context(self.host.media_storage.pin(path))
            needed = self.host.settings.observation_input_file_bytes + sum(
                Path(asset["path"]).stat().st_size for asset in inputs
            )
            leases.enter_context(self.host.media_storage.reserve("edge-renders", needed))
            destination, manifest = await self.checked_io(
                job, render_project, self.host, job, business, plan
            )
            result = await self.commit_render(job, business, destination, manifest)
            self.host.media_storage.register(destination, "edge-renders", owner=business["id"])
            return result

    async def commit_render(self, job, business, destination, manifest):
        facts = manifest["asset"]
        asset_id = facts["asset_id"]
        await self.before_commit(job["id"])
        self.store.guard_job(job)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self.store._task(db, business["id"], business["principal"])
            if current["version"] != job["version"] or current["status"] in {
                "paused",
                "cancelled",
                "completed",
                "expired",
            }:
                raise Conflict("recipe result superseded before publication")
            if job["kind"] == "capture":
                from .contracts import Scene

                scene = Scene.model_validate_json(
                    db.execute("SELECT value FROM metadata WHERE key='scene'").fetchone()[0]
                )
                if not scene.allowed or (
                    business["constraints"]["no_motion"] and scene.viewpoint != "front-mid"
                ):
                    raise CaptureConditionsUnavailable(
                        "scene permission expired before capture publication"
                    )
                manifest["capture_authorization"] = {
                    "scene_revision": scene.revision,
                    "field_sources": scene.field_sources,
                    "viewpoint": scene.viewpoint,
                    "checked_at": self.store.clock(),
                    "simulated": True,
                    "physical_action_executed": False,
                }
            db.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?)",
                (
                    asset_id,
                    job["id"],
                    str(destination.resolve()),
                    canonical(manifest),
                    "capture" if job["kind"] == "capture" else "deliverable",
                ),
            )
            self.store._audit(
                db,
                business["id"],
                "media.recipe.committed",
                {
                    "job_id": job["id"],
                    "asset_id": asset_id,
                    "sha256": facts["sha256"],
                    "recipe": manifest.get("recipe"),
                    "project": manifest.get("project"),
                },
            )
        destination.with_suffix(".json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2)
        )
        return {
            "verified": True,
            "asset_id": asset_id,
            "sha256": facts["sha256"],
            "media_manifest": manifest,
            "simulated": manifest["simulated"],
        }

    def assets(self, business_id, principal):
        from .harness_catalog import explicit_review_title

        if business_id:
            self.store.business_task(business_id, principal)
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT a.*,b.id AS business_task_id,j.version AS job_version,
                COALESCE(j.created,b.created) AS created,f.status AS storage_status
                FROM assets a LEFT JOIN harness_jobs j ON j.id=a.task_id
                LEFT JOIN business_tasks b ON b.id=j.task_id OR b.id=a.task_id
                LEFT JOIN managed_media_files f ON f.path=a.path
                WHERE a.task_id IS NULL OR (b.principal=? AND (? IS NULL OR (b.id=? AND
                    (j.version=b.version OR (a.kind='observation_input' AND json_extract(a.info,'$.binding.task_id')=b.id
                      AND json_extract(a.info,'$.binding.expected_version')=b.version AND json_extract(a.info,'$.binding.principal')=b.principal)
                    OR EXISTS (SELECT 1 FROM transferred_assets t
                      WHERE t.task_id=b.id AND t.task_version=b.version AND t.asset_id=json_extract(a.info,'$.remote_asset_id'))))))""",
                (principal, business_id, business_id),
            ).fetchall()
        assets = [
            {
                **{k: v for k, v in {**dict(r), "info": json.loads(r["info"])}.items()
                   if k not in {"path", "storage_status"}},
                "availability": self.host.media_storage.availability(
                    r["path"], json.loads(r["info"]), r["storage_status"]
                ),
            }
            for r in rows
        ]
        for asset in assets:
            info = asset["info"]
            if not info.get("title"):
                title = explicit_review_title(
                    info.get("qc", {}).get("visual_review", {}).get("notes")
                )
                if title:
                    info["title"] = title
        return assets

    def composition_assets(self, business_id, principal):
        available = {a["id"]: a for a in self.assets(business_id, principal)}
        for asset in self.assets(None, principal):
            if asset["kind"] == "deliverable":
                available.setdefault(asset["id"], asset)
        return list(available.values())

    async def process_one(self):
        job = self.store.claim_job()
        if job is None:
            return False
        try:
            return await self._process_claimed(job)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from .harness_worker_recovery import finish_failed_job

            return await finish_failed_job(self.host, job, exc)

    async def _process_claimed(self, job):
        try:
            result = await self.run(job)
        except asyncio.CancelledError:
            raise
        except CaptureConditionsUnavailable:
            active = self.store.defer_capture_job(job)
            scene = self.store.scene()
            if active and scene.allowed:
                task = self.store.business_task(job["task_id"], job["principal"])
                if not task["constraints"]["no_motion"] or scene.viewpoint == "front-mid":
                    self.store.submit(
                        "scene-resume-" + job["id"] + "-" + str(scene.revision),
                        job["principal"],
                        "system",
                        "scene.changed",
                        {
                            "text": "采集中断期间执行条件已恢复，请核验当前状态后继续原步骤。",
                            "task_version": job["version"],
                        },
                        task_id=job["task_id"],
                    )
            return True
        except Exception as exc:
            error = type(exc).__name__
            result = {"error": error, "simulated": True}
            if job["kind"] == "compose" and self.host.settings.media_review_required:
                result["error_detail"] = str(exc)[:1600]
        else:
            error = None
        if job["kind"] == "compose" and self.host.settings.media_review_required:
            result = {
                **result,
                "review_required": True,
                "review_status": "render_failed" if error else "pending",
            }
        if not error and result.get("media_manifest"):
            task = self.store.business_task(job["task_id"], job["principal"])
            result = {
                **result,
                "operation": job["kind"],
                "asset_side": self.host.side,
                "stage": "capture_step"
                if job["kind"] == "capture" and task["kind"] in {"goal", "compose"}
                else "period_complete"
                if task["kind"] == "goal"
                else "completed",
            }
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            status = self.store.finish_job(job, result, error=error, _db=db)
            if status != "superseded" and not result.get("review_required"):
                self.host.mail.job_finished(db, job, result, error)
        return True

    def load_fixtures(self):
        source_root = self.store.root.parent / "source"
        original = source_root / "cat-track.webm"
        record = source_root / "source.json"
        if original.is_file() and record.is_file():
            from .harness_media_contracts import SourceRegistration

            provenance = json.loads(record.read_text())
            if file_hash(original) != provenance["sha256"]:
                raise Conflict("registered baseline source checksum mismatch")
            self.host.sources.register(
                "local-user",
                SourceRegistration(
                    source_id="licensed-cat",
                    path=str(original),
                    title=provenance["title"],
                    source_kind="licensed_stock_video",
                    source_ref=provenance["source_page"],
                    license=provenance["license"],
                    author=provenance["author"],
                ),
            )
        wide = self.store.root / "fixtures/wide.mp4"
        if self.host.side == "cloud" and wide.is_file():
            info = json.loads(wide.with_suffix(".json").read_text())
            if file_hash(wide) != info["asset"]["sha256"]:
                raise Conflict("fixture checksum mismatch")
            self.store.put_asset(info["asset"]["asset_id"], None, wide, info, "wide")
