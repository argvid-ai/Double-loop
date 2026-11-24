"""Versioned, read-only planning table; probabilities are explicit assumptions.

This component does not authorize tasks or certify quality. It uses independent
per-frame positive decisions, not joint multi-image inference probabilities.
"""

import json
import math
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import Field

from .contracts import Contract

TABLE_PATH = Path(__file__).with_name("data") / "sampling_lookup.v1.json"


class SamplingLookupQuery(Contract):
    target_detection: Literal[0.7, 0.8, 0.9]
    effective_frame_probability: Literal[0.7, 0.8, 0.9]
    conditional_detection_probability: Literal[0.7, 0.8, 0.9]
    assume_independent: Literal[True] = Field(
        description="必须明确采用独立采样机会假设；这不是相邻视频帧实际独立的证明。"
    )
    window_seconds: Literal[5, 10, 15] | None = None
    model: Literal["clip", "blip_vqa", "qwen"] | None = None


def discovery_probability(hit, conditional, samples):
    for value in [hit, conditional]:
        if isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("probabilities must be finite and in [0,1]")
    if type(samples) is not int or samples < 0:
        raise ValueError("samples must be a nonnegative integer")
    success = hit * conditional
    if success == 1:
        return float(samples > 0)
    return -math.expm1(samples * math.log1p(-success))


def minimum_samples(hit, conditional, target):
    discovery_probability(hit, conditional, 0)
    if isinstance(target, bool) or not math.isfinite(target) or not 0 < target <= 1:
        raise ValueError("target must be in (0,1]")
    success = hit * conditional
    if success == 0 or (target == 1 and success < 1):
        return None
    if success == 1:
        return 1
    count = max(1, math.ceil(math.log1p(-target) / math.log1p(-success)))
    while discovery_probability(hit, conditional, count) + 1e-14 < target:
        count += 1
    while count > 1 and discovery_probability(hit, conditional, count - 1) >= target - 1e-14:
        count -= 1
    return count


def lookup_sampling_plan(query):
    query = SamplingLookupQuery.model_validate(query)
    raw = TABLE_PATH.read_bytes()
    table = json.loads(raw)
    rows = [
        row
        for row in table["rows"]
        if row["target_detection"] == query.target_detection
        and row["effective_frame_probability"] == query.effective_frame_probability
        and row["conditional_detection_probability"] == query.conditional_detection_probability
        and (query.window_seconds is None or row["window_seconds"] == query.window_seconds)
        and (query.model is None or row["model"] == query.model)
    ]
    return {
        "table_version": table["version"],
        "table_sha256": sha256(raw).hexdigest(),
        "status": "conditional_planning_only",
        "probability_basis": "user_supplied_independent_trials_assumption_not_model_accuracy",
        "assume_independent": True,
        "execution_admitted": False,
        "model_assessments": {
            name: {
                k: {dk: dv for dk, dv in value[k].items() if dk not in {"trials", "evidence"}}
                if k == "diagnostic"
                else value[k]
                for k in ["title", "diagnostic", "assessment", "current_multiframe_mode"]
            }
            for name, value in table["models"].items()
            if query.model is None or name == query.model
        },
        "runtime_cost_profiles": {
            name: {
                k: v
                for k, v in profile.items()
                if k
                in {
                    "id",
                    "status",
                    "hardware",
                    "sampling_interval_seconds",
                    "model_threads",
                    "history_dependent",
                    "context_mode",
                    "pipeline_stages",
                    "submitted_images_range",
                    "preparation_basis",
                    "quality_certified",
                    "long_duration_verified",
                }
            }
            for name, profile in table.get("runtime_cost_profiles", {}).items()
            if query.model is None or name == query.model
        },
        # Keep the full coefficients/statistics in the versioned catalogue. A
        # nine-row planning reply must fit NanoBot's existing tool-result budget
        # rather than being spilled to a file and queried repeatedly.
        "rows": [
            {
                k: v
                for k, v in row.items()
                if k
                not in {
                    "cost_basis",
                    "execution_mode",
                    "judgment_stride_seconds",
                    "target_detection",
                    "effective_frame_probability",
                    "conditional_detection_probability",
                    "estimated_serial_seconds",
                    "estimated_handoff_seconds",
                    "samples_per_second",
                    "independent_frame_evaluations_per_hour",
                    "measured_shape_match",
                }
            }
            for row in rows
        ],
        "query_assumptions": query.model_dump(),
        "limitations": table["limitations"],
    }
