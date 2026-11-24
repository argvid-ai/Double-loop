"""Prepare real, time-aligned observation input from a registered replay file.

The Gateway supplies the source and interval; the model never chooses a host
path. This adapter neither schedules windows nor decides which video to retain.
Original bytes remain read-only. Missing media fails explicitly, without a
previous-frame substitution, duplicated audio, or silent padding.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import struct
import sys
import time
import wave
from array import array
from dataclasses import asdict, dataclass
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path
from typing import Callable

import av
from PIL import Image


class IncompleteObservation(ValueError):
    def __init__(self, modality: str, detail: str):
        self.modality = modality
        super().__init__(f"{modality} unavailable: {detail}")


@dataclass(frozen=True)
class SamplingPolicy:
    frame_interval_ms: int = 500
    image_long_edge: int = 448
    audio_rate: int = 16000
    include_audio: bool = True
    image_region: tuple[float, float, float, float] | None = None
    include_image: bool = True
    validate_full_window: bool = True

    def __post_init__(self):
        if type(self.validate_full_window) is not bool:
            raise ValueError("image coverage validation policy must be explicit boolean")
        if type(self.include_image) is not bool or not (self.include_image or self.include_audio):
            raise ValueError("at least one explicit analysis modality is required")
        if type(self.include_audio) is not bool:
            raise ValueError("audio input policy must be explicit boolean")
        if type(self.frame_interval_ms) is not int or not 100 <= self.frame_interval_ms <= 10000:
            raise ValueError("unsupported frame sampling interval")
        if type(self.image_long_edge) is not int or not 32 <= self.image_long_edge <= 1024:
            raise ValueError("unsupported analysis image size")
        if type(self.audio_rate) is not int or self.audio_rate not in (16000, 24000, 48000):
            raise ValueError("unsupported analysis audio rate")
        if self.image_region is not None:
            if (
                len(self.image_region) != 4
                or any(
                    type(v) not in (int, float) or not math.isfinite(v) for v in self.image_region
                )
                or not 0 <= self.image_region[0] < self.image_region[2] <= 1
                or not 0 <= self.image_region[1] < self.image_region[3] <= 1
            ):
                raise ValueError("image region must be normalized left, top, right, bottom")


def _fingerprint(path: Path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _file_record(path: Path):
    data = path.read_bytes()
    return {"file": path.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


class FileReplayWindow:
    simulated = True

    def __init__(
        self,
        source: Path,
        source_ref: str,
        cache_root: Path,
        policy: SamplingPolicy = SamplingPolicy(),
        *,
        video_reader=None,
        source_identity=None,
        validate_source=None,
        memory_images=False,
    ):
        if memory_images and policy.include_audio:
            raise ValueError("memory image preparation requires an image-only consumer")
        self.memory_images = memory_images
        self.frame_bytes = {}
        self.video_reader = video_reader
        self.source = source.resolve(strict=True)
        if not self.source.is_file():
            raise ValueError("registered source must be a regular file")
        self.source_ref, self.policy = source_ref, policy
        self.cache_root = cache_root.resolve()
        if self.source.is_relative_to(self.cache_root):
            raise ValueError("source must be outside disposable analysis cache")
        self.fingerprint = (
            tuple(source_identity) if source_identity is not None else _fingerprint(self.source)
        )
        self.validate_source = validate_source
        self.timings = self._new_timings()

    @staticmethod
    def _new_timings():
        timings = {
            key: 0.0
            for key in (
                "open_seek_seconds",
                "decode_seconds",
                "transform_seconds",
                "jpeg_write_seconds",
                "hash_seconds",
            )
        }
        timings.update(opens=0, seeks=0, decoded_frames=0)
        return timings

    def prepare(
        self,
        start_ms: int,
        end_ms: int,
        *,
        batch_id: str,
        check_active: Callable[[], None] = lambda: None,
    ) -> dict:
        """Produce one new interval. Caller must include this cost in latency.

        Results are immutable per batch ID. A manifest is published only after
        both modalities cover the requested range. On failure only the newly
        created analysis directory is removed; source/HD storage is untouched.
        """
        import re

        from .contracts import ID

        if not isinstance(batch_id, str) or not re.fullmatch(ID, batch_id):
            raise ValueError("invalid batch identity")
        if (
            type(start_ms) is not int
            or type(end_ms) is not int
            or start_ms < 0
            or not 0 < end_ms - start_ms <= 60000
        ):
            raise ValueError("invalid or unbounded observation interval")
        if self.policy.include_audio and any(
            ms * self.policy.audio_rate % 1000 for ms in (start_ms, end_ms)
        ):
            raise ValueError("audio boundaries must be exact sample positions")
        check_active()
        if self.validate_source:
            self.validate_source()
        elif _fingerprint(self.source) != self.fingerprint:
            raise ValueError("registered source changed")
        folder = self.cache_root / batch_id
        if not self.memory_images:
            self.cache_root.mkdir(parents=True, exist_ok=True)
            folder.mkdir()  # No overwrite or reuse of an unverified prior payload.
        started = time.monotonic()
        self.timings = self._new_timings()
        try:
            images = (
                self._images(start_ms, end_ms, folder, check_active)
                if self.policy.include_image
                else []
            )
            sound = (
                self._audio(start_ms, end_ms, folder, check_active)
                if self.policy.include_audio
                else None
            )
            check_active()
            if self.validate_source:
                self.validate_source()
            elif _fingerprint(self.source) != self.fingerprint:
                raise ValueError("source changed during preparation")
            manifest = {
                "schema": "cix.observation-media/1",
                "batch_id": batch_id,
                "source_ref": self.source_ref,
                "source_file": str(self.source),
                "source_fingerprint": list(self.fingerprint),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "simulated": True,
                "source_kind": "file_replay",
                "actual_media": True,
                "frames": images,
                "audio": sound,
                "missing_modalities": [],
                "audio_input": "provided" if self.policy.include_audio else "not_requested",
                "policy": asdict(self.policy),
                "preparation_seconds": time.monotonic() - started,
                "image_preparation_timing": dict(self.timings),
                "image_coverage_check": "full_window"
                if self.policy.validate_full_window
                else "sampled_frames_only",
                "image_reader": "continuous" if self.video_reader else "window",
            }
            if self.memory_images:
                return {"manifest": manifest, "directory": folder,
                        "storage": "memory", "frame_bytes": self.frame_bytes}
            (folder / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
            )
            return {"manifest": manifest, "directory": folder}
        except BaseException:
            if self.video_reader is not None:
                self.video_reader.reset()
            if not self.memory_images:
                shutil.rmtree(folder)
            self.frame_bytes.clear()
            raise

    @contextmanager
    def _video_frames(self, start, end):
        if self.video_reader is not None:
            if self.video_reader.source != self.source:
                raise ValueError("continuous reader belongs to another source")
            with self.video_reader.window(start, end, self.timings) as frames:
                yield frames
            return
        began = time.monotonic()
        with av.open(str(self.source)) as container:
            self.timings["opens"] += 1
            if not container.streams.video:
                raise IncompleteObservation("image", "no video track")
            stream = container.streams.video[0]
            container.seek(
                int(Fraction(start, 1000) / stream.time_base), stream=stream, backward=True
            )
            self.timings["seeks"] += 1
            self.timings["open_seek_seconds"] += time.monotonic() - began
            yield self._timed_decode(iter(container.decode(stream)))

    def _timed_decode(self, decoder):
        while True:
            began = time.monotonic()
            try:
                frame = next(decoder)
            except StopIteration:
                self.timings["decode_seconds"] += time.monotonic() - began
                return
            self.timings["decode_seconds"] += time.monotonic() - began
            self.timings["decoded_frames"] += 1
            yield frame

    def images(self, start, end, folder, check):
        """Public per-range frame extraction boundary for media adapters."""
        return self._images(start, end, folder, check)

    def audio(self, start, end, folder, check):
        """Public per-range audio preparation boundary for media adapters."""
        return self._audio(start, end, folder, check)

    def _images(self, start, end, folder, check):
        target = start
        result = []
        previous = None
        rotation = 0
        last_end = None
        with self._video_frames(start, end) as frames:
            rotation = self.video_reader.rotation if self.video_reader else 0
            for frame in frames:
                check()
                # Display matrices are often attached only to the first
                # decoded frame, including the frame preceding a seek target.
                if any(side.type.name == "DISPLAYMATRIX" for side in frame.side_data):
                    matrix = next(
                        side for side in frame.side_data if side.type.name == "DISPLAYMATRIX"
                    )
                    values = struct.unpack("=9i", bytes(matrix))
                    a, b, _, c, d, _, _, _, _ = values
                    if a * d - b * c < 0:
                        raise IncompleteObservation(
                            "image", "reflected display matrix is unsupported"
                        )
                    if (
                        values[2]
                        or values[5]
                        or values[8] != 1 << 30
                        or a * a + b * b != 1 << 32
                        or c * c + d * d != 1 << 32
                        or a * c + b * d
                    ):
                        raise IncompleteObservation(
                            "image", "non-rotation display transform is unsupported"
                        )
                    rotation = int(frame.rotation)
                    if self.video_reader is not None:
                        self.video_reader.rotation = rotation
                    if rotation not in (-180, -90, 0, 90, 180):
                        raise IncompleteObservation("image", "unsupported display rotation")
                if frame.pts is None:
                    raise IncompleteObservation("image", "frame has no timestamp")
                pts = Fraction(frame.pts) * frame.time_base * 1000
                if previous is not None and pts < previous:
                    raise IncompleteObservation("image", "frame timestamps regressed")
                previous = pts
                last_end = pts + Fraction(frame.duration or 0) * frame.time_base * 1000
                if target >= end:
                    if last_end >= end:
                        return result
                    continue
                # A sample may fall inside the final frame before a file boundary.
                # Use its declared display duration, never an invented held frame.
                if pts < target and last_end <= target:
                    continue
                if pts >= min(target + self.policy.frame_interval_ms, end):
                    raise IncompleteObservation("image", f"missing sampling interval at {target}ms")
                transform_started = time.monotonic()
                picture = frame.to_image()
                if rotation:
                    picture = picture.rotate(rotation, expand=True)
                region_record = {}
                if self.policy.image_region is not None:
                    left, top, right, bottom = self.policy.image_region
                    box = (
                        math.floor(left * picture.width),
                        math.floor(top * picture.height),
                        math.ceil(right * picture.width),
                        math.ceil(bottom * picture.height),
                    )
                    region_record = {
                        "image_region": list(self.policy.image_region),
                        "region_pixels": list(box),
                        "original_display_size": [picture.width, picture.height],
                    }
                    picture = picture.crop(box)
                picture.thumbnail((self.policy.image_long_edge,) * 2, Image.Resampling.LANCZOS)
                self.timings["transform_seconds"] += time.monotonic() - transform_started
                path = folder / f"frame-{len(result):04d}.jpg"
                jpeg_started = time.monotonic()
                if self.memory_images:
                    import io

                    with io.BytesIO() as output:
                        picture.save(output, format="JPEG", quality=90)
                        data = output.getvalue()
                    self.frame_bytes[path.name] = data
                else:
                    picture.save(path, quality=90)
                self.timings["jpeg_write_seconds"] += time.monotonic() - jpeg_started
                hash_started = time.monotonic()
                record = (
                    {"file": path.name, "bytes": len(data),
                     "sha256": hashlib.sha256(data).hexdigest()}
                    if self.memory_images else _file_record(path)
                )
                self.timings["hash_seconds"] += time.monotonic() - hash_started
                result.append(
                    {
                        **record,
                        "sample_at_ms": target,
                        "source_pts_ms": float(pts),
                        "source_frame_end_ms": float(last_end),
                        "rotation_applied": rotation,
                        "width": picture.width,
                        "height": picture.height,
                        **region_record,
                    }
                )
                target += self.policy.frame_interval_ms
                if target >= end and (not self.policy.validate_full_window or last_end >= end):
                    return result
        raise IncompleteObservation("image", f"source ended before {target}ms")

    def _audio(self, start, end, folder, check):
        rate = self.policy.audio_rate
        start_sample, end_sample = start * rate // 1000, end * rate // 1000
        samples = array("h", [0]) * (end_sample - start_sample)
        covered = bytearray(len(samples))
        count = 0

        def consume(frame):
            nonlocal count
            if frame.pts is None:
                raise IncompleteObservation("audio", "audio has no timestamp")
            absolute = round(Fraction(frame.pts) * frame.time_base * rate)
            relative = absolute - start_sample
            low, high = max(relative, 0), min(relative + frame.samples, len(samples))
            if high <= low:
                return
            if any(covered[low:high]):
                raise IncompleteObservation("audio", "overlapping samples")
            values = array("h")
            values.frombytes(bytes(frame.planes[0])[: frame.samples * 2])
            samples[low:high] = values[low - relative : high - relative]
            covered[low:high] = b"\x01" * (high - low)
            count += high - low

        with av.open(str(self.source)) as container:
            if not container.streams.audio:
                raise IncompleteObservation("audio", "no audio track")
            stream = container.streams.audio[0]
            stream_end = (
                ((stream.start_time or 0) + stream.duration) * stream.time_base
                if stream.duration is not None
                else None
            )
            if stream_end is not None and Fraction(end, 1000) > stream_end:
                raise IncompleteObservation(
                    "audio", "requested range exceeds the declared audio duration"
                )
            # Preroll retains the resampling filter's actual source samples.
            seek_ms = max(0, start - 1000)
            container.seek(
                int(Fraction(seek_ms, 1000) / stream.time_base), stream=stream, backward=True
            )
            resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
            for frame in container.decode(stream):
                check()
                if frame.pts is None:
                    raise IncompleteObservation("audio", "audio has no timestamp")
                if Fraction(frame.pts) * frame.time_base * 1000 > end + 1000:
                    break
                # AAC commonly decodes a complete final frame even though only
                # part of it belongs to the track. Never count encoder padding
                # as captured audio or feed it to the resampler as valid data.
                skip, discard = 0, 0
                for side in frame.side_data:
                    if side.type.name == "SKIP_SAMPLES":
                        skip, discard = struct.unpack("<II", bytes(side)[:8])
                valid = frame.samples - discard
                if frame.duration:
                    valid = min(
                        valid, int(Fraction(frame.duration) * frame.time_base * frame.sample_rate)
                    )
                if stream_end is not None:
                    valid = min(
                        valid,
                        int(
                            (stream_end - Fraction(frame.pts) * frame.time_base) * frame.sample_rate
                        ),
                    )
                valid = max(0, valid - skip)
                if not valid:
                    continue
                if valid != frame.samples or skip:
                    frame = self._trim_audio(frame, skip, valid)
                for converted in resampler.resample(frame):
                    consume(converted)
                if (
                    count == len(samples)
                    or Fraction(frame.pts) * frame.time_base * 1000 > end + 1000
                ):
                    break
            if count != len(samples):
                for converted in resampler.resample(None):
                    consume(converted)
        if count != len(samples):
            raise IncompleteObservation(
                "audio", f"{len(samples) - count} source samples missing; no silence inserted"
            )
        if sys.byteorder != "little":
            samples.byteswap()
        path = folder / "audio.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(rate)
            output.writeframes(samples.tobytes())
        return {
            **_file_record(path),
            "start_ms": start,
            "end_ms": end,
            "samples": len(samples),
            "sample_rate": rate,
            "channels": 1,
            "sample_width_bytes": 2,
            "silence_padding_samples": 0,
        }

    @staticmethod
    def _trim_audio(frame, skip, samples):
        trimmed = av.AudioFrame(format=frame.format.name, layout=frame.layout.name, samples=samples)
        trimmed.sample_rate = frame.sample_rate
        trimmed.time_base = frame.time_base
        offset = Fraction(skip, frame.sample_rate) / frame.time_base
        if offset.denominator != 1:
            raise IncompleteObservation(
                "audio", "skip boundary cannot be represented at source timestamp precision"
            )
        trimmed.pts = frame.pts + int(offset)
        channels = 1 if frame.format.is_planar else len(frame.layout.channels)
        stride = frame.format.bytes * channels
        for source, target in zip(frame.planes, trimmed.planes):
            data = bytes(source)[skip * stride : (skip + samples) * stride]
            target.update(data + b"\x00" * (target.buffer_size - len(data)))
        return trimmed
