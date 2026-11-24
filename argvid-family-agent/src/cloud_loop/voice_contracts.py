"""Small voice wire types shared by the recorder and edge/cloud gateways."""

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import ID, Contract, Invocation


class StreamingASRProof(Contract):
    model: str = Field(min_length=1, max_length=160)
    model_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    runtime: Literal["sherpa-onnx"]
    runtime_version: str = Field(min_length=1, max_length=40)
    processing_host: str = Field(min_length=1, max_length=160)
    recognition_simulated: bool = Field(strict=True)
    source_kind: Literal["file", "microphone"]
    source_simulated: bool = Field(strict=True)
    sample_rate: Literal[16000]
    start_frame: int = Field(strict=True, ge=0)
    end_frame: int = Field(strict=True, gt=0)
    keywords: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def coherent(self):
        if self.recognition_simulated is not False:
            raise ValueError("streaming ASR ingress requires real recognition")
        if self.source_simulated != (self.source_kind == "file"):
            raise ValueError("replay and live provenance differ")
        if not 0 < self.end_frame - self.start_frame <= 60 * self.sample_rate:
            raise ValueError("streaming utterance must be bounded actual audio")
        return self


class VoiceUtterance(Contract):
    event_id: str = Field(pattern=ID)
    transcript: str = Field(min_length=1, max_length=8000)
    source_ref: str = Field(min_length=1, max_length=400)
    received_at: datetime
    recorded_at: datetime | None = None
    replayed_at: datetime | None = None
    audio_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    asr: dict = Field(default_factory=dict)
    purpose: Literal["user_command", "media_content"] = "user_command"
    _aware = field_validator("received_at", "recorded_at", "replayed_at")(
        lambda value: Invocation.aware(value) if value is not None else None
    )


def validate_streaming_utterance(utterance: VoiceUtterance) -> StreamingASRProof:
    proof = StreamingASRProof.model_validate(utterance.asr)
    if not utterance.audio_sha256:
        raise ValueError("streaming ASR requires an actual recording hash")
    if proof.source_kind == "file" and utterance.replayed_at is None:
        raise ValueError("streaming replay requires replay time")
    if proof.source_kind == "microphone" and (
        utterance.recorded_at is None or utterance.replayed_at is not None
    ):
        raise ValueError("microphone input requires recording time and cannot claim replay")
    return proof
