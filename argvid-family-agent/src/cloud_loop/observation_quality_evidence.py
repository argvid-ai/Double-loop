"""Read-only quality evidence from recorded runs and verified local files."""

import json
import math
import sqlite3
from pathlib import Path

from .contracts import Contract, digest
from .media import file_hash
from .observation_contracts import Reference, Version
from .observation_media import _fingerprint
from .observation_quality import merged, validate_labels


class QualityRun(Contract):
    partition_id: Reference
    database: str
    task_id: Reference
    version: Version
    generation: Version
    principal: Reference = "local-user"


def bind_quality(store, task_id, source_ref, manifest, labels, manifest_sha256, baseline_id):
    labels = validate_labels(manifest, labels, manifest_sha256, baseline_id)
    if source_ref not in {item.partition_id for item in labels.intervals}:
        raise ValueError("task is outside labelled partitions")
    if labels.frozen_at.timestamp() > store.clock():
        raise ValueError("labels cannot be frozen in the future")
    binding = {
        "task_id": task_id,
        "partition_id": source_ref,
        "labels_digest": digest(labels),
        "manifest_sha256": manifest_sha256,
        "baseline_id": baseline_id,
        "created": store.clock(),
    }
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute(
            "SELECT 1 FROM observation_media WHERE task_id=? LIMIT 1", (task_id,)
        ).fetchone():
            raise ValueError("labels must be bound before any input is observed")
        db.execute("""CREATE TABLE IF NOT EXISTS observation_quality_bindings (
            task_id TEXT PRIMARY KEY,body TEXT NOT NULL)""")
        db.execute(
            "INSERT INTO observation_quality_bindings VALUES (?,?)",
            (task_id, json.dumps(binding, sort_keys=True)),
        )
    return binding


def load_run(run, manifest, labels, manifest_sha256):
    run = QualityRun.model_validate(run)
    path = Path(run.database).resolve(strict=True)
    if not path.is_file():
        raise ValueError("run database must already exist")
    part = next(part for part in manifest["partitions"] if part["id"] == run.partition_id)
    expected = manifest["sources"][part["source"]]["sha256"]
    # URI is fully escaped by as_uri; mode=ro cannot create a missing database.
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        row = db.execute(
            "SELECT * FROM observation_tasks WHERE task_id=? AND principal=?",
            (run.task_id, run.principal),
        ).fetchone()
        if (
            not row
            or row["version"] != run.version
            or row["generation"] != run.generation
            or row["status"] != "stopped"
        ):
            raise ValueError("score an explicitly stopped matching task version/generation")
        config = json.loads(row["config"])
        if config["source_ref"] != run.partition_id:
            raise ValueError("task source differs from validation partition")
        entry = db.execute(
            "SELECT body FROM observation_quality_bindings WHERE task_id=?", (run.task_id,)
        ).fetchone()
        binding = json.loads(entry[0]) if entry else {}
        if (
            binding.get("labels_digest") != digest(labels)
            or binding.get("manifest_sha256") != manifest_sha256
            or binding.get("partition_id") != run.partition_id
            or binding.get("baseline_id") != labels.baseline_id
        ):
            raise ValueError("run does not carry the frozen pre-run label binding")
        rows = []
        for row in db.execute(
            "SELECT * FROM observation_media WHERE task_id=? AND kind='batch' ORDER BY sequence",
            (run.task_id,),
        ):
            body = json.loads(row["body"])
            if body["version"] == run.version and body["generation"] == run.generation:
                if body["principal"] != run.principal or row["source_ref"] != run.partition_id:
                    raise ValueError("batch ownership differs")
                if row["received_at"] < binding["created"]:
                    raise ValueError("observation predates frozen label binding")
                rows.append((dict(row), body))
        if not rows or len({row["segment_id"] for row, _ in rows}) != 1:
            raise ValueError(
                "quality requires one non-repeating continuous source segment per partition"
            )
        segment = rows[0][0]["segment_id"]
        fingerprints = {tuple(body["source_fingerprint"]) for _, body in rows}
        if len(fingerprints) != 1:
            raise ValueError("source changed during scored run")
        fingerprint = next(iter(fingerprints))
        source = None
        for record in db.execute(
            "SELECT * FROM observation_verified_files WHERE sha256=?", (expected,)
        ):
            if tuple(json.loads(record["fingerprint"])) == fingerprint:
                selected = Path(record["path"])
                if (
                    _fingerprint(selected) == fingerprint
                    and file_hash(selected) == expected
                    and _fingerprint(selected) == fingerprint
                ):
                    source = selected
                    break
        if source is None:
            raise ValueError("original source bytes/fingerprint cannot be verified")
        observed = {
            "source_sha256": expected,
            "seen": [],
            "saved": [],
            "cleaned": [],
            "decisions": {"keep": [], "discard": [], "uncertain": []},
            "actual_inference": True,
            "first_decisions": {"keep": [], "discard": [], "uncertain": []},
            "binding": {
                **run.model_dump(),
                "database": str(path),
                "labels_bound_at": binding["created"],
            },
            "saved_files": [],
        }
        batch_ids = {r["entry_id"]: (r, b) for r, b in rows}
        for row, body in rows:
            if row["seen"] is not None:
                seen = json.loads(row["seen"])
                if (
                    seen["version"] != run.version
                    or seen["generation"] != run.generation
                    or seen["principal"] != run.principal
                ):
                    raise ValueError("seen receipt has another execution identity")
                observed["seen"].append((row["start_ms"], row["end_ms"]))
                observed["actual_inference"] &= seen["inference_simulated"] is False
        initial = set()
        for record in db.execute(
            "SELECT payload FROM audit WHERE task_id=? AND kind='observation.decision' ORDER BY seq",
            (run.task_id,),
        ):
            value = json.loads(record[0])
            context = value["context"]
            arguments = value["arguments"]
            if (
                context["batch_id"] not in batch_ids
                or context["version"] != run.version
                or context["generation"] != run.generation
            ):
                continue
            if context["principal"] != run.principal:
                raise ValueError("initial decision belongs to another principal")
            if context["mode"] != "live" or value["decision_id"] in initial:
                continue
            batch = batch_ids[context["batch_id"]][0]
            if (
                not batch["start_ms"]
                <= arguments["start_ms"]
                < arguments["end_ms"]
                <= batch["end_ms"]
            ):
                raise ValueError("initial decision exceeds batch")
            initial.add(value["decision_id"])
            observed["first_decisions"][arguments["decision"]].append(
                (arguments["start_ms"], arguments["end_ms"])
            )
            observed["actual_inference"] &= context["inference_simulated"] is False
        for row in db.execute(
            "SELECT * FROM observation_decisions WHERE task_id=? AND version=?",
            (run.task_id, run.version),
        ):
            if row["batch_id"] not in batch_ids:
                continue
            context = json.loads(row["context"])
            batch = batch_ids[row["batch_id"]][0]
            if (
                context["generation"] != run.generation
                or context["principal"] != run.principal
                or row["source_ref"] != run.partition_id
                or row["segment_id"] != segment
            ):
                raise ValueError("decision execution identity differs")
            if batch["seen"] is None:
                raise ValueError("decision has no committed observation receipt")
            if not batch["start_ms"] <= row["start_ms"] < row["end_ms"] <= batch["end_ms"]:
                raise ValueError("decision outside observed batch")
            observed["decisions"][row["decision"]].append((row["start_ms"], row["end_ms"]))
            observed["actual_inference"] &= context["inference_simulated"] is False
        for row in db.execute(
            "SELECT * FROM observation_exports WHERE task_id=? AND status='saved'", (run.task_id,)
        ):
            selection = json.loads(row["body"])
            if (
                selection["version"],
                selection["generation"],
                selection["source_ref"],
                selection["segment_id"],
            ) != (run.version, run.generation, run.partition_id, segment):
                continue
            if selection["principal"] != run.principal:
                raise ValueError("saved media ownership differs")
            asset = db.execute("SELECT * FROM assets WHERE id=?", (row["id"],)).fetchone()
            if not asset:
                raise ValueError("saved export has no asset index")
            info = json.loads(asset["info"])
            verified = json.loads(row["verified"])
            if (
                asset["kind"] != "observation"
                or asset["task_id"] != run.task_id
                or info["selection"] != selection
                or info["task_id"] != run.task_id
                or info["project_id"] != row["project_id"]
                or any(info.get(key) != value for key, value in verified.items())
            ):
                raise ValueError("saved index does not match export verification")
            storage = db.execute(
                "SELECT root FROM observation_storage WHERE id=? AND principal=? AND project_id=?",
                (row["binding_ref"], run.principal, row["project_id"]),
            ).fetchone()
            if not storage:
                raise ValueError("saved media has no registered storage root")
            file = Path(asset["path"])
            storage_root = Path(storage["root"])
            if (
                file.resolve() != file
                or storage_root.resolve() != storage_root
                or not file.is_relative_to(storage_root)
                or file != storage_root / row["relative_path"]
            ):
                raise ValueError("saved file escapes registered storage")
            current_file = _fingerprint(file)
            # Removing the export staging hardlink changes ctime after indexing.
            # Rehash the stable current file; inode/size/mtime must still match.
            if (
                tuple(info["source_fingerprint"]) != fingerprint
                or tuple(info["asset"]["file_fingerprint"])[:4] != current_file[:4]
                or file_hash(file) != info["asset"]["sha256"]
                or info["asset"]["sha256"] != verified["sha256"]
                or current_file != _fingerprint(file)
            ):
                raise ValueError("saved file or provenance changed")
            if (
                not info.get("file_bytes_real")
                or info["visible_video_end_ms"] <= info["visible_video_start_ms"]
            ):
                raise ValueError("saved video lacks verified source-time coverage")
            # Audio and video must both cover the interval credited as retained.
            audio = info["tracks"]["audio"]
            origin = info["output_timeline_origin_ms"]
            coverage = info["audio_sample_coverage"]["contiguous_sample_coverage_seconds"]
            if (
                type(origin) not in {int, float}
                or type(coverage) not in {int, float}
                or not math.isfinite(origin)
                or not math.isfinite(coverage)
                or coverage <= 0
            ):
                raise ValueError("saved audio lacks continuous sample proof")
            start = max(info["visible_video_start_ms"], audio["source_start_ms"], origin)
            end = min(
                info["visible_video_end_ms"], audio["source_end_ms"], origin + coverage * 1000
            )
            if end > start:
                observed["saved"].append((start, end))
            observed["actual_inference"] &= selection["inference_simulated"] is False
            observed["saved_files"].append(
                {
                    "id": row["id"],
                    "sha256": info["asset"]["sha256"],
                    "source_start_ms": start,
                    "source_end_ms": end,
                }
            )
        has_gc = db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='observation_cache_evictions'"
        ).fetchone()
        if has_gc:
            for row in db.execute(
                "SELECT * FROM observation_cache_evictions WHERE state='deleted'"
            ):
                reclamation = json.loads(row["evidence"])
                payload = reclamation["payload"]
                body = payload["batch"]
                if body["task_id"] != run.task_id or body["batch_id"] not in batch_ids:
                    continue
                if (
                    body["principal"],
                    body["version"],
                    body["generation"],
                    body["source_ref"],
                    body["segment_id"],
                ) != (run.principal, run.version, run.generation, run.partition_id, segment):
                    raise ValueError("cache reclamation execution differs")
                batch = batch_ids[body["batch_id"]][0]
                if (
                    batch["seen"] is None
                    or tuple(body["source_fingerprint"]) != fingerprint
                    or row["reclaimed_bytes"] <= 0
                    or row["reclaimed_bytes"] != reclamation["bytes"]
                    or any(
                        Path(row[key]).exists() or Path(row[key]).is_symlink()
                        for key in ("directory", "trash_path")
                    )
                ):
                    raise ValueError("cache reclamation not verified")
                observed["cleaned"].append((batch["start_ms"], batch["end_ms"]))
        for key in ("seen", "saved", "cleaned"):
            observed[key] = merged(observed[key])
        return observed
    finally:
        db.close()
