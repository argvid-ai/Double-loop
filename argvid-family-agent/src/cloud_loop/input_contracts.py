"""Installation and ingress contracts, independent of capture process execution."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .contracts import Contract
from .observation_contracts import Reference
from .observation_inputs import InputBinding


class CaptureEndpoint(Contract):
    driver: str = Field(pattern="^(v4l2|rtsp|alsa|avfoundation|file_replay|paired_stream)$")
    address: str = Field(min_length=1, max_length=300)
    audio_device: str | None = Field(default=None, max_length=80)
    input_format: Literal["mjpeg", "yuyv422"] | None = None
    video_size: str | None = Field(default=None, pattern=r"^[1-9][0-9]{1,3}x[1-9][0-9]{1,3}$")
    framerate: int | None = Field(default=None, strict=True, ge=1, le=120)
    audio_channels: int | None = Field(default=None, strict=True, ge=1, le=8)
    audio_sample_rate: int | None = Field(default=None, strict=True, ge=8000, le=192000)
    video_bitrate_bps: int | None = Field(default=None, strict=True, ge=64000, le=100000000)

    @model_validator(mode="after")
    def safe_endpoint(self):
        if any(v is not None for v in (self.input_format, self.video_size, self.framerate)):
            if self.driver != "v4l2":
                raise ValueError("explicit video capture format requires V4L2")
        if self.video_bitrate_bps is not None and self.driver in {"alsa", "paired_stream"}:
            raise ValueError("video bitrate requires an owned video encoder")
        if self.audio_channels is not None or self.audio_sample_rate is not None:
            if not (self.driver == "alsa" or (self.driver == "v4l2" and self.audio_device)):
                raise ValueError("explicit microphone format requires an ALSA input")
        if self.driver == "paired_stream" and not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,199}", self.address
        ):
            raise ValueError("paired stream address must name an installed source")
        if self.driver == "file_replay" and not Path(self.address).is_absolute():
            raise ValueError("replay capture requires an installed absolute file path")
        if self.driver == "v4l2" and not re.fullmatch(
            r"/dev/(?:video[0-9]+|v4l/by-id/[A-Za-z0-9][A-Za-z0-9_.:-]*)", self.address
        ):
            raise ValueError("invalid V4L2 node")
        if self.driver == "alsa" and not re.fullmatch(
            r"(default|hw:[0-9]+(?:,[0-9]+)?|hw:CARD=[A-Za-z0-9_]+,DEV=[0-9]+)", self.address
        ):
            raise ValueError("invalid ALSA device")
        if self.driver == "avfoundation" and not re.fullmatch(
            r"([0-9]+|none):([0-9]+|none)", self.address
        ):
            raise ValueError("invalid AVFoundation indices")
        if self.driver == "avfoundation" and self.address == "none:none":
            raise ValueError("AVFoundation requires at least one input")
        if self.driver == "rtsp":
            url = urlsplit(self.address)
            _ = url.port  # Validate a malformed/out-of-range port before invoking FFmpeg.
            if (
                url.scheme not in {"rtsp", "rtsps"}
                or not url.hostname
                or url.username
                or url.password
                or url.query
                or url.fragment
            ):
                raise ValueError("use a credential-free configured RTSP endpoint")
        if self.audio_device and (
            self.driver != "v4l2"
            or not re.fullmatch(
                r"(default|hw:[0-9]+(?:,[0-9]+)?|hw:CARD=[A-Za-z0-9_]+,DEV=[0-9]+)",
                self.audio_device,
            )
        ):
            raise ValueError("additional V4L2 sound must be an explicit ALSA input")
        return self


class InputSourceSpec(Contract):
    binding: InputBinding
    title: str = Field(min_length=1, max_length=120)
    location_ref: Reference | None = None
    max_file_bytes: int = Field(default=64 * 1024**2, strict=True, gt=0, le=512 * 1024**2)
    quota_bytes: int = Field(default=512 * 1024**2, strict=True, gt=0, le=1024**5)
    min_free_bytes: int = Field(default=64 * 1024**2, strict=True, ge=0)
    max_window_ms: int = Field(default=60000, strict=True, ge=100, le=60000)
    max_file_duration_ms: int = Field(default=3600000, strict=True, ge=100, le=86400000)
    capture: CaptureEndpoint | None = None
    continuous_capture: bool = False
    protect_recordings: bool = False
    wait_for_client: bool = False
    incremental_video: bool = False
    video_publish_interval_ms: int | None = Field(default=None, strict=True, ge=100, le=10000)
    recording_segment_ms: int = Field(default=60000, strict=True, ge=1000, le=3600000)
    replay_duration_ms: int | None = Field(default=None, strict=True, ge=1000, le=86400000)
    shared_voice_config_file: str | None = None
    idle_timeout_seconds: int = Field(default=30, strict=True, ge=5, le=300)

    @model_validator(mode="after")
    def budgets(self):
        if self.wait_for_client and (
            (self.capture is not None and self.capture.driver != "paired_stream")
            or self.binding.finite
        ):
            raise ValueError("client waiting applies to a live paired input")
        if (
            self.capture
            and self.capture.driver == "paired_stream"
            and (
                self.capture.address != self.binding.source_ref
                or not self.continuous_capture
                or not self.incremental_video
                or "image" not in self.binding.modalities
                or self.shared_voice_config_file
            )
        ):
            raise ValueError(
                "paired recording requires its own source and incremental video; shared speech is not installed"
            )
        if self.replay_duration_ms is not None and not (
            self.continuous_capture
            and self.capture
            and self.capture.driver == "file_replay"
            and self.binding.simulated
        ):
            raise ValueError("bounded replay completion requires simulated continuous file capture")
        if self.capture and self.capture.driver == "file_replay" and not self.binding.simulated:
            raise ValueError("file replay must be explicitly simulated as a physical source")
        if self.continuous_capture:
            if not self.capture:
                raise ValueError("continuous capture needs a driver")
            if self.recording_segment_ms < 60000 and not (
                self.binding.simulated and self.capture.driver == "file_replay"
            ):
                raise ValueError("production recording segments must be at least 60 seconds")
            if self.recording_segment_ms > self.max_file_duration_ms:
                raise ValueError("recording segment exceeds the registered file duration limit")
        if self.incremental_video and (
            not self.continuous_capture or "image" not in self.binding.modalities
        ):
            raise ValueError("incremental video requires continuous video capture")
        if self.shared_voice_config_file and (
            not self.continuous_capture
            or "audio" not in self.binding.modalities
            or not Path(self.shared_voice_config_file).is_absolute()
        ):
            raise ValueError(
                "shared speech requires continuous audio capture and an absolute installed config"
            )
        if self.max_file_bytes > self.quota_bytes:
            raise ValueError("one input file exceeds source quota")
        if (
            self.capture
            and self.capture.driver == "v4l2"
            and "audio" in self.binding.modalities
            and not self.capture.audio_device
        ):
            raise ValueError("V4L2 audio requires an explicit microphone binding")
        if self.capture and self.capture.driver == "alsa" and self.binding.modalities != ("audio",):
            raise ValueError("ALSA capture is audio-only")
        if self.capture and self.binding.finite:
            raise ValueError(
                "device capture must use a live source; finite input is a file/push session"
            )
        return self


class InputFile(Contract):
    event_id: Reference
    segment_id: Reference
    start_ms: int = Field(strict=True, ge=0)
    end_ms: int | None = Field(default=None, strict=True, gt=0)
    file_start_ms: int = Field(default=0, strict=True, ge=0)
    capture_revision: Reference | None = None
    recorded_start_at: datetime | None = None
    clock_authority: str = Field(
        default="unknown", pattern="^(unknown|device_reported|node_reported|operator_verified)$"
    )

    @model_validator(mode="after")
    def time(self):
        if self.end_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("empty input range")
        if self.recorded_start_at is not None:
            if self.recorded_start_at.tzinfo is None or self.recorded_start_at.utcoffset() is None:
                raise ValueError("recording time requires timezone")
            if self.clock_authority == "unknown":
                raise ValueError("recording clock provenance is required")
        elif self.clock_authority != "unknown":
            raise ValueError("known clock requires a recording time")
        return self
