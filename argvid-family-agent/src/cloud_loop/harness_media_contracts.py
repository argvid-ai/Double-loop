"""Functional media contracts, separate from the frozen legacy six-second protocol."""

from typing import Literal

from pydantic import Field, model_validator

from .contracts import ID, Contract


class MediaSegment(Contract):
    source_id: str | None = Field(default=None, pattern=ID)
    asset_id: str | None = Field(default=None, pattern=ID)
    start_ms: int = Field(default=0, ge=0)
    duration_ms: int = Field(ge=100)
    crop: tuple[int, int, int, int] | None = None

    @model_validator(mode="after")
    def valid_source(self):
        if (self.source_id is None) == (self.asset_id is None):
            raise ValueError("select exactly one registered source or asset")
        if self.crop and (min(self.crop[:2]) < 0 or min(self.crop[2:]) < 2):
            raise ValueError("invalid crop")
        return self


class MediaRecipe(Contract):
    renderer: Literal["ffmpeg", "remotion"] = "ffmpeg"
    segments: list[MediaSegment] = Field(min_length=1)
    width: int = Field(default=1080, ge=160, le=1920)
    height: int = Field(default=1920, ge=160, le=1920)
    fps: Literal[24, 25, 30] = 30
    audio: Literal["mute", "preserve"] = Field(
        default="mute",
        description="已有合同默认mute；用户未要求静音的新剪辑应显式选preserve，保留已有音轨；无音轨的选段会在剪辑时明确记录静音填充。",
    )

    @model_validator(mode="after")
    def supported_shape(self):
        if self.renderer == "remotion" and any(s.crop is not None for s in self.segments):
            raise ValueError("Remotion preset does not yet support crop; use the ffmpeg renderer")
        if self.width % 2 or self.height % 2:
            raise ValueError("H.264 dimensions must be even")
        if any(s.duration_ms * self.fps % 1000 for s in self.segments):
            raise ValueError("each segment duration must be an exact frame count at the output fps")
        return self

    @property
    def duration_ms(self):
        return sum(s.duration_ms for s in self.segments)


class MediaProject(Contract):
    """An agent-authored Remotion project, submitted to the existing job runner."""

    directory: str = Field(min_length=1, max_length=1000)
    entry_point: str = Field(default="src/index.tsx", min_length=1, max_length=400)
    composition_id: str = Field(min_length=1, max_length=100)
    asset_ids: list[str] = Field(default_factory=list)


class SourceRegistration(Contract):
    source_id: str = Field(pattern=ID)
    path: str = Field(min_length=1, max_length=1000)
    title: str = Field(min_length=1, max_length=160)
    source_kind: Literal["user_provided_video", "licensed_stock_video"]
    source_ref: str = Field(min_length=1, max_length=1000)
    license: str = Field(min_length=1, max_length=200)
    author: str = Field(default="", max_length=160)


class SourceQuery(Contract):
    target: Literal["local", "peer"] = "local"
    source_id: str | None = Field(default=None, pattern=ID)


class SourceFrame(Contract):
    source_id: str = Field(pattern=ID)
    position_ms: int = Field(ge=0)


class FrameRead(SourceFrame):
    source_id: str | None = Field(default=None, pattern=ID)
    media_ref: str | None = Field(default=None, pattern=ID)
    target: Literal["local", "peer"] = "local"

    @model_validator(mode="after")
    def one_reference(self):
        if (self.source_id is None) == (self.media_ref is None):
            raise ValueError("select one registered source_id or raw media_ref")
        return self


class PeerFrameRead(FrameRead):
    principal: str = Field(pattern=ID)


class PlaybackControl(SourceFrame):
    playing: bool = False


class MediaTransfer(Contract):
    request_id: str = Field(pattern=ID)
    principal: str = Field(pattern=ID)
    manifest: dict
    content_base64: str = Field(min_length=1, max_length=40_000_000)


class PeerSourceQuery(Contract):
    principal: str = Field(pattern=ID)
    source_id: str | None = Field(default=None, pattern=ID)
