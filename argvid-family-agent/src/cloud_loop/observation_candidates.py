"""Continuous candidate views and explicit review decisions over immutable markers."""

from typing import Literal
import json

from pydantic import Field

from .contracts import Contract, digest
from .observation_contracts import Reference, Version
from .store import Conflict


class ReviewedMarker(Contract):
    marker_id: Reference
    revision: Version


class ReviewObservationCandidates(Contract):
    task_id: Reference = Field(description="录像任务ID，复制candidate_review_identity.task_id；不是业务剪辑任务ID")
    task_version: Version = Field(description="录像任务版本，复制candidate_review_identity.task_version；不是业务任务版本")
    markers: list[ReviewedMarker] = Field(min_length=1, max_length=500)
    disposition: Literal["retain", "release"]
    reason: str = Field(min_length=1, max_length=1000)
    used_asset_id: Reference | None = None


def candidate_groups(markers, *, has_more=False, after_sequence=-1):
    """Group only exact temporal continuity; pages remain explicit partial views."""
    groups = []
    for marker in markers:
        key = (marker["source_ref"], marker["decision"], marker.get("review_disposition"))
        previous = groups[-1] if groups else None
        if previous is None or previous["_key"] != key or previous["end_ms"] != marker["start_ms"]:
            previous = {
                "_key": key,
                "source_ref": marker["source_ref"],
                "decision": marker["decision"],
                "start_ms": marker["start_ms"],
                "end_ms": marker["end_ms"],
                "markers": [],
                "media_parts": [],
            }
            groups.append(previous)
        previous["end_ms"] = marker["end_ms"]
        previous["markers"].append(
            {
                "marker_id": marker["id"],
                "revision": marker["revision"],
                "start_ms": marker["start_ms"],
                "end_ms": marker["end_ms"],
                "media_ref": marker["media_ref"],
                "evidence_ref": marker["evidence_ref"],
            }
        )
        for media_ref in marker["media_parts"]:
            if media_ref not in previous["media_parts"]:
                previous["media_parts"].append(media_ref)
    for index, group in enumerate(groups):
        group["review_disposition"] = group.pop("_key")[2]
        group["candidate_id"] = "candidate-" + digest(group["markers"])[:32]
        group["may_continue_before_page"] = index == 0 and after_sequence >= 0
        group["may_continue_after_page"] = index == len(groups) - 1 and has_more
    return groups


class CandidateReviews:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS observation_candidate_reviews (
                marker_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                disposition TEXT NOT NULL, reason TEXT NOT NULL,
                used_asset_id TEXT, updated REAL NOT NULL)""")

    def apply(self, principal, request):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute(
                "SELECT version FROM observation_tasks WHERE task_id=? AND principal=?",
                (request.task_id, principal),
            ).fetchone()
            if not task or task["version"] != request.task_version:
                has_markers = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='observation_decisions'").fetchone()
                first = db.execute(
                    """SELECT d.task_id,t.version FROM observation_decisions d
                    JOIN observation_tasks t ON t.task_id=d.task_id
                    WHERE d.id=? AND t.principal=?""", (request.markers[0].marker_id, principal),
                ).fetchone() if has_markers else None
                details = {
                    "error": "observation_identity_mismatch",
                    "message": "录像任务ID或version不匹配；不能使用剪辑业务任务身份。",
                    "expected_recording": dict(first) if first else None,
                    "changed": False,
                }
                if first:
                    details["expected_recording"] = {"task_id": first["task_id"], "task_version": first["version"]}
                raise Conflict("candidate_review:" + json.dumps(details, ensure_ascii=False))
            for item in request.markers:
                marker = db.execute(
                    "SELECT revision FROM observation_decisions WHERE id=? AND task_id=? AND version=?",
                    (item.marker_id, request.task_id, request.task_version),
                ).fetchone()
                if not marker or marker["revision"] != item.revision:
                    raise Conflict("candidate marker revision changed")
            if (
                request.disposition == "retain"
                and db.execute("SELECT 1 FROM sqlite_master WHERE name='raw_recordings'").fetchone()
            ):
                media_refs = set()
                for item in request.markers:
                    row = db.execute(
                        "SELECT m.body FROM observation_media m JOIN observation_decisions d ON d.task_id=m.task_id AND d.batch_id=m.entry_id WHERE d.id=?",
                        (item.marker_id,),
                    ).fetchone()
                    body = json.loads(row[0])
                    media_refs.update(body.get("media_parts") or [body["media_ref"]])
                for media_ref in media_refs:
                    raw = db.execute(
                        "SELECT state FROM raw_recordings WHERE media_ref=?", (media_ref,)
                    ).fetchone()
                    if raw and raw[0] != "available":
                        raise Conflict("candidate source has expired; protection cannot restore it")
                shared = getattr(self.store, "shared_media_storage", None)
                if shared:
                    shared.promote(self.store.input_media, principal, media_refs, request.reason)
            for item in request.markers:
                db.execute(
                    "INSERT OR REPLACE INTO observation_candidate_reviews VALUES (?,?,?,?,?,?)",
                    (
                        item.marker_id,
                        item.revision,
                        request.disposition,
                        request.reason,
                        request.used_asset_id,
                        self.store.clock(),
                    ),
                )
            self.store._audit(
                db,
                request.task_id,
                "observation.candidates.reviewed",
                {
                    "principal": principal,
                    **request.model_dump(mode="json"),
                },
            )
        shared = getattr(self.store, "shared_media_storage", None)
        if shared and request.disposition == "release":
            # Other retained markers may reference the same physical original.
            # Recheck after the review commit and serialize against later reviews.
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                retained, requested = set(), set()
                for row in db.execute(
                    "SELECT d.id,r.disposition,m.body FROM observation_decisions d "
                    "JOIN observation_tasks t ON t.task_id=d.task_id "
                    "JOIN observation_media m ON m.task_id=d.task_id AND m.entry_id=d.batch_id "
                    "JOIN observation_candidate_reviews r ON r.marker_id=d.id AND r.revision=d.revision "
                    "WHERE t.principal=?", (principal,)
                ):
                    body = json.loads(row["body"])
                    refs = body.get("media_parts") or [body["media_ref"]]
                    if row["disposition"] == "retain":
                        retained.update(refs)
                    if row["id"] in {item.marker_id for item in request.markers}:
                        requested.update(refs)
                shared.release_candidates(self.store.input_media, principal, requested - retained)
        return {
            "applied": True,
            "markers_reviewed": len(request.markers),
            "disposition": request.disposition,
            "files_deleted": False,
        }
