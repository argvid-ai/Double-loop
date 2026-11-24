"""Delivered visual evidence and bounded review of a versioned editing task."""

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract, ID, canonical
from .media import file_hash
from .store import Conflict


class ReviewRenderedMedia(Contract):
    task_id: str = Field(pattern=ID)
    expected_version: int = Field(ge=1)
    asset_id: str | None = Field(default=None, pattern=ID)
    action: Literal["approve", "revise"]
    notes: str = Field(min_length=1, max_length=2000)
    title: str | None = Field(
        default=None,
        min_length=1,
        max_length=24,
        description="批准时给成片一个简短内容标题，不含日期、编号或任务指令；日期由界面添加。",
    )

    @model_validator(mode="after")
    def title_from_explicit_note(self):
        if not self.title:
            from .harness_catalog import explicit_review_title

            value = explicit_review_title(self.notes)
            if value:
                object.__setattr__(self, "title", value)
        return self


def coverage_error(positions, start, end, max_gap_ms=2000):
    """A stated sparse-review policy, never a claim of frame-by-frame coverage."""
    points = sorted({p for p in positions if start <= p < end})
    edge = max_gap_ms // 2
    if len(points) < 2:
        return "inspect at least two distinct actual frames inside the selected range"
    if points[0] - start > edge or end - points[-1] > edge:
        return "inspect nearer the selected range's start/end"
    if any(b - a > max_gap_ms for a, b in zip(points, points[1:])):
        return "inspect additional frames inside the selected range"
    return None


def record_delivered(store, event, request_id, receipts):
    """Called only after a successful model response to the actual image request."""
    with store.connect() as db:
        for call_id, receipt in receipts.items():
            binding = receipt.get("review_binding")
            if not binding:
                continue
            task = db.execute(
                "SELECT version,status FROM business_tasks WHERE id=? AND principal=?",
                (binding["task_id"], event["principal"]),
            ).fetchone()
            if (
                not task
                or task["version"] != binding["version"]
                or task["status"] in {"paused", "cancelled", "expired", "completed"}
            ):
                continue
            db.execute(
                "INSERT OR IGNORE INTO media_frame_deliveries VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    event["principal"],
                    binding["task_id"],
                    binding["version"],
                    receipt["asset_id"],
                    binding["sha256"],
                    call_id,
                    request_id,
                    canonical(sorted({f["actual_position_ms"] for f in receipt["frames"]})),
                    store.clock(),
                ),
            )


class MediaReview:
    def __init__(self, host):
        self.host, self.store = host, host.store
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS media_frame_deliveries (
                    principal TEXT NOT NULL,task_id TEXT NOT NULL,version INTEGER NOT NULL,
                    asset_id TEXT NOT NULL,asset_sha TEXT NOT NULL,call_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,positions TEXT NOT NULL,created REAL NOT NULL,
                    PRIMARY KEY(principal,task_id,version,request_id,call_id));
                CREATE INDEX IF NOT EXISTS media_frame_lookup ON media_frame_deliveries
                    (principal,task_id,version,asset_id,asset_sha);
                CREATE TABLE IF NOT EXISTS media_review_decisions (
                    task_id TEXT NOT NULL,version INTEGER NOT NULL,asset_id TEXT,
                    verdict TEXT NOT NULL,notes TEXT NOT NULL,event_id TEXT NOT NULL,
                    result TEXT NOT NULL,PRIMARY KEY(task_id,version));
            """)

    def evidence(self, task, asset_id, sha, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self.evidence(task, asset_id, sha, connection)
        rows = db.execute(
            "SELECT request_id,positions FROM media_frame_deliveries WHERE principal=? "
            "AND task_id=? AND version=? AND asset_id=? AND asset_sha=?",
            (task["principal"], task["id"], task["version"], asset_id, sha),
        ).fetchall()
        return sorted({p for r in rows for p in json.loads(r["positions"])}), sorted(
            {r["request_id"] for r in rows}
        )

    def require_inputs(self, task, recipe):
        if (
            not self.host.settings.media_review_required
            or self.host.settings.media_review_policy == "model"
        ):
            return
        with self.store.connect() as db:
            previous = db.execute(
                "SELECT result FROM media_review_decisions WHERE task_id=? AND verdict='revise' "
                "AND json_extract(result,'$.task_version')=?",
                (task["id"], task["version"]),
            ).fetchone()
        if previous:
            specification = json.loads(previous["result"])["output_spec"]
            if any(getattr(recipe, key) != value for key, value in specification.items()):
                raise Conflict(
                    "automatic revision must preserve the accepted output specification and renderer"
                )
        available = {a["id"]: a for a in self.host.effects.assets(task["id"], task["principal"])}
        for segment in recipe.segments:
            asset = available.get(segment.asset_id)
            if not asset or asset["kind"] != "observation_input":
                raise Conflict(
                    "reviewed edits require attached observation candidates; attach the selected raw range first"
                )
            positions, _ = self.evidence(task, asset["id"], asset["info"]["asset"]["sha256"])
            error = coverage_error(
                positions,
                segment.start_ms,
                segment.start_ms + segment.duration_ms,
                self.host.settings.media_review_max_gap_ms,
            )
            if error:
                raise Conflict(
                    f"visual review missing for {asset['id']} [{segment.start_ms},{segment.start_ms + segment.duration_ms})ms: {error}; max gap {self.host.settings.media_review_max_gap_ms}ms"
                )

    def decide(self, event, args):
        if event["read_only"] or not self.host.settings.media_review_required:
            raise Conflict("this event/deployment cannot approve or revise an edit")
        if event["kind"] not in {"user", "voice", "media.review", "autonomy", "work.message",
                                 "timer", "storage.review"}:
            raise Conflict("review requires an authorized editing turn")
        if event["kind"] == "media.review" and event["task_id"] != args.task_id:
            raise Conflict("review turn is bound to its own task")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.store.event(event["id"], event["principal"], _db=db)["status"] != "running":
                raise Conflict("review input is no longer running")
            task = self.store._task(db, args.task_id, event["principal"])
            previous = db.execute(
                "SELECT * FROM media_review_decisions WHERE task_id=? AND version=?",
                (args.task_id, args.expected_version),
            ).fetchone()
            if previous:
                result = json.loads(previous["result"])
                if (
                    previous["verdict"] == args.action
                    and previous["asset_id"] == args.asset_id
                    and task["version"] == result["task_version"]
                ):
                    return result
                raise Conflict("review decision already committed or task changed")
            if task["version"] != args.expected_version or task["status"] != "ready":
                raise Conflict("review task changed or does not await review")
            job = db.execute(
                "SELECT * FROM harness_jobs WHERE task_id=? AND version=? AND kind='compose'",
                (task["id"], task["version"]),
            ).fetchone()
            receipt = task.get("result") or {}
            if (
                not job
                or job["status"] not in {"completed", "failed"}
                or not receipt.get("review_required")
            ):
                raise Conflict("no current render awaits review")
            if args.action == "approve":
                if (
                    job["status"] != "completed"
                    or not receipt.get("verified")
                    or args.asset_id != receipt.get("asset_id")
                ):
                    raise Conflict("only the current technically verified render can be approved")
                asset = db.execute(
                    "SELECT * FROM assets WHERE id=? AND task_id=?", (args.asset_id, job["id"])
                ).fetchone()
                if not asset:
                    raise Conflict("render asset is not owned by this job")
                info = json.loads(asset["info"])
                path = Path(asset["path"])
                if (
                    path.resolve() != path
                    or path.parent
                    not in {self.store.root / "assets", self.store.root / "edge-renders"}
                    or file_hash(path) != info["asset"]["sha256"]
                ):
                    raise Conflict("render bytes changed after review")
                positions, requests = self.evidence(
                    task, args.asset_id, info["asset"]["sha256"], db
                )
                if not positions:
                    raise Conflict(
                        "final video review missing: view the actual output before approval"
                    )
                if self.host.settings.media_review_policy == "coverage" and info.get("recipe"):
                    start = 0
                    for segment in info["recipe"]["segments"]:
                        end = start + segment["duration_ms"]
                        error = coverage_error(
                            positions, start, end, self.host.settings.media_review_max_gap_ms
                        )
                        if error:
                            raise Conflict(
                                f"final video review missing in [{start},{end})ms: {error}"
                            )
                        start = end
                review = {
                    "status": "approved",
                    "coverage": "sampled_frames_only",
                    "notes": args.notes,
                    "model_request_ids": requests,
                    "positions_ms": positions,
                    "sampling_policy": self.host.settings.media_review_policy,
                    "event_id": event["id"],
                }
                info["qc"] = {
                    **info["qc"],
                    "visual_quality_assessed": True,
                    "visual_review": review,
                }
                if args.title:
                    from .harness_catalog import media_title

                    info["title"] = media_title(args.title)
                db.execute("UPDATE assets SET info=? WHERE id=?", (canonical(info), args.asset_id))
                approved = {
                    **receipt,
                    "review_status": "approved",
                    "visual_review": review,
                    "media_manifest": info,
                }
                db.execute(
                    "UPDATE business_tasks SET status=?,result=?,updated=? WHERE id=?",
                    (
                        "ready" if task["kind"] == "goal" else "completed",
                        canonical(approved),
                        self.store.clock(),
                        task["id"],
                    ),
                )
                self.host.mail.job_finished(db, dict(job), approved, None)
                result = {
                    "status": "completed",
                    "task_id": task["id"],
                    "task_version": task["version"],
                    "asset_id": args.asset_id,
                    "result": approved,
                }
            else:
                output = db.execute(
                    "SELECT start_version FROM media_outputs WHERE task_id=? AND version=?",
                    (task["id"], task["version"]),
                ).fetchone()
                used = db.execute(
                    "SELECT COUNT(*) FROM media_review_decisions WHERE task_id=? AND verdict='revise' AND version>=?",
                    (task["id"], output["start_version"] if output else 0),
                ).fetchone()[0]
                limit = self.host.settings.media_review_max_revisions
                if limit is not None and used >= limit:
                    raise Conflict(
                        "automatic revision limit reached; report unfinished work to the user"
                    )
                if job["status"] == "completed" and event["kind"] == "media.review":
                    current_asset = args.asset_id or receipt.get("asset_id")
                    if current_asset != receipt.get("asset_id"):
                        raise Conflict("revision must identify the current render")
                    positions, _ = self.evidence(
                        task, current_asset, receipt["media_manifest"]["asset"]["sha256"], db
                    )
                    minimum = 2 if self.host.settings.media_review_policy == "coverage" else 1
                    if len(positions) < minimum:
                        raise Conflict(
                            "inspect the rendered video before requesting a visual revision"
                        )
                if task["kind"] not in {"goal", "edit"} or task["constraints"].get("media_recipe"):
                    raise Conflict(
                        "automatic revision cannot rewrite an explicit user recipe or a non-edit task"
                    )
                version = task["version"] + 1
                plan = db.execute(
                    "SELECT plan FROM harness_job_plans WHERE job_id=?", (job["id"],)
                ).fetchone()
                frozen = json.loads(plan["plan"]).get("recipe", {})
                output_spec = (
                    {
                        k: frozen.get(k, "ffmpeg" if k == "renderer" else None)
                        for k in ("width", "height", "fps", "audio", "renderer")
                    }
                    if self.host.settings.media_review_policy == "coverage"
                    else {}
                )
                db.execute(
                    "UPDATE business_tasks SET version=?,status='ready',result=NULL,updated=? WHERE id=?",
                    (version, self.store.clock(), task["id"]),
                )
                db.execute(
                    "INSERT OR REPLACE INTO run_task_versions VALUES (?,?,?)",
                    (event["id"], task["id"], version),
                )
                db.execute("UPDATE media_outputs SET version=? WHERE task_id=? AND version=?",
                           (version, task["id"], task["version"]))
                self.store._history(
                    db,
                    f"task:{task['id']}:{version}",
                    event["principal"],
                    task["id"],
                    "media.revision",
                    args.notes,
                    event["id"],
                    self.store.clock(),
                )
                result = {
                    "status": "revision_ready",
                    "task_id": task["id"],
                    "task_version": version,
                    "output_spec": output_spec,
                    "instruction": "Continue the saved user goal in this new version. Choose the edits and review needed, then submit the revised project or recipe.",
                }
            db.execute(
                "INSERT INTO media_review_decisions VALUES (?,?,?,?,?,?,?)",
                (
                    task["id"],
                    task["version"],
                    args.asset_id,
                    args.action,
                    args.notes,
                    event["id"],
                    canonical(result),
                ),
            )
            self.store._audit(db, task["id"], "media.review.decided", result)
        if args.action == "revise":
            event.setdefault("_task_versions", {})[task["id"]] = result["task_version"]
        elif getattr(self.store, "shared_media_storage", None):
            # Publication is already committed. Cleanup failure must not turn a
            # delivered film into a failed/replayed editing operation.
            try:
                self.host.media_storage.collect("observation-inputs")
            except Exception as exc:
                self.store.audit(task["id"], "media.cleanup.pending",
                                 {"error_type": type(exc).__name__})
        return result
