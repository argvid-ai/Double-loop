"""Real file processing. FFmpeg executes; PyAV reads FFmpeg's decoded stream facts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import av
import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

from .contracts import MediaFacts

SOURCE_BOUND_RENDER = "cix.render/verified-inputs-v1"


def _publish_materialized(temporary: Path, destination: Path, *, check_active=None):
    # Shape checks cannot bind old bytes to the current source recipe. Keep
    # replaced bytes for review; a failed generation never removes the old file.
    if check_active:
        check_active()
    if destination.exists():
        previous = destination.with_name(
            destination.stem + ".previous-" + uuid.uuid4().hex + destination.suffix
        )
        if check_active is None:
            shutil.copy2(destination, previous)
        else:
            pending = previous.with_suffix(".partial" + previous.suffix)
            with destination.open("rb") as source, pending.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    check_active()
                    output.write(chunk)
            shutil.copystat(destination, pending)
            check_active()
            pending.replace(previous)
    if check_active:
        check_active()
    temporary.replace(destination)


def file_hash(path: Path, *, check_active=None):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            if check_active:
                check_active()
            h.update(chunk)
    return h.hexdigest()


def inspect_video(path: Path, *, check_active=None) -> dict:
    if check_active:
        check_active()
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        rate = float(stream.average_rate or stream.guessed_rate or 0)
        if rate <= 0:
            raise ValueError("video has no usable frame rate")
        count = 0
        first, last = None, None
        for frame in container.decode(stream):
            if check_active:
                check_active()
            count += 1
            if frame.time is not None:
                first = frame.time if first is None else first
                last = frame.time
        if not count:
            raise ValueError("video has no decoded frames")
        duration = (last - first + 1 / rate) if last is not None else count / rate
        return {
            "width": stream.width,
            "height": stream.height,
            "fps": rate,
            "duration_ms": round(duration * 1000),
            "frames": count,
            "codec": stream.codec_context.name,
            "size_bytes": path.stat().st_size,
            "sha256": file_hash(path, check_active=check_active),
            "audio_streams": len(container.streams.audio),
            "probe": "PyAV 16.0.1 / FFmpeg libraries; full decode",
        }


def media_facts(path: Path, asset_id: str, side: str, *, check_active=None) -> MediaFacts:
    facts = inspect_video(path, check_active=check_active)
    return MediaFacts(
        asset_id=asset_id,
        uri=f"{side}-asset://{asset_id}",
        **{k: v for k, v in facts.items() if k not in {"audio_streams", "probe"}},
    )


def check_clip(path: Path, seconds: int, *, check_active=None):
    facts = inspect_video(path, check_active=check_active)
    if (facts["width"], facts["height"], facts["frames"], facts["audio_streams"]) != (
        1080,
        1920,
        seconds * 30,
        0,
    ):
        raise ValueError("unexpected video dimensions/frame count/audio")
    if (
        abs(facts["duration_ms"] - seconds * 1000) > 34
        or abs(facts["fps"] - 30) > 0.01
        or facts["codec"] != "h264"
    ):
        raise ValueError("unexpected duration/frame rate/codec")
    return facts


def ffmpeg(args: list[str], *, check_active=None):
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        *args,
    ]
    if check_active is None:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    else:
        check_active()
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        deadline = time.monotonic() + 180
        try:
            while True:
                check_active()
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(command, 180)
                try:
                    stdout, stderr = process.communicate(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    pass
            check_active()
            result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=0.2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.communicate(timeout=0.2)
                    except subprocess.TimeoutExpired as exc:
                        raise RuntimeError(
                            f"FFmpeg process {process.pid} could not be reaped"
                        ) from exc
            raise
    if result.returncode:
        raise RuntimeError("FFmpeg processing failed: " + result.stderr[-1200:])


def make_clip(
    source: Path, destination: Path, *, start_s: float, crop: list[int] | None, check_active=None
):
    if check_active:
        check_active()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(source)) as container:
        stream = container.streams.video[0]
        duration = (
            float(stream.duration * stream.time_base)
            if stream.duration is not None
            else container.duration / av.time_base
            if container.duration is not None
            else None
        )
    if duration is None:
        duration = inspect_video(source, check_active=check_active)["duration_ms"] / 1000
    if start_s < 0 or start_s + 6 > duration + 0.001:
        raise ValueError("source does not contain the requested six-second interval")
    filters = []
    if crop is not None:
        x, y, width, height = crop
        if min(x, y) < 0 or min(width, height) <= 0:
            raise ValueError("invalid crop")
        filters.append(f"crop={width}:{height}:{x}:{y}")
    candidates = (
        [Path(os.environ["CIX_FONT_FILE"])]
        if os.environ.get("CIX_FONT_FILE")
        else [
            Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
            Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
            Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
        ]
    )
    font = next((p for p in candidates if p.is_file()), None)
    if font is None:
        raise RuntimeError("a local font is required for the replay watermark; set CIX_FONT_FILE")
    filters += [
        "scale=1080:1920:force_original_aspect_ratio=decrease",
        "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0x172522",
        "setsar=1",
        "fps=30:eof_action=pass",
        "setpts=PTS-STARTPTS",
    ]
    # ARM imageio-ffmpeg builds can lack drawtext/libharfbuzz. Rasterize the
    # literal provenance label with the already installed Pillow dependency;
    # FFmpeg's standard overlay filter then works on both deployed platforms.
    label = "STOCK REPLAY - NOT LIVE CAPTURE"
    face = ImageFont.truetype(str(font), 32)
    box = face.getbbox(label)
    watermark = destination.with_suffix(".watermark.png")
    overlay = Image.new("RGBA", (box[2] - box[0] + 24, box[3] - box[1] + 24), (0, 0, 0, 179))
    ImageDraw.Draw(overlay).text(
        (12 - box[0], 12 - box[1]), label, font=face, fill=(255, 255, 255, 255)
    )
    overlay.save(watermark)
    temporary = destination.with_suffix(".partial.mp4")
    graph = (
        "[0:v]"
        + ",".join(filters)
        + "[base];[base][1:v]overlay=x=(main_w-overlay_w)/2:y=main_h-70:format=auto[out]"
    )
    cancel_options = {"check_active": check_active} if check_active is not None else {}
    ffmpeg(
        [
            "-y",
            "-ss",
            str(start_s),
            "-i",
            str(source),
            "-loop",
            "1",
            "-framerate",
            "30",
            "-i",
            str(watermark),
            "-t",
            "6",
            "-an",
            "-filter_complex",
            graph,
            "-map",
            "[out]",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "22",
            "-r",
            "30",
            "-fps_mode",
            "cfr",
            "-enc_time_base",
            "1:30",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(temporary),
        ],
        **cancel_options,
    )
    facts = check_clip(temporary, 6, check_active=check_active)
    if check_active:
        check_active()
    _publish_materialized(temporary, destination, check_active=check_active)
    return facts


def render(wide: Path, close: Path, destination: Path, *, check_active=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    check_clip(wide, 6, check_active=check_active)
    check_clip(close, 6, check_active=check_active)
    temporary = destination.with_suffix(".partial.mp4")
    ffmpeg(
        [
            "-y",
            "-i",
            str(wide),
            "-i",
            str(close),
            "-filter_complex",
            "[0:v]setpts=PTS-STARTPTS[a];[1:v]setpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "22",
            "-r",
            "30",
            "-fps_mode",
            "cfr",
            "-enc_time_base",
            "1:30",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(temporary),
        ],
        **({"check_active": check_active} if check_active else {}),
    )
    facts = check_clip(temporary, 12, check_active=check_active)
    _publish_materialized(temporary, destination, check_active=check_active)
    return facts


def provenance(root: Path):
    return json.loads((root / "source" / "source.json").read_text())


def processor_versions():
    return {"ffmpeg": imageio_ffmpeg.get_ffmpeg_version(), "pyav": av.__version__}


def prepare_clips(root: Path):
    """Selection is frozen only after visual inspection; never invent semantic QC."""
    record = provenance(root)
    source = root / "source" / "cat-track.webm"
    if file_hash(source) != record["sha256"]:
        raise ValueError("source checksum changed")
    selection = json.loads((root / "source" / "selection.json").read_text())
    if (
        selection["source_sha256"] != record["sha256"]
        or selection["annotation_source"] != "manual_visual_review"
    ):
        raise ValueError("selection must match the visually reviewed source")
    output = {}
    for side, kind in (("cloud", "wide"), ("edge", "closeup")):
        path = root / side / "fixtures" / f"{kind}.mp4"
        selected = selection[kind]
        make_clip(source, path, start_s=selected["start_s"], crop=selected.get("crop"))
        output[kind] = {
            "asset": media_facts(path, f"fixture-{kind}", side).model_dump(mode="json"),
            "subject_id": selection["subject_id"],
            "selection": selected,
            "provenance": record,
            "annotation_source": "manual_visual_review",
            "simulated": True,
        }
        (path.parent / f"{kind}.json").write_text(
            json.dumps(output[kind], ensure_ascii=False, indent=2)
        )
        (path.parent / "ATTRIBUTION.txt").write_text(
            f"{record['author']} — Cat playing with a round track toy\n{record['source_page']}\n"
            f"{record['license']} — {record['license_url']}\nChanges: excerpt, crop/pad, resize, mute, watermark, encode.\n"
            "Replay only; not newly captured footage.\n"
        )
    return output
