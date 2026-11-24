"""Native visual requests/results and task-owned rules; no installation paths."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import ID, Contract

Text = Annotated[str, Field(min_length=1, max_length=240)]
Score = Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False)]


class VqaRule(Contract):
    kind: Literal["visual_question"]
    question: Text
    keep_answers: tuple[Text, ...] = Field(default=("yes",), min_length=1, max_length=8)
    discard_answers: tuple[Text, ...] = Field(default=("no",), min_length=1, max_length=8)

    @model_validator(mode="after")
    def distinct_answers(self):
        left, right = (
            set(v.strip().casefold() for v in rows)
            for rows in (self.keep_answers, self.discard_answers)
        )
        if not self.question.strip() or "" in left | right or left & right:
            raise ValueError("question and disjoint nonempty answer sets are required")
        return self


class ContrastiveRule(Contract):
    kind: Literal["text_similarity"]
    positive_text: Text = Field(
        description="CLIP图文相似度候选应使用平行的英文照片描述；例如a photo of a cat。"
    )
    negative_text: Text = Field(
        description="描述可见的替代场景，与正候选使用相同句式，例如a photo of an empty floor；避免no cat/not/without等文字否定，CLIP不是逻辑否定判别器。"
    )
    min_similarity: Score = 0.2
    min_margin: float = Field(default=0.02, gt=0, le=2, allow_inf_nan=False)

    @model_validator(mode="after")
    def distinct_text(self):
        if (
            not self.positive_text.strip()
            or not self.negative_text.strip()
            or (self.positive_text.strip().casefold() == self.negative_text.strip().casefold())
        ):
            raise ValueError("distinct nonempty positive/negative descriptions are required")
        return self


VisualRule = Annotated[VqaRule | ContrastiveRule, Field(discriminator="kind")]


class VisualFrame(Contract):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sample_at_ms: int = Field(strict=True, ge=0)
    jpeg_base64: str = Field(min_length=4, max_length=3 * 1024**2)


class VisualRequest(Contract):
    schema_version: Literal["harness.native-visual/1"] = "harness.native-visual/1"
    request_id: str = Field(pattern=ID)
    model_alias: str = Field(pattern=ID)
    family: Literal["clip", "blip_vqa"]
    revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    frames: tuple[VisualFrame, ...] = Field(min_length=1, max_length=32)
    question: Text | None = None
    texts: tuple[Text, Text] | None = None

    @model_validator(mode="after")
    def native_shape(self):
        if self.family == "clip" and (self.texts is None or self.question is not None):
            raise ValueError("CLIP requires texts, not a question or chat messages")
        if self.family == "blip_vqa" and (self.question is None or self.texts is not None):
            raise ValueError("BLIP VQA requires a question, not candidate scores")
        return self


class NativeFrameResult(Contract):
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    sample_at_ms: int = Field(strict=True, ge=0)
    answer: str | None = Field(default=None, max_length=1000)
    similarities: tuple[Score, Score] | None = None


class VisualResult(Contract):
    request_id: str = Field(pattern=ID)
    model_alias: str = Field(pattern=ID)
    family: Literal["clip", "blip_vqa"]
    revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    simulated: bool = Field(strict=True)
    frames: tuple[NativeFrameResult, ...] = Field(min_length=1, max_length=32)
    inference_seconds: float = Field(ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def result_shape(self):
        for frame in self.frames:
            if self.family == "clip" and (frame.similarities is None or frame.answer is not None):
                raise ValueError("CLIP must return native cosine similarities")
            if self.family == "blip_vqa" and (
                frame.answer is None or frame.similarities is not None
            ):
                raise ValueError("BLIP must return its native answer")
        return self


def evaluate_rule(rule: VisualRule, result: VisualResult):
    """Only valid observations reach this function. Unknown is never a negative."""
    if (isinstance(rule, VqaRule)) != (result.family == "blip_vqa"):
        raise ValueError("task rule is incompatible with the installed model")
    routes = []
    for frame in result.frames:
        if isinstance(rule, VqaRule):
            answer = frame.answer.strip().casefold()
            if answer in {v.strip().casefold() for v in rule.keep_answers}:
                branch = "keep"
            elif answer in {v.strip().casefold() for v in rule.discard_answers}:
                branch = "discard"
            else:
                branch = "protect"
        else:
            positive, negative = frame.similarities
            if positive >= rule.min_similarity and positive - negative >= rule.min_margin:
                branch = "keep"
            elif negative >= rule.min_similarity and negative - positive >= rule.min_margin:
                branch = "discard"
            else:
                branch = "protect"
        routes.append(branch)
    # A match protects the window; discard requires all sampled frames to be negative.
    branch = "keep" if "keep" in routes else "protect" if "protect" in routes else "discard"
    return branch, routes
