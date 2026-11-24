"""Frozen human-label validation and interval arithmetic for phase0.5 section3.3.

Human identity/independence remains an external review obligation. Neither a
label declaration nor passing these numerical checks grants model admission.
"""

import math
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract, digest
from .observation_contracts import Counter, Reference


class QualityInterval(Contract):
    id: Reference
    partition_id: Reference
    start_ms: Counter
    end_ms: Counter
    expected_decision: Literal["keep", "discard", "uncertain"]
    event_type: str = Field(min_length=1, max_length=100)
    critical: bool = Field(strict=True)
    audio_only: bool = Field(strict=True)
    normal_input: bool = Field(strict=True)

    @model_validator(mode="after")
    def valid(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("empty human interval")
        if (self.critical or self.audio_only) and self.expected_decision != "keep":
            raise ValueError("event flags require keep label")
        return self


class QualityLabels(Contract):
    label_schema: Literal["cix.observation-quality-labels/1"] = Field(alias="schema")
    status: Literal["frozen_human_review"]
    reviewer: str = Field(min_length=1, max_length=100)
    frozen_at: datetime
    baseline_id: Reference
    manifest_sha256: str = Field(pattern="^[0-9a-f]{64}$")
    intervals: list[QualityInterval] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def reviewed(self):
        if not self.reviewer.strip() or self.frozen_at.tzinfo is None:
            raise ValueError("reviewer and aware freeze time required")
        return self


def merged(ranges):
    output = []
    for start, end in sorted(ranges):
        if (
            type(start) not in {int, float}
            or type(end) not in {int, float}
            or not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end <= start
        ):
            raise ValueError("invalid observed interval")
        if output and start <= output[-1][1]:
            output[-1] = (output[-1][0], max(end, output[-1][1]))
        else:
            output.append((start, end))
    return output


def duration(ranges):
    return sum(end - start for start, end in merged(ranges))


def intersect(left, right):
    left, right = merged(left), merged(right)
    i = j = 0
    output = []
    while i < len(left) and j < len(right):
        start = max(left[i][0], right[j][0])
        end = min(left[i][1], right[j][1])
        if end > start:
            output.append((start, end))
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return output


def subtract(left, right):
    output = []
    right = merged(right)
    for start, end in merged(left):
        cursor = start
        for a, b in right:
            if b <= cursor or a >= end:
                continue
            if a > cursor:
                output.append((cursor, a))
            cursor = max(cursor, b)
            if cursor >= end:
                break
        if cursor < end:
            output.append((cursor, end))
    return output


def validate_labels(manifest, raw, manifest_sha256, baseline_id):
    labels = QualityLabels.model_validate(raw)
    if labels.baseline_id != baseline_id or labels.manifest_sha256 != manifest_sha256:
        raise ValueError("labels refer to another baseline or dataset manifest")
    if manifest.get("schema") != "cix.quality-review-candidates/1":
        raise ValueError("unsupported dataset manifest")
    parts = {part["id"]: part for part in manifest["partitions"]}
    if len(parts) != len(manifest["partitions"]):
        raise ValueError("duplicate dataset partition")
    ids = set()
    intervals = {}
    for item in labels.intervals:
        part = parts.get(item.partition_id)
        if not part or part["role"] != "validation":
            raise ValueError("development/guard data cannot be validation labels")
        if not part["start_ms"] <= item.start_ms < item.end_ms <= part["end_ms"]:
            raise ValueError("label outside validation partition")
        if item.id in ids:
            raise ValueError("duplicate human event identity")
        ids.add(item.id)
        source = manifest["sources"][part["source"]]
        if not part["end_ms"] <= source["facts"]["duration_ms"]:
            raise ValueError("partition exceeds original source")
        # Count labelled source time once even if someone aliases a source ID.
        key = source["sha256"]
        intervals.setdefault(key, []).append((item.start_ms, item.end_ms))
    for ranges in intervals.values():
        if sum(b - a for a, b in ranges) != duration(ranges):
            raise ValueError("overlapping human intervals or duplicate source aliases")
    return labels


def evaluate(manifest, labels, evidence):
    """Evidence comes from read-only SQLite/file verification, not model prose."""
    parts = {p["id"]: p for p in manifest["partitions"]}
    by_part = {}
    for label in labels.intervals:
        by_part.setdefault(label.partition_id, []).append(label)
    if set(evidence["partitions"]) != set(by_part):
        raise ValueError("evidence must match all labelled partitions")
    totals = {
        key: 0
        for key in (
            "target_ms",
            "retained_target_ms",
            "blank_ms",
            "cleaned_blank_ms",
            "retained_blank_ms",
            "normal_ms",
            "uncertain_normal_ms",
            "final_uncertain_normal_ms",
            "normal_without_first_decision_ms",
            "normal_without_decision_ms",
            "normal_unobserved_ms",
            "cleaned_target_ms",
            "cleaned_protected_ms",
            "unobserved_ms",
        )
    }
    events = []
    critical_incomplete = []
    missed = []
    has_real = True
    run_bindings = []
    for ident, items in by_part.items():
        observed = evidence["partitions"][ident]
        part = parts[ident]
        if observed["source_sha256"] != manifest["sources"][part["source"]]["sha256"]:
            raise ValueError("observed source differs from labels")
        has_real = has_real and observed["actual_inference"] is True
        wanted = [(i.start_ms, i.end_ms) for i in items]
        totals["unobserved_ms"] += duration(wanted) - duration(intersect(wanted, observed["seen"]))
        marks = observed["decisions"]
        kept = merged(observed["saved"])
        cleaned = merged(observed["cleaned"])
        decision_ranges = []
        for decision in ("keep", "discard", "uncertain"):
            decision_ranges.extend(marks[decision])
        if sum(duration(marks[d]) for d in marks) != duration(decision_ranges):
            raise ValueError("contradictory overlapping decisions")
        initial = observed["first_decisions"]
        initial_ranges = [r for values in initial.values() for r in values]
        if sum(duration(values) for values in initial.values()) != duration(initial_ranges):
            raise ValueError("contradictory initial decisions")
        for label in items:
            region = [(label.start_ms, label.end_ms)]
            length = label.end_ms - label.start_ms
            retained = duration(intersect(region, kept))
            deleted = duration(intersect(region, cleaned))
            if label.expected_decision == "keep":
                totals["target_ms"] += length
                totals["retained_target_ms"] += retained
                totals["cleaned_target_ms"] += deleted
                if retained == 0:
                    missed.append(label.id)
                # One microsecond only compensates the export verifier's PTS
                # arithmetic; partial critical events are explicitly failures.
                if label.critical and length - retained > 0.001:
                    critical_incomplete.append(label.id)
                segments = [(a, b) for a, b in kept if a < label.end_ms and b > label.start_ms]
                events.append(
                    {
                        "id": label.id,
                        "duration_ms": length,
                        "retained_ms": retained,
                        "missing_ms": length - retained,
                        "critical": label.critical,
                        "audio_only": label.audio_only,
                        "saved_segments": len(segments),
                        "start_offset_ms": segments[0][0] - label.start_ms if segments else None,
                        "end_offset_ms": segments[-1][1] - label.end_ms if segments else None,
                    }
                )
            elif label.expected_decision == "discard":
                actually_removed = subtract(intersect(cleaned, marks["discard"]), kept)
                totals["blank_ms"] += length
                totals["cleaned_blank_ms"] += duration(intersect(region, actually_removed))
                totals["retained_blank_ms"] += retained
            else:
                totals["cleaned_protected_ms"] += deleted
            if label.normal_input:
                totals["normal_ms"] += length
                totals["uncertain_normal_ms"] += duration(intersect(region, initial["uncertain"]))
                totals["final_uncertain_normal_ms"] += duration(
                    intersect(region, marks["uncertain"])
                )
                totals["normal_without_first_decision_ms"] += length - duration(
                    intersect(region, initial_ranges)
                )
                totals["normal_without_decision_ms"] += length - duration(
                    intersect(region, decision_ranges)
                )
                totals["normal_unobserved_ms"] += length - duration(
                    intersect(region, observed["seen"])
                )
        run_bindings.append(observed["binding"])

    def ratio(n, d):
        return totals[n] / totals[d] if totals[d] else None

    ratios = {
        "target_time_coverage": ratio("retained_target_ms", "target_ms"),
        "blank_cache_cleanup": ratio("cleaned_blank_ms", "blank_ms"),
        "blank_retention": ratio("retained_blank_ms", "blank_ms"),
        "normal_uncertain": ratio("uncertain_normal_ms", "normal_ms"),
    }
    source_count = len(
        {
            manifest["sources"][parts[i.partition_id]["source"]]["sha256"]
            for i in labels.intervals
            if i.expected_decision == "keep"
        }
    )
    sufficient = (
        len(events) >= 20
        and sum(e["audio_only"] for e in events) >= 3
        and source_count >= 2
        and any(e["critical"] for e in events)
    )
    gates = {
        "minimum_event_counts_and_sources": sufficient,
        "denominators_present": all(totals[k] > 0 for k in ("target_ms", "blank_ms", "normal_ms")),
        "critical_events_complete": not critical_incomplete,
        "target_coverage_95": ratios["target_time_coverage"] is not None
        and ratios["target_time_coverage"] >= 0.95,
        "blank_cleanup_80": ratios["blank_cache_cleanup"] is not None
        and ratios["blank_cache_cleanup"] >= 0.8,
        "normal_uncertain_10": ratios["normal_uncertain"] is not None
        and ratios["normal_uncertain"] <= 0.1,
        "normal_decisions_complete": totals["normal_without_decision_ms"] <= 0.001,
        "normal_observation_coverage_complete": totals["normal_unobserved_ms"] <= 0.001,
        "initial_normal_decisions_complete": totals["normal_without_first_decision_ms"] <= 0.001,
        "no_wrong_cleanup": totals["cleaned_target_ms"] + totals["cleaned_protected_ms"] <= 0.001,
        "actual_inference": has_real,
    }
    return {
        "schema": "cix.observation-quality-result/1",
        "status": "numerical_thresholds_met" if all(gates.values()) else "not_passed",
        "label_digest": digest(labels),
        "reviewer_declaration": labels.reviewer,
        "independent_human_review_verified": False,
        "events": events,
        "event_count": len(events),
        "audio_only_events": sum(e["audio_only"] for e in events),
        "distinct_event_sources": source_count,
        "label_event_types": sorted({i.event_type for i in labels.intervals}),
        "dataset_scenario_coverage_verified": False,
        "wholly_missed_event_ids": missed,
        "incomplete_critical_event_ids": critical_incomplete,
        "duration_denominators_and_counts": totals,
        "ratios": ratios,
        "checks": gates,
        "run_bindings": run_bindings,
        "model_admitted": False,
        "whole_stage_accepted": False,
        "scope": "only frozen labelled validation intervals; cleanup means verified analysis-cache reclamation, not deletion of protected original recordings; semantic labels and other acceptance gates require independent review",
    }
