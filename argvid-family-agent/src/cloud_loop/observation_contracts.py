"""Observation task and ledger inputs; references are resolved by the host.

No reference here is a host path or proof that media/model resources are ready.
Media time is integer milliseconds within a continuous segment. Ledger sequence
numbers span the whole task, including explicit missing ranges, starting at zero.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import ID, Contract
from .observation_semantics import SemanticDecision
from .visual_contracts import VisualRule

Reference = Annotated[str, Field(pattern=ID)]
Counter = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]
Version = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]
Timestamp = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class ObservationConfig(Contract):
    task_id: Reference
    principal: Reference
    project_id: Reference
    version: Version
    device_ref: Reference
    source_ref: Reference
    session_ref: Reference
    storage_binding_ref: Reference
    model_preset_ref: Reference
    goal: str = Field(min_length=1, max_length=8000)
    batch_policy_ref: Reference
    cache_policy_ref: Reference
    save_policy_ref: Reference
    tools: tuple[Reference, ...] = ()
    skills: tuple[Reference, ...] = ()
    ptz_scope_ref: Reference | None = None
    expires_at: Timestamp | None = None
    capture_until: Timestamp | None = None
    resume_after_restart: bool = False
    notification_policy: Literal["lifecycle_only", "registered_policy"] = "lifecycle_only"
    notification_policy_ref: Reference | None = None
    executor_ref: Reference | None = None
    decision: SemanticDecision | None = None
    visual_rule: VisualRule | None = None
    output_mode: Literal["retained_media", "markers"] = "retained_media"

    @model_validator(mode="after")
    def validate_policy(self):
        if self.resume_after_restart and self.capture_until is None:
            raise ValueError("restart recovery requires a bounded owned capture")
        if self.capture_until is not None:
            if (
                self.expires_at is None
                or not 0 < self.expires_at - self.capture_until <= 30
                or self.output_mode != "markers"
                or self.ptz_scope_ref is not None
                or self.skills
                or set(self.tools) - {"mark_media", "read_media", "get_edge_state"}
            ):
                raise ValueError("bounded capture drain requires marker-only work and at most 30 seconds")
        if self.decision is not None and self.visual_rule is not None:
            raise ValueError("semantic decision and native rule are mutually exclusive")
        if (self.executor_ref is None) != (self.decision is None and self.visual_rule is None):
            raise ValueError("named executor and semantic decision must be bound together")
        if self.output_mode == "markers" and self.executor_ref is None:
            raise ValueError("marker output requires a named observation executor")
        if not self.goal.strip():
            raise ValueError("goal must not be blank")
        if len(set(self.tools)) != len(self.tools) or len(set(self.skills)) != len(self.skills):
            raise ValueError("duplicate tool or skill reference")
        if (self.notification_policy == "registered_policy") != (
            self.notification_policy_ref is not None
        ):
            raise ValueError("registered notification policy requires its reference")
        return self


class ObservationRun(Contract):
    principal: Reference
    task_id: Reference
    version: Version
    generation: Counter


class ObservationCommand(ObservationRun):
    """confirm_started/fail/recover are host acknowledgements, not model actions.

    start/resume reserve a new generation in 'starting'. Only confirm_started
    reports a running subscription. On process restart, the host must recover
    each formerly starting/running task before establishing new subscriptions.
    """

    request_id: Reference
    action: Literal["start", "pause", "resume", "stop", "confirm_started", "fail", "recover"]
    reason: str | None = Field(default=None, min_length=1, max_length=2000)

    @model_validator(mode="after")
    def failure_reason(self):
        if self.action in {"fail", "recover"} and (not self.reason or not self.reason.strip()):
            raise ValueError("fail/recover requires a reason")
        return self


class MediaRange(ObservationRun):
    source_ref: Reference
    segment_id: Reference
    sequence: Counter
    start_ms: Counter
    end_ms: Counter

    @model_validator(mode="after")
    def nonempty_range(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("media range must be nonempty [start_ms, end_ms)")
        return self


class ObservationBatch(MediaRange):
    batch_id: Reference
    media_ref: Reference  # registered manifest/payload reference, not media bytes
    media_parts: tuple[Reference, ...] = ()  # Ordered original files for one analysis window.
    captured_at: Timestamp
    segment_start_ms: Counter = 0
    simulated: bool = Field(strict=True)
    source_fingerprint: tuple[Counter, Counter, Counter, Counter, Counter] | None = None
    camera_pose_revision: Counter | None = None
    camera_state_simulated: bool | None = Field(default=None, strict=True)
    missing_modalities: tuple[Literal["image", "audio"], ...] = ()

    @model_validator(mode="after")
    def distinct_missing_modalities(self):
        if self.segment_start_ms > self.start_ms:
            raise ValueError("segment start exceeds batch start")
        if len(set(self.missing_modalities)) != len(self.missing_modalities):
            raise ValueError("duplicate missing modality")
        return self


class ObservationGap(MediaRange):
    """An unavailable source interval, not a processed batch or inferred state."""

    gap_id: Reference
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class DeferredBatch(ObservationRun):
    """Explicitly skip a recorded batch in live context, retaining its debt."""

    request_id: Reference
    batch_id: Reference
    reason: str = Field(min_length=1, max_length=2000, pattern=r"\S")


class SeenBatch(ObservationRun):
    """Host assertion of actual consumption, never inferred from queue removal.

    evidence_ref identifies the model request/result record. Backfill uses a
    separate session; this receipt grants no device or file-deletion authority.
    """

    request_id: Reference
    batch_id: Reference
    session_ref: Reference
    evidence_ref: Reference
    mode: Literal["live", "backfill"] = "live"
    inference_simulated: bool = Field(default=False, strict=True)
