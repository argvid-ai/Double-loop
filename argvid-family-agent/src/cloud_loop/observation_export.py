"""Preserve observation media without scaling, muting, or losing provenance.

This materializer publishes only ranges whose codec preroll/tail already exist
in the captured range. A boundary needing additional packets remains pending;
it is never silently extended into future footage or called a complete clip.
At an unavailable capture-head dependency or after stop, the caller can permit a verified boundary transcode:
unchanged decoded video pixels/timing plus high-bitrate, signal-checked AAC.
This fallback is recorded separately and is not encoded-packet preservation.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path

import av
import imageio_ffmpeg
import numpy as np

from .contracts import ID
from .media import file_hash
from .observation_media import _fingerprint


class MoreCapturedMediaRequired(ValueError):
    def __init__(self, actual_start_ms, actual_end_ms):
        self.actual_start_ms, self.actual_end_ms = actual_start_ms, actual_end_ms
        super().__init__("codec boundary extends outside the available captured range")


class EncodedCopyMismatch(ValueError):
    """Copy timestamp normalization cannot be proved against source packets."""


def _packet_time(packet, field):
    value = getattr(packet, field)
    if value is None:
        raise ValueError("media packet has no timestamp")
    return Fraction(value) * packet.time_base


@contextmanager
def _packets(path, kind, seek_seconds=None):
    with av.open(str(path)) as container:
        stream = next((s for s in container.streams if s.type == kind), None)
        if stream is None:
            raise ValueError("required media track missing")
        if seek_seconds is not None:
            video = container.streams.video[0]
            container.seek(int(seek_seconds / video.time_base), stream=video, backward=True)
        yield stream, (packet for packet in container.demux(stream) if packet.size)


def _verify_packets(source, output, start_seconds, kind, check):
    """Stream comparison is bounded in memory even for a long retained event."""
    with (
        _packets(source, kind, start_seconds) as (source_stream, originals),
        _packets(output, kind) as (target_stream, copies),
    ):
        original = next(originals, None)
        count = 0
        first = None
        last = None
        max_error = 0.0
        max_duration = 0.0
        aggregate = hashlib.sha256()
        for copied in copies:
            check()
            expected = _packet_time(copied, "dts") + start_seconds
            tolerance = max(
                Fraction(1, 1000), source_stream.time_base * 2, target_stream.time_base * 2
            )
            while (
                count == 0
                and original is not None
                and _packet_time(original, "dts") < expected - tolerance
            ):
                original = next(originals, None)
            if original is None:
                raise ValueError("output packet has no source counterpart")
            for name in ("pts", "dts"):
                error = abs(
                    _packet_time(original, name) - (_packet_time(copied, name) + start_seconds)
                )
                if error > tolerance:
                    raise EncodedCopyMismatch("output packet time differs from original")
                max_error = max(max_error, float(error))
            original_bytes = bytes(original)
            if original_bytes != bytes(copied):
                raise EncodedCopyMismatch("output packet bytes differ from original")
            if not original.duration or original.duration <= 0:
                raise ValueError("cannot establish source packet coverage")
            max_duration = max(
                max_duration, float(Fraction(original.duration) * original.time_base)
            )
            begin = _packet_time(original, "pts")
            end = begin + Fraction(original.duration) * original.time_base
            valid_start = Fraction(source_stream.start_time or 0) * source_stream.time_base
            valid_end = (
                Fraction((source_stream.start_time or 0) + source_stream.duration)
                * source_stream.time_base
                if source_stream.duration is not None
                else end
            )
            begin = max(begin, valid_start)
            end = min(end, valid_end)
            if end > begin:
                first = begin if first is None else min(first, begin)
                last = end if last is None else max(last, end)
            aggregate.update(len(original_bytes).to_bytes(8, "big"))
            aggregate.update(original_bytes)
            count += 1
            original = next(originals, None)
        if not count or first is None:
            raise ValueError("empty copied media track")
        if source_stream.codec_context.name != target_stream.codec_context.name:
            raise ValueError("retained codec changed")
        return {
            "packet_count": count,
            "packet_bytes_identical": True,
            "packet_sequence_sha256": aggregate.hexdigest(),
            "max_timestamp_error_seconds": max_error,
            "source_start_ms": float(first * 1000),
            "source_end_ms": float(last * 1000),
            "max_source_packet_duration_ms": max_duration * 1000,
            "codec": target_stream.codec_context.name,
        }


def _display_matrix(frame):
    return next(
        (bytes(side).hex() for side in frame.side_data if side.type.name == "DISPLAYMATRIX"), None
    )


def _verify_audio_samples(path, requested_seconds, check):
    """Packet duration can span a silent hole; count actual decoded samples."""
    cursor = Fraction(0)
    frames = 0
    previous_end = None
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        stream_start = Fraction(stream.start_time or 0) * stream.time_base
        stream_end = (
            Fraction((stream.start_time or 0) + stream.duration) * stream.time_base
            if stream.duration is not None
            else None
        )
        for frame in container.decode(stream):
            check()
            if frame.pts is None or frame.is_corrupt:
                raise ValueError("invalid retained audio frame")
            begin = Fraction(frame.pts) * frame.time_base
            samples = frame.samples
            if frame.duration:
                samples = min(
                    samples, int(Fraction(frame.duration) * frame.time_base * frame.sample_rate)
                )
            finish = begin + Fraction(samples, frame.sample_rate)
            begin = max(begin, stream_start)
            if stream_end is not None:
                finish = min(finish, stream_end)
            if finish <= begin:
                continue
            tolerance = Fraction(1, frame.sample_rate)
            if previous_end is not None and begin < previous_end - tolerance:
                raise ValueError("retained audio sample timeline overlaps")
            previous_end = finish
            frames += 1
            low, high = max(begin, Fraction(0)), min(finish, requested_seconds)
            if high > low:
                if low > cursor + tolerance:
                    raise ValueError("retained audio has an internal sample gap")
                cursor = max(cursor, high)
        if not frames or cursor < requested_seconds - Fraction(1, stream.codec_context.sample_rate):
            raise ValueError("retained audio does not cover requested event")
    return {
        "decoded_frames": frames,
        "contiguous_sample_coverage_seconds": float(cursor),
        "silence_synthesized": False,
    }


def _facts(path, *, tail=False, check=lambda: None):
    with av.open(str(path)) as container:
        if len(container.streams.video) != 1 or len(container.streams.audio) != 1:
            raise ValueError("retention requires one video track and one audio track")
        video = container.streams.video[0]
        audio = container.streams.audio[0]
        if tail and video.duration is not None:
            end = Fraction((video.start_time or 0) + video.duration) * video.time_base
            container.seek(
                int(max(Fraction(0), end - 2) / video.time_base), stream=video, backward=True
            )
        first = last = None
        for frame in container.decode(video):
            check()
            if frame.is_corrupt:
                raise ValueError("corrupt retained video frame")
            if first is None:
                first = frame
            last = frame
            if not tail:
                break
        if first is None:
            raise ValueError("retained video cannot be decoded")
        return {
            "width": video.codec_context.width,
            "height": video.codec_context.height,
            "pixel_format": first.format.name,
            "rotation": first.rotation,
            "display_matrix": _display_matrix(first),
            "video_codec": video.codec_context.name,
            "audio_codec": audio.codec_context.name,
            "audio_rate": audio.codec_context.sample_rate,
            "audio_channels": audio.codec_context.channels,
            "first_video_seconds": float(Fraction(first.pts) * first.time_base),
            "last_video_end_seconds": float(
                Fraction(last.pts + (last.duration or 0)) * last.time_base
            ),
        }


def _presentation_frames(container, stream, check):
    """VFR display intervals follow adjacent presentation timestamps.

    With reordered frames, a decoded frame's duration can inherit a decode-order
    packet duration. It need not equal the interval until the next shown frame.
    """
    previous = None
    begin = None
    for frame in container.decode(stream):
        check()
        if frame.pts is None or frame.is_corrupt:
            raise ValueError("invalid source presentation frame")
        when = Fraction(frame.pts) * frame.time_base
        if previous is not None:
            if when <= begin:
                raise ValueError("source presentation timestamps do not increase")
            yield previous, begin, when
        previous, begin = frame, when
    if previous is not None:
        end = (
            Fraction((stream.start_time or 0) + stream.duration) * stream.time_base
            if stream.duration is not None
            else begin + Fraction(previous.duration or 0) * previous.time_base
        )
        if end <= begin:
            raise ValueError("source final display interval is unavailable")
        yield previous, begin, end


def _boundary_starts(source, start, available_start, check):
    with av.open(str(source)) as container:
        video = container.streams.video[0]
        container.seek(int(start / video.time_base), stream=video, backward=True)
        for frame, begin, finish in _presentation_frames(container, video, check):
            if begin <= start < finish:
                origin = Fraction(int(begin * 1000000), 1000000)
                # A frame already on screen at capture start contributes only
                # its remaining display interval, not pre-capture footage.
                return origin, max(origin, available_start)
            if begin > start:
                break
    raise ValueError("source video does not cover event start")


def _boundary_video(source, output, start, end, check, *, decoder_start=None):
    """Compare every published frame to its captured source frame, in order."""
    with av.open(str(source)) as original, av.open(str(output)) as target:
        stream = original.streams.video[0]
        seek = start if decoder_start is None else decoder_start
        original.seek(int(seek / stream.time_base), stream=stream, backward=True)

        def selected():
            for frame, when, finish in _presentation_frames(original, stream, check):
                if when >= end:
                    break
                if finish > start:
                    yield frame, when, finish

        frames = selected()
        count = 0
        first = last = None
        aggregate = hashlib.sha256()
        previous = None
        for copied, copied_begin, copied_finish in _presentation_frames(
            target, target.streams.video[0], check
        ):
            check()
            item = next(frames, None)
            if item is None or copied.is_corrupt:
                raise ValueError("boundary export has missing or corrupt video frames")
            frame, original_begin, original_finish = item
            begin = max(start, original_begin)
            finish = min(end, original_finish)
            if previous is not None and begin > previous + Fraction(1, 1000000):
                raise ValueError("boundary source video has an uncovered interval")
            if abs(begin - (copied_begin + start)) > Fraction(1, 1000000):
                raise ValueError("boundary export changed video frame timing")
            if abs(finish - (copied_finish + start)) > Fraction(1, 1000000):
                raise ValueError("boundary export exceeds captured frame interval")
            if frame.format.name != copied.format.name or not np.array_equal(
                frame.to_ndarray(), copied.to_ndarray()
            ):
                raise ValueError("boundary export changed decoded video pixels")
            aggregate.update(frame.to_ndarray().tobytes())
            count += 1
            first = begin if first is None else first
            last = finish
            previous = finish
        if next(frames, None) is not None or not count or last < end - Fraction(1, 1000000):
            raise ValueError("boundary export did not preserve all selected video frames")
        return {
            "decoded_frames": count,
            "decoded_pixels_identical": True,
            "decoded_sequence_sha256": aggregate.hexdigest(),
            "source_start_ms": float(first * 1000),
            "source_end_ms": float(last * 1000),
            "packet_bytes_identical": False,
        }


def _audio_chunks(path, start, end, check):
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        rate = stream.codec_context.sample_rate
        low, high = round(start * rate), round(end * rate)
        cursor = low
        # Seeking to zero can skip the negative AAC priming packet. Start-of-
        # file verification must decode normally so it matches actual playback.
        if start > 1:
            container.seek(int((start - 1) / stream.time_base), stream=stream, backward=True)
        for frame in container.decode(audio=0):
            check()
            if frame.pts is None or frame.is_corrupt or frame.format.name != "fltp":
                raise ValueError("boundary audio requires valid decoded AAC float samples")
            begin = round(Fraction(frame.pts) * frame.time_base * rate)
            length = frame.samples
            if frame.duration:
                length = min(length, round(Fraction(frame.duration) * frame.time_base * rate))
            if stream.duration is not None:
                length = min(
                    length,
                    round(
                        Fraction((stream.start_time or 0) + stream.duration)
                        * stream.time_base
                        * rate
                    )
                    - begin,
                )
            left, right = max(begin, low), min(begin + length, high)
            if right <= left:
                continue
            if left != cursor:
                raise ValueError("boundary audio samples have a gap or overlap")
            yield frame.to_ndarray()[:, left - begin : right - begin]
            cursor = right
            if cursor >= high:
                break
        if cursor < high:
            raise ValueError("boundary audio samples end before selected interval")


def _boundary_audio(source, output, start, end, check):
    """AAC is re-encoded, not claimed lossless; verify alignment and signal error."""
    originals = iter(_audio_chunks(source, start, end, check))
    copies = iter(_audio_chunks(output, Fraction(0), end - start, check))
    a = next(originals, None)
    b = next(copies, None)
    count = 0
    energy = error = 0.0
    while a is not None and b is not None:
        check()
        n = min(a.shape[1], b.shape[1])
        if a.shape[0] != b.shape[0]:
            raise ValueError("boundary audio channels changed")
        x = a[:, :n].astype(np.float64)
        y = b[:, :n].astype(np.float64)
        energy += float(np.sum(x * x))
        error += float(np.sum((x - y) ** 2))
        count += x.size
        a = a[:, n:] if n < a.shape[1] else next(originals, None)
        b = b[:, n:] if n < b.shape[1] else next(copies, None)
    if a is not None or b is not None or not count:
        raise ValueError("boundary audio sample count changed")
    relative = (error / max(energy, 1e-30)) ** 0.5
    absolute = (error / count) ** 0.5
    if absolute > 1e-5 and relative > 0.08:
        raise ValueError("boundary AAC signal error exceeds verification bound")
    return {
        "compared_samples_all_channels": count,
        "relative_rms_error": relative,
        "absolute_rms_error": absolute,
        "sample_alignment_verified": True,
        "decoded_samples_identical": False,
        "silence_synthesized": False,
        "codec": "aac",
        "verification_limit_relative_rms": 0.08,
    }


def copy_observation_span(
    source: Path,
    root: Path,
    relative_path: str,
    *,
    start_ms: int,
    end_ms: int,
    available_start_ms: int,
    available_end_ms: int,
    source_ref: str,
    simulated: bool,
    max_output_bytes: int,
    min_free_bytes: int,
    timeout_seconds=180,
    check_active=lambda: None,
    on_staging=None,
    on_verified=None,
    publish=None,
    allow_boundary_transcode=False,
    allow_capture_head_transcode=False,
):
    """Real source-copy materialization, independent of the keep/discard decision.

    The caller obtains the available/protected range from its authoritative
    index. The function never deletes originals or an existing final output.
    Publication is exclusive: retries must inspect an existing recorded result.
    """
    if (
        any(
            type(value) is not int
            for value in (
                start_ms,
                end_ms,
                available_start_ms,
                available_end_ms,
                max_output_bytes,
                min_free_bytes,
            )
        )
        or not 0 <= available_start_ms <= start_ms < end_ms <= available_end_ms
        or max_output_bytes <= 0
        or min_free_bytes < 0
        or type(simulated) is not bool
    ):
        raise ValueError("invalid retained span or storage budget")
    if type(timeout_seconds) not in {int, float} or not 0 < timeout_seconds <= 86400:
        raise ValueError("invalid materialization deadline")
    if type(allow_boundary_transcode) is not bool:
        raise ValueError("invalid boundary fallback flag")
    if type(allow_capture_head_transcode) is not bool:
        raise ValueError("invalid capture-head fallback flag")
    if not isinstance(source_ref, str) or not re.fullmatch(ID, source_ref):
        raise ValueError("invalid source reference")
    source = Path(source).resolve(strict=True)
    root = Path(root).resolve()
    if not source.is_file():
        raise ValueError("source must be a registered regular file")
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts or relative.suffix != ".mp4":
        raise ValueError("retained output must be a project-relative mp4")
    root.mkdir(parents=True, exist_ok=True)
    destination = (root / relative).resolve()
    if not destination.is_relative_to(root) or destination == source:
        raise ValueError("retained path escapes storage binding")
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    before = _fingerprint(source)
    source_facts = _facts(source, check=check_active)
    if source_facts["video_codec"] not in {"h264", "hevc"} or source_facts["audio_codec"] != "aac":
        raise ValueError("source-copy path currently requires H.264/HEVC with AAC")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=".retaining-", dir=root) as temporary:
        folder = Path(temporary)
        output = folder / "output.mp4"
        if on_staging:
            on_staging(folder)

        def check():
            check_active()
            if not destination.parent.resolve().is_relative_to(root):
                raise ValueError("storage directory changed")
            if _fingerprint(source) != before:
                raise ValueError("source changed during materialization")
            if output.exists() and output.stat().st_size > max_output_bytes:
                raise ValueError("retained file exceeds reserved bytes")
            if shutil.disk_usage(root).free < min_free_bytes:
                raise ValueError("minimum free space reached")
            if time.monotonic() - started > timeout_seconds:
                raise TimeoutError("retention materialization timed out")

        check()
        if shutil.disk_usage(root).free < max_output_bytes + min_free_bytes:
            raise ValueError("reserved output does not fit while preserving free-space floor")
        command = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-n",
            "-ss",
            str(start_ms / 1000),
            "-i",
            str(source),
            "-t",
            str((end_ms - start_ms) / 1000),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-fs",
            str(max_output_bytes),
            "-movflags",
            "+faststart",
            str(output),
        ]

        def execute(command):
            with (folder / "encoder.log").open("ab") as log:
                process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=log)
                try:
                    while process.poll() is None:
                        check()
                        time.sleep(0.025)
                    check()
                    if process.returncode:
                        raise ValueError("retention encoder or muxer failed")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()

        execute(command)
        head = _facts(output, check=check)
        tail = _facts(output, tail=True, check=check)
        for field in (
            "width",
            "height",
            "pixel_format",
            "rotation",
            "display_matrix",
            "video_codec",
            "audio_codec",
            "audio_rate",
            "audio_channels",
        ):
            if head[field] != source_facts[field]:
                raise ValueError("retained media specification differs from source: " + field)
        copy_issue = None
        try:
            tracks = {
                kind: _verify_packets(source, output, Fraction(start_ms, 1000), kind, check)
                for kind in ("video", "audio")
            }
        except EncodedCopyMismatch as exc:
            if not (
                allow_boundary_transcode
                or (allow_capture_head_transcode and start_ms == available_start_ms)
            ):
                raise
            copy_issue = str(exc)
            tracks = {}
        earliest = min((track["source_start_ms"] for track in tracks.values()), default=None)
        latest = max((track["source_end_ms"] for track in tracks.values()), default=None)
        origin = Fraction(start_ms, 1000)
        boundary = None
        if copy_issue or earliest < available_start_ms - 0.01 or latest > available_end_ms + 0.01:
            missing_head = (earliest is not None and earliest < available_start_ms - 0.01) or (
                copy_issue and start_ms == available_start_ms
            )
            if not (allow_boundary_transcode or (allow_capture_head_transcode and missing_head)):
                raise MoreCapturedMediaRequired(earliest, latest)
            if source_facts["pixel_format"] != "yuv420p":
                raise ValueError("boundary fallback currently requires yuv420p")
            boundary = {
                "copy_packet_start_ms": earliest,
                "copy_packet_end_ms": latest,
                "reason": "stopped_source_codec_boundary"
                if allow_boundary_transcode
                else "capture_start_codec_boundary",
                "copy_verification_error": copy_issue,
                "source_specification": source_facts,
            }
            output.unlink()
            decoder_origin, origin = _boundary_starts(
                source, Fraction(start_ms, 1000), Fraction(available_start_ms, 1000), check
            )
            duration = float(Fraction(end_ms, 1000) - origin)
            seek = max(0, float(origin) - 1)
            clipping = []
            if decoder_origin < origin:
                shift = round((float(origin) - seek) * 1000000)
                finish = end_ms / 1000 - seek
                timing = [
                    "-ss",
                    str(seek),
                    "-t",
                    str(finish + 1),
                    "-noautorotate",
                    "-i",
                    str(source),
                ]
                clipping = [
                    "-vf",
                    f"settb=1/1000000,trim=start={float(decoder_origin) - seek}:end={finish},setpts=max(0\\,PTS-{shift})",
                    "-af",
                    f"atrim=start={float(origin) - seek}:end={finish},asetpts=PTS-STARTPTS",
                ]
            else:
                timing = [
                    "-ss",
                    str(seek),
                    "-noautorotate",
                    "-i",
                    str(source),
                    "-ss",
                    str(float(origin) - seek),
                    "-t",
                    str(duration),
                ]
            # settb/trim may leave frame.duration unset. MP4 derives interior
            # durations from DTS spacing; give the final packet its exact
            # remaining display interval rather than a zero-length last frame.
            packet_duration = (
                f"max(0\\,{duration}/TB-PTS)"
                if clipping
                else f"min(DURATION\\,max(0\\,{duration}/TB-PTS))"
            )
            execute(
                [
                    imageio_ffmpeg.get_ffmpeg_exe(),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-nostdin",
                    "-n",
                    *timing,
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    *clipping,
                    "-c:v",
                    "libx264",
                    "-threads",
                    "2",
                    "-preset",
                    "fast",
                    "-crf",
                    "0",
                    "-fps_mode",
                    "passthrough",
                    "-enc_time_base:v",
                    "1:1000000",
                    "-bsf:v",
                    "setts=duration=" + packet_duration,
                    "-c:a",
                    "aac",
                    "-b:a",
                    "320k",
                    "-fs",
                    str(max_output_bytes),
                    "-movflags",
                    "+faststart",
                    str(output),
                ]
            )
            head = _facts(output, check=check)
            tail = _facts(output, tail=True, check=check)
            for field in (
                "width",
                "height",
                "pixel_format",
                "rotation",
                "display_matrix",
                "audio_rate",
                "audio_channels",
            ):
                if head[field] != source_facts[field]:
                    raise ValueError("boundary media specification changed: " + field)
            begin, finish = origin, Fraction(end_ms, 1000)
            boundary["video"] = _boundary_video(
                source, output, begin, finish, check, decoder_start=decoder_origin
            )
            boundary["decoder_start_ms"] = float(decoder_origin * 1000)
            boundary["first_frame_display_clipped"] = decoder_origin < origin
            boundary["audio"] = _boundary_audio(source, output, begin, finish, check)
            # Source-copy receipts belong to the rejected temporary file, not
            # the replacement. Never advertise encoded-packet identity here.
            tracks = {
                "video": {**boundary["video"], "codec": head["video_codec"]},
                "audio": {
                    **boundary["audio"],
                    "packet_bytes_identical": False,
                    "source_start_ms": float(origin * 1000),
                    "source_end_ms": end_ms,
                },
            }
            earliest = float(origin * 1000)
            latest = end_ms
            head["first_video_seconds"] = (
                tracks["video"]["source_start_ms"] - float(origin * 1000)
            ) / 1000
            tail["last_video_end_seconds"] = (
                tracks["video"]["source_end_ms"] - float(origin * 1000)
            ) / 1000
        first_visible = float(origin * 1000) + head["first_video_seconds"] * 1000
        last_visible = float(origin * 1000) + tail["last_video_end_seconds"] * 1000
        tolerance = 0.001 if boundary else tracks["video"]["max_source_packet_duration_ms"] + 1
        if first_visible > start_ms + tolerance or last_visible < end_ms - tolerance:
            raise ValueError("retained video does not cover requested event")
        if (
            tracks["audio"]["source_start_ms"] > start_ms + 1
            or tracks["audio"]["source_end_ms"] < end_ms - 1
        ):
            raise ValueError("retained audio does not cover requested event")
        audio_coverage = _verify_audio_samples(output, Fraction(end_ms, 1000) - origin, check)
        info = {
            "schema": "cix.retained-media/1",
            "source_ref": source_ref,
            "source_fingerprint": list(before),
            "requested_start_ms": start_ms,
            "requested_end_ms": end_ms,
            "output_timeline_origin_ms": float(origin * 1000),
            "available_start_ms": available_start_ms,
            "available_end_ms": available_end_ms,
            "retained_packet_start_ms": earliest,
            "retained_packet_end_ms": latest,
            "visible_video_start_ms": first_visible,
            "visible_video_end_ms": last_visible,
            "tracks": tracks,
            "audio_sample_coverage": audio_coverage,
            "specification": {
                key: value
                for key, value in head.items()
                if key not in {"first_video_seconds", "last_video_end_seconds"}
            },
            "output_video_start_seconds": head["first_video_seconds"],
            "output_video_end_seconds": tail["last_video_end_seconds"],
            "simulated": simulated,
            "file_bytes_real": True,
            "materialization": "verified_boundary_transcode" if boundary else "encoded_source_copy",
            "full_video_decode": bool(boundary),
            "verification": (
                "all selected video pixels/timestamps matched; AAC sample alignment and signal error verified"
                if boundary
                else "all encoded packets/timestamps matched to source; head/tail video and all audio decoded"
            ),
            "bytes": output.stat().st_size,
            "sha256": file_hash(output, check_active=check),
            "file_fingerprint": list(_fingerprint(output)),
            "elapsed_seconds": time.monotonic() - started,
            "relative_path": str(relative),
        }
        if boundary:
            info["boundary_transcode"] = boundary
        check()
        with output.open("rb") as stream:
            os.fsync(stream.fileno())
        if on_verified:
            on_verified(info)
        check()
        if publish:
            publish(output, destination, info)
        else:
            os.link(output, destination)  # Atomic no-clobber publication on the same filesystem.
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return info
