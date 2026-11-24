"""Durable keep/discard/uncertain intervals and event grouping.

Model observations, decisions, protected ranges, and saved files are distinct.
This manager never deletes media, invents a decision for an unseen interval, or
reports an event saved. The archive owns actual verified file publication.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract, canonical, digest
from .observation_contracts import Counter, ObservationRun, Reference, Version
from .observation_store import ObservationStore
from .store import Conflict


@dataclass(frozen=True)
class RetentionPolicy:
    pre_roll_ms: int = 5000
    post_roll_ms: int = 5000
    discard_hold_ms: int = 300000

    def __post_init__(self):
        if any(type(value) is not int or value < 0 for value in asdict(self).values()):
            raise ValueError("retention intervals must be nonnegative integer milliseconds")


class DecisionContext(ObservationRun):
    """Trusted Harness context, not model-provided tool arguments.

    The runner establishes that this model response actually consumed the
    referenced input before invoking a tool. Live tool authority additionally
    requires the Gateway's current-attempt callback.
    """

    batch_id: Reference
    session_ref: Reference
    evidence_ref: Reference
    mode: Literal["live", "backfill"] = "live"
    inference_simulated: bool = Field(strict=True)
    observation_version: Version | None = None


class MediaDecision(Contract):
    request_id: Reference
    start_ms: Counter
    end_ms: Counter
    decision: Literal["keep", "discard", "uncertain"]
    reason: str = Field(min_length=1, max_length=1000)
    expected_revision: Counter = 0
    resolve_id: Reference | None = None

    @model_validator(mode="after")
    def interval(self):
        if self.end_ms <= self.start_ms or not self.reason.strip():
            raise ValueError("empty decision interval or reason")
        return self


def _covered(intervals, start, end):
    if end <= start:
        return True
    cursor = start
    for left, right in sorted(intervals):
        if right <= cursor:
            continue
        if left > cursor:
            return False
        cursor = max(cursor, right)
        if cursor >= end:
            return True
    return cursor >= end


class ObservationDecisions:
    def __init__(self, ledger: ObservationStore, policies: dict[str, RetentionPolicy]):
        self.ledger, self.store = ledger, ledger.store
        self.policies = dict(policies)
        from .observation_candidates import CandidateReviews

        self.reviews = CandidateReviews(self.store)
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS observation_retention_policies (
                    task_id TEXT NOT NULL,version INTEGER NOT NULL,spec TEXT NOT NULL,
                    PRIMARY KEY(task_id,version)
                );
                CREATE TABLE IF NOT EXISTS observation_decision_requests (
                    principal TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL,decision_id TEXT NOT NULL,
                    PRIMARY KEY(principal,id)
                );
                CREATE TABLE IF NOT EXISTS observation_decisions (
                    id TEXT PRIMARY KEY,task_id TEXT NOT NULL,version INTEGER NOT NULL,batch_id TEXT NOT NULL,
                    source_ref TEXT NOT NULL,segment_id TEXT NOT NULL,start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,
                    decision TEXT NOT NULL,revision INTEGER NOT NULL,context TEXT NOT NULL,reason TEXT NOT NULL,
                    event_id TEXT,updated REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS observation_decision_ranges
                    ON observation_decisions(task_id,version,source_ref,segment_id,start_ms,end_ms);
                CREATE TABLE IF NOT EXISTS observation_retention_events (
                    id TEXT PRIMARY KEY,task_id TEXT NOT NULL,version INTEGER NOT NULL,source_ref TEXT NOT NULL,
                    segment_id TEXT NOT NULL,core_start_ms INTEGER NOT NULL,core_end_ms INTEGER NOT NULL,
                    start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,revision INTEGER NOT NULL,
                    status TEXT NOT NULL,merged_into TEXT,export_id TEXT,created REAL NOT NULL
                );
            """)
            if "export_plan" not in {
                row["name"] for row in db.execute("PRAGMA table_info(observation_retention_events)")
            }:
                db.execute("ALTER TABLE observation_retention_events ADD COLUMN export_plan TEXT")

    def _event(self, db, event_id):
        seen = set()
        while event_id:
            if event_id in seen:
                raise Conflict("event alias cycle")
            seen.add(event_id)
            row = db.execute(
                "SELECT * FROM observation_retention_events WHERE id=?", (event_id,)
            ).fetchone()
            if row is None:
                raise KeyError(event_id)
            if row["merged_into"]:
                event_id = row["merged_into"]
            else:
                return dict(row)

    def freeze_policy(self, run):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.ledger._task(db, run.principal, run.task_id)
            self.ledger._guard(task, run)
            policy = self.policies.get(json.loads(task["config"])["save_policy_ref"])
            if policy is None:
                raise Conflict("retention policy is not registered")
            value = canonical(asdict(policy))
            old = db.execute(
                "SELECT spec FROM observation_retention_policies WHERE task_id=? AND version=?",
                (run.task_id, run.version),
            ).fetchone()
            if old and old["spec"] != value:
                raise Conflict("running retention policy changed")
            db.execute(
                "INSERT OR IGNORE INTO observation_retention_policies VALUES (?,?,?)",
                (run.task_id, run.version, value),
            )
            return asdict(policy)

    def _result(self, db, ident, *, duplicate=False):
        row = db.execute("SELECT * FROM observation_decisions WHERE id=?", (ident,)).fetchone()
        result = {
            "duplicate": duplicate,
            "decision": {**dict(row), "context": json.loads(row["context"])},
            "event": self._event(db, row["event_id"]) if row["event_id"] else None,
            "file_saved": False,
        }  # Only the archive may supply a saved-file receipt.
        instruction = self.ledger.instruction_config(
            json.loads(row["context"])["principal"], row["task_id"], row["version"]
        )
        if instruction.output_mode == "markers":
            batch = self.ledger._batch(db, row["task_id"], row["batch_id"])
            result.update(mark_applied=True, output_mode="markers", marker=self._marker(row, batch))
        return result

    @staticmethod
    def _marker(row, batch):
        context = json.loads(row["context"])
        captured = json.loads(batch["body"])
        return {
            "id": row["id"],
            "task_id": row["task_id"],
            "version": row["version"],
            "sequence": batch["sequence"],
            "batch_id": row["batch_id"],
            "source_ref": row["source_ref"],
            "segment_id": row["segment_id"],
            "media_ref": captured["media_ref"],
            "media_parts": captured.get("media_parts") or [captured["media_ref"]],
            "start_ms": row["start_ms"],
            "end_ms": row["end_ms"],
            "decision": row["decision"],
            "reason": row["reason"],
            "revision": row["revision"],
            "evidence_ref": context["evidence_ref"],
            "source_fingerprint": captured["source_fingerprint"],
            "source_simulated": captured["simulated"],
            "inference_simulated": context["inference_simulated"],
            "event_id": row["event_id"],
        }

    def markers(self, principal, task_id, *, after_sequence=-1, limit=100, decision=None):
        if (
            type(after_sequence) is not int
            or after_sequence < -1
            or type(limit) is not int
            or not 1 <= limit <= 500
        ):
            raise ValueError("invalid marker page")
        with self.store.connect() as db:
            task = self.ledger._task(db, principal, task_id)
            config = json.loads(task["config"])
            if decision not in {None, "keep", "discard", "uncertain"}:
                raise ValueError("invalid marker decision filter")
            rows = db.execute(
                """SELECT d.* FROM observation_decisions d JOIN observation_media m
                ON m.task_id=d.task_id AND m.entry_id=d.batch_id WHERE d.task_id=? AND d.version=?
                AND m.sequence>? AND (? IS NULL OR d.decision=?) ORDER BY m.sequence,d.start_ms LIMIT ?""",
                (task_id, task["version"], after_sequence, decision, decision, limit + 1),
            ).fetchall()
            page = [
                self._marker(row, self.ledger._batch(db, task_id, row["batch_id"]))
                for row in rows[:limit]
            ]
            for marker in page:
                review = db.execute(
                    "SELECT disposition,reason,used_asset_id FROM observation_candidate_reviews WHERE marker_id=? AND revision=?",
                    (marker["id"], marker["revision"]),
                ).fetchone()
                marker["review_disposition"] = review["disposition"] if review else "pending"
                marker["review"] = dict(review) if review else None
            stats = {
                row[0]: {"count": row[1], "duration_ms": row[2]}
                for row in db.execute(
                    """SELECT decision,COUNT(*),SUM(end_ms-start_ms)
                FROM observation_decisions WHERE task_id=? AND version=? GROUP BY decision""",
                    (task_id, task["version"]),
                )
            }
            for name in ("keep", "discard", "uncertain"):
                stats.setdefault(name, {"count": 0, "duration_ms": 0})
            latest = db.execute(
                """SELECT m.sequence FROM observation_media m WHERE m.task_id=?
                AND EXISTS (SELECT 1 FROM observation_decisions d WHERE d.task_id=m.task_id
                    AND d.batch_id=m.entry_id AND d.version=?)
                ORDER BY m.sequence DESC LIMIT 1""", (task_id, task["version"]),
            ).fetchone()
        from .observation_candidates import candidate_groups

        return {
            "task_id": task_id,
            "version": task["version"],
            "project_id": config["project_id"],
            "task_kind": "observation",
            "output_mode": config.get("output_mode", "retained_media"),
            "markers": page,
            "candidates": candidate_groups(
                page, has_more=len(rows) > limit, after_sequence=after_sequence
            ),
            "candidate_scope": "current_marker_page_only",
            "recording_state": {"status": task["status"], "generation": task["generation"],
                                "reason": task["reason"], "checked_at": self.store.clock()},
            "candidate_review_identity": {"task_id": task_id, "task_version": task["version"]},
            "has_more": len(rows) > limit,
            "latest_available_sequence": latest[0] if latest else None,
            "page_is_latest_progress": not (len(rows) > limit) and decision is None,
            "page_meaning": "这是已存在标记的一页，不是本轮新采集或已看过的图片。仍有下一页时不能据此判断端侧进度停滞。",
            "decision_filter": decision,
            "summary": stats,
            "next_after_sequence": page[-1]["sequence"] if len(rows) > limit else None,
            "merged": False,
            "files_exported_by_this_query": False,
        }

    def record(self, context: DecisionContext, decision: MediaDecision, *, is_current):
        context = DecisionContext.model_validate(context.model_dump())
        decision = MediaDecision.model_validate(decision.model_dump())
        target_version = context.observation_version or context.version
        if target_version > context.version or (
            target_version != context.version and context.mode != "backfill"
        ):
            raise Conflict("historical instructions require an isolated original-version context")
        wire = {
            "context": context.model_dump(mode="json"),
            "arguments": decision.model_dump(mode="json"),
        }
        ident = (
            decision.resolve_id
            or "decision-"
            + digest(
                [
                    context.task_id,
                    target_version,
                    context.batch_id,
                    decision.start_ms,
                    decision.end_ms,
                ]
            )[:32]
        )
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self.ledger._task(db, context.principal, context.task_id)
            prior = db.execute(
                "SELECT * FROM observation_decision_requests WHERE principal=? AND id=?",
                (context.principal, decision.request_id),
            ).fetchone()
            if prior:
                if prior["body"] != canonical(wire):
                    raise Conflict("decision request identity collision")
                return self._result(db, prior["decision_id"], duplicate=True)
            self.ledger._guard(task, context, running=True)
            if not is_current():
                raise Conflict("observation attempt is no longer current")
            config = json.loads(task["config"])
            output_mode = self.ledger.instruction_config(
                context.principal, context.task_id, target_version
            ).output_mode
            batch = self.ledger._batch(db, context.task_id, context.batch_id)
            captured = json.loads(batch["body"])
            if context.mode == "live":
                if (
                    context.session_ref != config["session_ref"]
                    or captured["version"] != context.version
                    or captured["generation"] != context.generation
                    or batch["gap_reason"] is not None
                ):
                    raise Conflict("live decision does not match current observation binding")
            elif context.session_ref == config["session_ref"]:
                raise Conflict("historical decision requires an isolated context")
            if (
                not captured["start_ms"]
                <= decision.start_ms
                < decision.end_ms
                <= captured["end_ms"]
            ):
                raise Conflict(
                    "decision must refer to the input actually observed by this invocation"
                )
            if output_mode == "markers" and (decision.start_ms, decision.end_ms) != (
                captured["start_ms"],
                captured["end_ms"],
            ):
                raise Conflict("marker scorer must mark its entire observed batch")
            if decision.decision == "discard" and captured["missing_modalities"]:
                raise Conflict("missing media cannot be treated as empty footage")
            old = db.execute("SELECT * FROM observation_decisions WHERE id=?", (ident,)).fetchone()
            effective_version = target_version
            if decision.resolve_id:
                if (
                    old is None
                    or old["task_id"] != context.task_id
                    or old["batch_id"] != context.batch_id
                    or (old["start_ms"], old["end_ms"]) != (decision.start_ms, decision.end_ms)
                    or old["decision"] != "uncertain"
                    or context.mode != "backfill"
                    or context.observation_version != old["version"]
                ):
                    raise Conflict(
                        "uncertainty resolution requires its original interval/version and isolated context"
                    )
                effective_version = old["version"]
            else:
                if old and old["decision"] == "uncertain" and target_version != context.version:
                    raise Conflict("older uncertainty requires explicit resolution")
                unresolved = db.execute(
                    """SELECT id FROM observation_decisions WHERE task_id=? AND batch_id=?
                    AND start_ms<? AND end_ms>? AND decision='uncertain' AND version!=? LIMIT 1""",
                    (
                        context.task_id,
                        context.batch_id,
                        decision.end_ms,
                        decision.start_ms,
                        target_version,
                    ),
                ).fetchone()
                if unresolved:
                    raise Conflict("older uncertainty requires explicit resolution")
            previous_policy = db.execute(
                "SELECT spec FROM observation_retention_policies WHERE task_id=? AND version=?",
                (context.task_id, effective_version),
            ).fetchone()
            historical = effective_version != context.version or decision.resolve_id is not None
            if historical and previous_policy is None:
                raise Conflict("original decision policy is unavailable")
            policy = (
                RetentionPolicy(**json.loads(previous_policy["spec"]))
                if historical and previous_policy
                else self.policies.get(config["save_policy_ref"])
            )
            if policy is None:
                raise Conflict("retention policy is not registered")
            policy_wire = canonical(asdict(policy))
            saved_policy = db.execute(
                "SELECT spec FROM observation_retention_policies WHERE task_id=? AND version=?",
                (context.task_id, effective_version),
            ).fetchone()
            if saved_policy and saved_policy["spec"] != policy_wire:
                raise Conflict("running retention policy changed")
            db.execute(
                "INSERT OR IGNORE INTO observation_retention_policies VALUES (?,?,?)",
                (context.task_id, effective_version, policy_wire),
            )
            if old:
                if decision.expected_revision != old["revision"]:
                    raise Conflict("decision revision mismatch")
                if old["decision"] != "uncertain" and old["decision"] != decision.decision:
                    raise Conflict("a definitive decision cannot be silently rewritten")
            elif decision.expected_revision != 0:
                raise Conflict("new decision requires revision zero")
            overlap = db.execute(
                """SELECT id FROM observation_decisions WHERE task_id=? AND version=?
                AND source_ref=? AND segment_id=? AND id!=? AND start_ms<? AND end_ms>? LIMIT 1""",
                (
                    context.task_id,
                    effective_version,
                    captured["source_ref"],
                    captured["segment_id"],
                    ident,
                    decision.end_ms,
                    decision.start_ms,
                ),
            ).fetchone()
            if overlap:
                raise Conflict(
                    "overlapping decisions must resolve the original interval explicitly"
                )
            revision = (
                old["revision"] + 1
                if old and old["decision"] != decision.decision
                else old["revision"]
                if old
                else 1
            )
            event_id = old["event_id"] if old else None
            if decision.decision == "keep" and output_mode != "markers":
                event_id = self._merge(
                    db,
                    context.model_copy(update={"version": effective_version}),
                    captured,
                    decision,
                    policy,
                )
            db.execute(
                """INSERT INTO observation_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET decision=excluded.decision,revision=excluded.revision,
                context=excluded.context,reason=excluded.reason,event_id=excluded.event_id,updated=excluded.updated""",
                (
                    ident,
                    context.task_id,
                    effective_version,
                    context.batch_id,
                    captured["source_ref"],
                    captured["segment_id"],
                    decision.start_ms,
                    decision.end_ms,
                    decision.decision,
                    revision,
                    canonical(context),
                    decision.reason,
                    event_id,
                    self.store.clock(),
                ),
            )
            db.execute(
                "INSERT INTO observation_decision_requests VALUES (?,?,?,?)",
                (context.principal, decision.request_id, canonical(wire), ident),
            )
            self.store._audit(
                db,
                context.task_id,
                "observation.decision",
                {
                    "decision_id": ident,
                    **wire,
                    "event_id": event_id,
                    "inference_simulated": context.inference_simulated,
                },
            )
            return self._result(db, ident)

    def _merge(self, db, context, captured, decision, policy):
        start = max(captured.get("segment_start_ms", 0), decision.start_ms - policy.pre_roll_ms)
        end = decision.end_ms + policy.post_roll_ms
        core_start, core_end = decision.start_ms, decision.end_ms
        found = {}
        while True:
            rows = db.execute(
                """SELECT * FROM observation_retention_events WHERE task_id=? AND version=?
                AND source_ref=? AND segment_id=? AND merged_into IS NULL AND start_ms<=? AND end_ms>=?""",
                (
                    context.task_id,
                    context.version,
                    captured["source_ref"],
                    captured["segment_id"],
                    end,
                    start,
                ),
            ).fetchall()
            new = [row for row in rows if row["id"] not in found]
            if not new:
                break
            for row in new:
                found[row["id"]] = row
                start = min(start, row["start_ms"])
                end = max(end, row["end_ms"])
                core_start = min(core_start, row["core_start_ms"])
                core_end = max(core_end, row["core_end_ms"])
        if found:
            rows = sorted(found.values(), key=lambda row: (row["core_start_ms"], row["id"]))
            canonical_event = rows[0]
            changed = len(rows) > 1 or (core_start, core_end) != (
                canonical_event["core_start_ms"],
                canonical_event["core_end_ms"],
            )
            if changed and any(row["status"] != "open" for row in rows):
                raise Conflict("exporting or saved event cannot be extended by a late decision")
            ident = canonical_event["id"]
            if changed:
                db.execute(
                    """UPDATE observation_retention_events SET core_start_ms=?,core_end_ms=?,start_ms=?,end_ms=?,
                    revision=revision+1 WHERE id=?""",
                    (core_start, core_end, start, end, ident),
                )
            for row in rows[1:]:
                db.execute(
                    "UPDATE observation_retention_events SET status='merged',merged_into=? WHERE id=?",
                    (ident, row["id"]),
                )
                db.execute(
                    "UPDATE observation_decisions SET event_id=? WHERE event_id=?",
                    (ident, row["id"]),
                )
            return ident
        ident = (
            "event-"
            + digest(
                [
                    context.task_id,
                    context.version,
                    captured["source_ref"],
                    captured["segment_id"],
                    core_start,
                ]
            )[:32]
        )
        db.execute(
            """INSERT INTO observation_retention_events
            (id,task_id,version,source_ref,segment_id,core_start_ms,core_end_ms,start_ms,end_ms,revision,status,merged_into,export_id,created)
            VALUES (?,?,?,?,?,?,?,?,?,1,'open',NULL,NULL,?)""",
            (
                ident,
                context.task_id,
                context.version,
                captured["source_ref"],
                captured["segment_id"],
                core_start,
                core_end,
                start,
                end,
                self.store.clock(),
            ),
        )
        return ident

    def event_plan(self, principal, event_id):
        with self.store.connect() as db:
            return self._plan(db, principal, event_id)

    def _plan(self, db, principal, event_id):
        event = self._event(db, event_id)
        task = self.ledger._task(db, principal, event["task_id"])
        policy = RetentionPolicy(
            **json.loads(
                db.execute(
                    "SELECT spec FROM observation_retention_policies WHERE task_id=? AND version=?",
                    (event["task_id"], event["version"]),
                ).fetchone()[0]
            )
        )
        captures = [
            dict(row)
            for row in db.execute(
                """SELECT * FROM observation_media WHERE task_id=?
            AND source_ref=? AND segment_id=? AND kind='batch' ORDER BY start_ms""",
                (event["task_id"], event["source_ref"], event["segment_id"]),
            )
        ]
        available = max((row["end_ms"] for row in captures), default=0)
        stopped = task["status"] == "stopped"
        end = min(event["end_ms"], available) if stopped else event["end_ms"]
        source_complete = _covered(
            [(r["start_ms"], r["end_ms"]) for r in captures], event["start_ms"], end
        )
        if any(
            json.loads(row["body"])["missing_modalities"]
            for row in captures
            if row["start_ms"] < end and row["end_ms"] > event["start_ms"]
        ):
            source_complete = False
        horizon = event["end_ms"] + policy.pre_roll_ms + 1
        quiet = [
            (row["start_ms"], row["end_ms"])
            for row in db.execute(
                """SELECT start_ms,end_ms FROM observation_decisions
            WHERE task_id=? AND version=? AND source_ref=? AND segment_id=? AND decision='discard' """,
                (event["task_id"], event["version"], event["source_ref"], event["segment_id"]),
            )
        ]
        segment_start = min((row["start_ms"] for row in captures), default=event["start_ms"])
        left_horizon = max(segment_start, event["start_ms"] - policy.post_roll_ms - 1)
        earlier_resolved = _covered(quiet, left_horizon, event["core_start_ms"])
        ended = earlier_resolved and (stopped or _covered(quiet, event["core_end_ms"], horizon))
        return {
            "event": event,
            "ready": event["status"] == "open" and source_complete and ended,
            "start_ms": event["start_ms"],
            "end_ms": end,
            "source_complete": source_complete,
            "tail_complete": end == event["end_ms"],
            "end_confirmed": ended,
            "earlier_context_resolved": earlier_resolved,
            "inference_simulated": any(
                json.loads(row[0])["inference_simulated"]
                for row in db.execute(
                    "SELECT context FROM observation_decisions WHERE event_id=?", (event["id"],)
                )
            ),
        }

    def reserve_event(self, principal, event_id, archive, *, max_output_bytes):
        """Freeze an ended event before reserving its asynchronous archive job.

        The plan survives crashes between these transactions. A retry uses the
        same selection/run/request identity, including after the task stopped.
        """
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise ValueError("invalid export reservation size")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            plan = self._plan(db, principal, event_id)
            event = plan["event"]
            task = self.ledger._task(db, principal, event["task_id"])
            if event["export_plan"]:
                frozen = json.loads(event["export_plan"])
            else:
                if not plan["ready"]:
                    raise Conflict(
                        "event is still open, unresolved, or waiting for captured context"
                    )
                frozen = {
                    "principal": principal,
                    "task_id": event["task_id"],
                    "version": event["version"],
                    "generation": task["generation"],
                    "event_id": event["id"],
                    "source_ref": event["source_ref"],
                    "segment_id": event["segment_id"],
                    "start_ms": plan["start_ms"],
                    "end_ms": plan["end_ms"],
                    "evidence_ref": "retention-plan-"
                    + digest([event["id"], event["revision"]])[:24],
                    "inference_simulated": plan["inference_simulated"],
                    "max_output_bytes": max_output_bytes,
                }
                db.execute(
                    "UPDATE observation_retention_events SET status='exporting',export_plan=? WHERE id=?",
                    (canonical(frozen), event["id"]),
                )
        run = ObservationRun(
            **{key: frozen[key] for key in ("principal", "task_id", "version", "generation")}
        )
        args = {key: value for key, value in frozen.items() if key not in run.model_dump()}
        job = archive.reserve(
            run,
            **args,
            request_id="export-event-" + digest(frozen)[:24],
            _protected_event=event["id"],
        )
        with self.store.connect() as db:
            db.execute(
                "UPDATE observation_retention_events SET export_id=? WHERE id=? AND export_plan=?",
                (job["id"], event["id"], canonical(frozen)),
            )
        return job

    def export_result(self, principal, event_id, archive):
        with self.store.connect() as db:
            event = self._event(db, event_id)
            self.ledger._task(db, principal, event["task_id"])
        if not event["export_id"]:
            return {"event": event, "file_saved": False}
        job = archive.get(principal, event["export_id"])
        if job["status"] == "saved":
            archive.media_path(principal, job["id"])
            with self.store.connect() as db:
                db.execute(
                    "UPDATE observation_retention_events SET status='saved' WHERE id=? AND export_id=?",
                    (event["id"], job["id"]),
                )
            event["status"] = "saved"
        return {"event": event, "file_saved": job["status"] == "saved", "export": job}

    def protected_ranges(self, principal, task_id, source_ref, segment_id, *, window=None):
        """Unseen/uncertain/unclassified intervals stay protected; no deletion here."""
        with self.store.connect() as db:
            self.ledger._task(db, principal, task_id)
            protected = []
            policies = {
                row["version"]: RetentionPolicy(**json.loads(row["spec"]))
                for row in db.execute(
                    "SELECT version,spec FROM observation_retention_policies WHERE task_id=?",
                    (task_id,),
                )
            }
            start, end = window if window is not None else (None, None)
            for row in db.execute(
                """SELECT start_ms,end_ms,id FROM observation_retention_events
                WHERE task_id=? AND source_ref=? AND segment_id=? AND merged_into IS NULL
                AND (? IS NULL OR (start_ms<? AND end_ms>?))""",
                (task_id, source_ref, segment_id, end, end, start),
            ):
                protected.append(
                    {
                        "start_ms": row["start_ms"],
                        "end_ms": row["end_ms"],
                        "reason": "event",
                        "ref": row["id"],
                    }
                )
            for row in db.execute(
                """SELECT * FROM observation_media WHERE task_id=? AND source_ref=?
                AND segment_id=? AND kind='batch' AND (? IS NULL OR (start_ms<? AND end_ms>?))""",
                (task_id, source_ref, segment_id, end, end, start),
            ):
                marks = list(
                    db.execute(
                        "SELECT * FROM observation_decisions WHERE task_id=? AND batch_id=?",
                        (task_id, row["entry_id"]),
                    )
                )
                complete = _covered(
                    [(mark["start_ms"], mark["end_ms"]) for mark in marks],
                    row["start_ms"],
                    row["end_ms"],
                )
                captured = json.loads(row["body"])
                versions = {captured["version"], *(mark["version"] for mark in marks)}
                hold_ms = max(
                    (
                        policies[version].discard_hold_ms
                        for version in versions
                        if version in policies
                    ),
                    default=300000,
                )
                held = self.store.clock() < captured["captured_at"] + hold_ms / 1000
                if (
                    row["seen"] is None
                    or not complete
                    or any(
                        mark["decision"] == "uncertain"
                        and not db.execute(
                            "SELECT 1 FROM observation_candidate_reviews WHERE marker_id=? AND revision=? AND disposition='release'",
                            (mark["id"], mark["revision"]),
                        ).fetchone()
                        for mark in marks
                    )
                    or held
                ):
                    protected.append(
                        {
                            "start_ms": row["start_ms"],
                            "end_ms": row["end_ms"],
                            "reason": "discard_hold"
                            if held
                            and complete
                            and row["seen"] is not None
                            and all(mark["decision"] == "discard" for mark in marks)
                            else "unseen_or_unresolved",
                            "ref": row["entry_id"],
                        }
                    )
            return protected
