"""Cross-task observation evidence, with explicit candidate and clock provenance.

Only a consuming runner may write facts. Cloud tools query evidence, not memory
guesses. Searching a candidate neither proves object identity nor proves absence.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import Contract, canonical, digest
from .observation_contracts import Reference
from .store import Conflict


class EvidenceDescription(Contract):
    text: str = Field(min_length=1, max_length=2000)
    labels: tuple[str, ...] = Field(default=(), max_length=20)
    entity_refs: tuple[Reference, ...] = Field(default=(), max_length=16)

    @field_validator("labels")
    @classmethod
    def bounded_labels(cls, values):
        if any(not v.strip() or len(v) > 80 for v in values):
            raise ValueError("evidence labels must be short nonempty text")
        return values


class EvidenceSearch(Contract):
    query: str = Field(default="", max_length=300)
    entity_ref: Reference | None = None
    source_ref: Reference | None = None
    location_ref: Reference | None = None
    decision: Literal["keep", "discard", "uncertain"] | None = "keep"
    start: datetime | None = None
    end: datetime | None = None
    after_id: Reference | None = None
    limit: int = Field(default=20, strict=True, ge=1, le=100)

    @model_validator(mode="after")
    def window(self):
        if (self.start is None) != (self.end is None):
            raise ValueError("provide both recording-time boundaries")
        for value in (self.start, self.end):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("evidence time requires timezone")
        if self.start is not None and self.end <= self.start:
            raise ValueError("empty evidence time window")
        return self


class PeriodReview(Contract):
    start: datetime
    end: datetime
    source_ref: Reference | None = None
    location_ref: Reference | None = None
    after_id: Reference | None = None
    limit: int = Field(default=50, strict=True, ge=1, le=100)

    @model_validator(mode="after")
    def window(self):
        EvidenceSearch(start=self.start, end=self.end)
        if (self.end - self.start).total_seconds() > 7 * 86400:
            raise ValueError("one review is limited to seven days; paginate separate periods")
        return self


def merged_ms(ranges):
    total = 0
    right = None
    for start, end in sorted(ranges):
        if right is None or start > right:
            total += end - start
        else:
            total += max(0, end - right)
        right = max(right or end, end)
    return round(total * 1000)


class ObservationEvidence:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS observation_facts (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, task_id TEXT NOT NULL,
                    batch_id TEXT NOT NULL, source_ref TEXT NOT NULL, location_ref TEXT,
                    recorded_start REAL,recorded_end REAL,sort_time REAL NOT NULL,
                    decision TEXT NOT NULL,body TEXT NOT NULL,search_text TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS observation_fact_time ON observation_facts(principal,sort_time,id);
                CREATE INDEX IF NOT EXISTS observation_fact_source ON observation_facts(principal,source_ref,recorded_start);
            """)

    def record(self, ledger, context, batch, description, *, decision, is_current, part_id="main"):
        """Called after actual provider/tool receipts, never exposed as a tool."""
        description = EvidenceDescription.model_validate(description.model_dump())
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = ledger._task(db, context.principal, context.task_id)
            ledger._guard(task, context, running=True)
            if not is_current():
                raise Conflict("observation evidence invocation ended")
            proof = db.execute(
                """SELECT kind,payload FROM audit WHERE task_id=?
                AND kind IN ('model.response','perception.fusion','simulation.inference')
                AND (json_extract(payload,'$.request_id')=? OR json_extract(payload,'$.evidence_ref')=?)
                ORDER BY seq DESC LIMIT 1""",
                (context.task_id, context.evidence_ref, context.evidence_ref),
            ).fetchone()
            if proof is None or (
                proof["kind"] == "simulation.inference" and not context.inference_simulated
            ):
                raise Conflict(
                    "observation evidence requires an actual provider/explicit simulation receipt"
                )
            captured = ledger._batch(db, context.task_id, context.batch_id)
            if json.loads(captured["body"]) != batch.model_dump(mode="json"):
                raise Conflict("evidence differs from consumed input")
            record_start = record_end = location = raw_ref = clock = None
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='managed_input_entries'"
            ).fetchone()
            raw = (
                db.execute(
                    "SELECT body FROM managed_input_entries WHERE id=? AND principal=? AND kind=?",
                    (batch.media_ref, context.principal, "media"),
                ).fetchone()
                if table
                else None
            )
            if raw:
                raw = json.loads(raw["body"])
                location, raw_ref, clock = (
                    raw["location_ref"],
                    batch.media_ref,
                    raw["clock_authority"],
                )
                if raw["recorded_start_at"]:
                    record_start = (
                        datetime.fromisoformat(raw["recorded_start_at"]).timestamp()
                        + (batch.start_ms - raw["start_ms"]) / 1000
                    )
                    record_end = record_start + (batch.end_ms - batch.start_ms) / 1000
            ident = (
                "fact-"
                + digest([context.task_id, batch.batch_id, context.evidence_ref, part_id])[:40]
            )
            body = {
                **description.model_dump(mode="json"),
                "fact_id": ident,
                "task_id": context.task_id,
                "batch_id": batch.batch_id,
                "source_ref": batch.source_ref,
                "instruction_version": context.observation_version or context.version,
                "capture_generation": batch.generation,
                "evaluation_generation": context.generation,
                "segment_id": batch.segment_id,
                "start_ms": batch.start_ms,
                "end_ms": batch.end_ms,
                "evidence_ref": context.evidence_ref,
                "media_ref": batch.media_ref,
                "raw_media_ref": raw_ref,
                "raw_media_parts": list(batch.media_parts or (batch.media_ref,)),
                "clock_authority": clock or "unknown",
                "recorded_start": record_start,
                "recorded_end": record_end,
                "location_ref": location,
                "decision": decision,
                "source_simulated": batch.simulated,
                "inference_simulated": context.inference_simulated,
                "simulated": batch.simulated or context.inference_simulated,
                "candidate_only": True,
                "object_identity_verified": False,
                "absence_proven": False,
            }
            old = db.execute("SELECT body FROM observation_facts WHERE id=?", (ident,)).fetchone()
            if old:
                if old["body"] != canonical(body):
                    raise Conflict("evidence identity changed")
                return body
            search = " ".join(
                [description.text, *description.labels, *description.entity_refs]
            ).casefold()
            db.execute(
                "INSERT INTO observation_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    context.principal,
                    context.task_id,
                    batch.batch_id,
                    batch.source_ref,
                    location,
                    record_start,
                    record_end,
                    record_start if record_start is not None else self.store.clock(),
                    decision,
                    canonical(body),
                    search,
                ),
            )
        return body

    def search(self, principal, query: EvidenceSearch):
        query = EvidenceSearch.model_validate(query.model_dump())
        clauses, parameters = ["principal=?"], [principal]
        for name in ("source_ref", "location_ref", "decision"):
            value = getattr(query, name)
            if value is not None:
                clauses.append(name + "=?")
                parameters.append(value)
        if query.start is not None:
            clauses.append("recorded_start<? AND recorded_end>?")
            parameters += [query.end.timestamp(), query.start.timestamp()]
        if query.query:
            clauses.append("search_text LIKE ? ESCAPE '\\'")
            text = (
                query.query.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            parameters.append("%" + text + "%")
        if query.entity_ref:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(json_extract(body,'$.entity_refs')) WHERE value=?)"
            )
            parameters.append(query.entity_ref)
        with self.store.connect() as db:
            if query.after_id:
                cursor = db.execute(
                    "SELECT sort_time,id FROM observation_facts WHERE id=? AND "
                    + " AND ".join(clauses),
                    (query.after_id, *parameters),
                ).fetchone()
                if cursor is None:
                    raise Conflict("evidence cursor is unavailable for this user")
                clauses.append("(sort_time<? OR (sort_time=? AND id<?))")
                parameters += [cursor["sort_time"], cursor["sort_time"], cursor["id"]]
            rows = db.execute(
                "SELECT * FROM observation_facts WHERE "
                + " AND ".join(clauses)
                + " ORDER BY sort_time DESC,id DESC LIMIT ?",
                (*parameters, query.limit + 1),
            ).fetchall()
        items = [json.loads(row["body"]) for row in rows[: query.limit]]
        with self.store.connect() as db:
            managed = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='managed_input_entries'"
            ).fetchone()
            for item in items:
                task = db.execute(
                    "SELECT config FROM observation_tasks WHERE task_id=? AND principal=?",
                    (item["task_id"], principal),
                ).fetchone()
                item["project_id"] = json.loads(task["config"])["project_id"]
                item["task_kind"] = "observation"
                item["media_source_kind"] = "raw_input"
                item["media_status"] = "not_checked"
                item["media_revalidation_required"] = True
                raw = (
                    db.execute(
                        "SELECT path,body FROM managed_input_entries WHERE principal=? AND id=?",
                        (principal, item["media_ref"]),
                    ).fetchone()
                    if managed
                    else None
                )
                if raw:
                    try:
                        from .recorded_video import validate_recording

                        body = json.loads(raw["body"])
                        try:
                            validate_recording(body, Path(raw["path"]))
                            item["media_status"] = (
                                "recording_in_progress"
                                if body.get("recording_state") == "growing"
                                else "available_metadata_only"
                            )
                        except Conflict:
                            item["media_status"] = "changed"
                    except OSError:
                        item["media_status"] = "unavailable"
        return {
            "evidence": items,
            "next_after_id": items[-1]["fact_id"] if len(rows) > query.limit else None,
            "absence_proven": False,
            "candidate_only": True,
            "simulated": any(i["simulated"] for i in items),
            "note": "No match means no matching indexed evidence; it does not prove the object is absent.",
        }

    def frame(self, principal, task_id, marker_id, index=0):
        if type(index) is not int or not 0 <= index < 100:
            raise KeyError(marker_id)
        with self.store.connect() as db:
            marker = db.execute(
                """SELECT d.context,d.batch_id FROM observation_decisions d
                JOIN observation_tasks t ON t.task_id=d.task_id WHERE d.id=? AND d.task_id=? AND t.principal=?""",
                (marker_id, task_id, principal),
            ).fetchone()
            if marker is None:
                raise KeyError(marker_id)
            ref = json.loads(marker["context"])["evidence_ref"]
            fused = db.execute(
                "SELECT payload FROM audit WHERE task_id=? AND kind='perception.fusion' AND json_extract(payload,'$.request_id')=?",
                (task_id, ref),
            ).fetchone()
            if fused:
                ref = json.loads(fused["payload"])["visual_result"]["evidence_ref"]
            row = db.execute(
                "SELECT payload FROM audit WHERE task_id=? AND kind='model.request' AND json_extract(payload,'$.request_id')=?",
                (task_id, ref),
            ).fetchone()
        if row is None:
            raise KeyError(marker_id)
        request = self.store.audit_payload(row["payload"])
        body = request["body"]
        expected = None
        if body.get("schema_version") == "harness.native-visual/1":
            if request.get("observation_batch_id") != marker["batch_id"]:
                raise Conflict("recorded native frame binding differs")
            frames = body["frames"]
            if index >= len(frames):
                raise KeyError(marker_id)
            if "jpeg_base64" not in frames[index]:
                return self._reproduce_frame(principal, task_id, marker["batch_id"], frames[index], index)
            encoded = frames[index]["jpeg_base64"]
            expected = frames[index]["sha256"]
        elif body.get("schema") == "harness.semantic-media/1":
            if request.get("observation_batch_id") != marker["batch_id"]:
                raise Conflict("recorded frame binding differs")
            frames = body["inputs"].get("images", [])
            if index >= len(frames):
                raise KeyError(marker_id)
            encoded = frames[index]["jpeg_base64"]
            expected = frames[index]["sha256"]
        else:
            content = body["messages"][-1]["content"]
            metadata = json.loads(content[0]["text"].split("\n", 1)[1])
            if metadata["batch_id"] != marker["batch_id"]:
                raise Conflict("recorded frame binding differs")
            images = [b["image_url"]["url"] for b in content if b.get("type") == "image_url"]
            if index >= len(images) or not images[index].startswith("data:image/jpeg;base64,"):
                raise KeyError(marker_id)
            encoded = images[index].split(",", 1)[1]
        if len(encoded) > 7 * 1024**2:
            raise Conflict("recorded image exceeds bound")
        raw = base64.b64decode(encoded, validate=True)
        if (
            not raw.startswith(b"\xff\xd8")
            or len(raw) > 5 * 1024**2
            or (expected and hashlib.sha256(raw).hexdigest() != expected)
        ):
            raise Conflict("invalid recorded input image")
        return raw

    def _reproduce_frame(self, principal, task_id, batch_id, frame, index):
        """Regenerate a viewed thumbnail, never retain historical image bytes."""
        from contextlib import ExitStack
        from dataclasses import fields
        from .observation_media import FileReplayWindow, SamplingPolicy, _fingerprint

        with self.store.connect() as db:
            rows = db.execute(
                "SELECT body FROM observation_payloads WHERE principal=? AND task_id=? "
                "AND json_extract(body,'$.batch.batch_id')=? ORDER BY created DESC",
                (principal, task_id, batch_id),
            ).fetchall()
        for row in rows:
            payload = json.loads(row[0])
            manifest = payload["manifest"]
            frames = manifest["frames"]
            if index >= len(frames) or frames[index]["sha256"] != frame["sha256"]:
                continue
            saved = frames[index]
            cached = Path(payload["directory"]) / saved["file"]
            if cached.is_file() and not cached.is_symlink():
                raw = cached.read_bytes()
                if hashlib.sha256(raw).hexdigest() == frame["sha256"]:
                    return raw
            with ExitStack() as leases:
                reader_options = {}
                if manifest.get("source_kind") == "managed_capture":
                    from .harness_input_store import InputMediaStore
                    from .recorded_video import validate_recording

                    media = InputMediaStore(self.store)
                    ref = saved.get("raw_media_ref", manifest["raw_media_ref"])
                    leases.enter_context(media.retention.lease(principal, ref))
                    raw = media.resolve(principal, ref, allow_growing=True)
                    source = raw["path"]
                    if raw.get("source_fingerprint_kind") == "recording_identity":
                        # Reuse the capture identity guard: append is expected,
                        # replacement/truncation is not. Still verify image hash.
                        reader_options = {
                            "source_identity": raw["source_fingerprint"],
                            "validate_source": lambda: validate_recording(raw, source),
                        }
                else:
                    source = Path(manifest["source_file"])
                    if tuple(manifest["source_fingerprint"]) != _fingerprint(source):
                        raise Conflict("original changed; cannot reproduce observation image")
                options = {k: v for k, v in manifest["policy"].items()
                           if k in {f.name for f in fields(SamplingPolicy)}}
                options.update(include_audio=False, include_image=True, validate_full_window=False)
                start = saved.get("file_sample_at_ms", saved["sample_at_ms"])
                reader = FileReplayWindow(
                    source, manifest["source_ref"], Path(payload["directory"]).parent,
                    SamplingPolicy(**options), memory_images=True, **reader_options,
                )
                prepared = reader.prepare(start, start + 1, batch_id="evidence-reread")
                data = prepared["frame_bytes"][prepared["manifest"]["frames"][0]["file"]]
                if hashlib.sha256(data).hexdigest() != frame["sha256"]:
                    raise Conflict("reproduced frame differs from original model input")
                return data
        raise Conflict("original image metadata unavailable; cannot reproduce evidence")

    def period(self, principal, query: PeriodReview):
        query = PeriodReview.model_validate(query.model_dump())
        facts = self.search(principal, EvidenceSearch(**query.model_dump(), decision=None))
        where, args = ["principal=?", "kind='media'"], [principal]
        if query.source_ref:
            where.append("source_ref=?")
            args.append(query.source_ref)
        if query.location_ref:
            where.append("json_extract(body,'$.location_ref')=?")
            args.append(query.location_ref)
        with self.store.connect() as db:
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='managed_input_entries'"
            ).fetchone()
            unknown = (
                db.execute(
                    "SELECT COUNT(*) FROM managed_input_entries WHERE "
                    + " AND ".join(where)
                    + " AND json_extract(body,'$.recorded_start_at') IS NULL",
                    args,
                ).fetchone()[0]
                if table
                else 0
            )
            rows = (
                db.execute(
                    "SELECT id,source_ref,body FROM managed_input_entries WHERE "
                    + " AND ".join(where)
                    + " AND (julianday(json_extract(body,'$.recorded_start_at'))-2440587.5)*86400<? AND "
                    + "(julianday(json_extract(body,'$.recorded_start_at'))-2440587.5)*86400+"
                    + "(json_extract(body,'$.end_ms')-json_extract(body,'$.start_ms'))/1000.0>? LIMIT 100001",
                    (*args, query.end.timestamp(), query.start.timestamp()),
                ).fetchall()
                if table
                else []
            )
            if len(rows) > 100000:
                raise Conflict(
                    "review scope exceeds coverage budget; select a source or smaller time range"
                )
            seen = {}
            has_media = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='observation_media'"
            ).fetchone()
            if has_media:
                ids = [row["id"] for row in rows]
                for offset in range(0, len(ids), 500):
                    page = ids[offset : offset + 500]
                    for row in db.execute(
                        "SELECT m.body FROM observation_media m JOIN observation_tasks t ON t.task_id=m.task_id "
                        + "WHERE t.principal=? AND m.seen IS NOT NULL AND json_extract(m.body,'$.media_ref') IN ("
                        + ",".join("?" for _ in page)
                        + ")",
                        (principal, *page),
                    ):
                        batch = json.loads(row["body"])
                        seen.setdefault(batch["media_ref"], []).append(
                            (batch["start_ms"], batch["end_ms"])
                        )
        with self.store.connect() as db:
            has_retention = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='raw_recordings'"
            ).fetchone()
            expired = (
                {
                    r["media_ref"]: dict(r)
                    for r in db.execute(
                        "SELECT media_ref,state,reason,expired FROM raw_recordings WHERE principal=? AND state!='available'",
                        (principal,),
                    )
                }
                if has_retention
                else {}
            )
        expired_ranges = []
        coverage = {}
        for row in rows:
            body = json.loads(row["body"])
            start = datetime.fromisoformat(body["recorded_start_at"]).timestamp()
            end = start + (body["end_ms"] - body["start_ms"]) / 1000
            interval = [max(start, query.start.timestamp()), min(end, query.end.timestamp())]
            if interval[1] <= interval[0]:
                continue
            item = coverage.setdefault(
                row["source_ref"],
                {"recorded": [], "seen": [], "location_ref": body["location_ref"]},
            )
            item["recorded"].append(interval)
            if row["id"] in expired:
                expired_ranges.append(
                    {
                        "media_ref": row["id"],
                        "source_ref": row["source_ref"],
                        "recorded_start": start,
                        "recorded_end": end,
                        **expired[row["id"]],
                    }
                )
                item.setdefault("expired", []).append(interval)
            else:
                item.setdefault("available", []).append(interval)
            for left, right in seen.get(row["id"], []):
                observed = [
                    max(interval[0], start + (left - body["start_ms"]) / 1000),
                    min(interval[1], start + (right - body["start_ms"]) / 1000),
                ]
                if observed[1] > observed[0]:
                    item["seen"].append(observed)
        sources = []
        for ref, item in coverage.items():
            recorded, observed = merged_ms(item["recorded"]), merged_ms(item["seen"])
            sources.append(
                {
                    "source_ref": ref,
                    "location_ref": item["location_ref"],
                    "recorded_ms": recorded,
                    "available_ms": merged_ms(item.get("available", [])),
                    "expired_ms": merged_ms(item.get("expired", [])),
                    "observed_ms": observed,
                    "recorded_not_analyzed_ms": max(0, recorded - observed),
                    "not_recorded_in_period_ms": round(
                        (query.end - query.start).total_seconds() * 1000
                    )
                    - recorded,
                }
            )
        groups = {}
        for fact in facts["evidence"]:
            key = (fact["source_ref"], fact["segment_id"], fact["batch_id"])
            group = groups.setdefault(
                key,
                {
                    "source_ref": fact["source_ref"],
                    "batch_id": fact["batch_id"],
                    "recorded_start": fact["recorded_start"],
                    "recorded_end": fact["recorded_end"],
                    "descriptions": [],
                    "evidence_refs": [],
                    "simulated": False,
                },
            )
            group["descriptions"].append(fact["text"])
            group["evidence_refs"].append(fact["evidence_ref"])
            group["simulated"] = group["simulated"] or fact["simulated"]
        return {
            **facts,
            "start": query.start.isoformat(),
            "end": query.end.isoformat(),
            "coverage": sources,
            "event_groups": list(groups.values()),
            "groups_partial_until_all_pages": True,
            "recording_clock_unknown_entries": unknown,
            "expired_ranges": expired_ranges,
            "continuous_coverage_verified": False,
            "endurance_24h_verified": False,
            "media_exported": False,
            "note": "Coverage is per source and location. Unrecorded time is not an empty scene; evidence pages do not prove a full-day recording.",
        }
