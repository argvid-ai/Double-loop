"""Parameterized file rendering; frame-exact output and source-bound receipts."""

from pathlib import Path

from PIL import Image, ImageDraw

from .media import _publish_materialized, ffmpeg, file_hash, inspect_video
from .media_storage import render_workspace


def check_recipe_output(path, recipe, *, check_active=None):
    facts = inspect_video(path, check_active=check_active)
    expected = recipe.duration_ms * recipe.fps // 1000
    if (facts["width"], facts["height"], facts["frames"]) != (
        recipe.width,
        recipe.height,
        expected,
    ) or (
        facts["audio_streams"] != 0
        if recipe.audio == "mute"
        else facts["audio_streams"] not in (0, 1)
    ):
        raise ValueError("output dimensions, decoded frame count or audio differ from recipe")
    if (
        facts["codec"] != "h264"
        or abs(facts["fps"] - recipe.fps) > 0.01
        or abs(facts["duration_ms"] - recipe.duration_ms) > 1000 / recipe.fps
    ):
        raise ValueError("output codec, rate or duration differ from recipe")
    return facts


def render_recipe(inputs, recipe, destination, *, check_active=None, threads=2):
    """inputs contain resolved paths + verified metadata; never model-supplied paths."""
    if len(inputs) != len(recipe.segments):
        raise ValueError("recipe input count mismatch")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    for item, segment in zip(inputs, recipe.segments):
        facts = item["facts"]
        if file_hash(Path(item["path"]), check_active=check_active) != facts["sha256"]:
            raise ValueError("registered source bytes changed")
        if segment.start_ms + segment.duration_ms > facts["duration_ms"]:
            raise ValueError("segment exceeds source duration")
        if segment.crop:
            x, y, w, h = segment.crop
            if x + w > facts.get("display_width", facts["width"]) or y + h > facts.get(
                "display_height", facts["height"]
            ):
                raise ValueError("crop exceeds source dimensions")
    with render_workspace(destination.parent) as work:
        parts = []
        preserve = recipe.audio == "preserve" and any(
            item["facts"].get("audio_streams", 0) > 0 for item in inputs
        )
        watermark = work / "label.png"
        if any(item.get("simulated") is not False for item in inputs):
            label = Image.new("RGBA", (min(recipe.width, 460), 28), (0, 0, 0, 190))
            ImageDraw.Draw(label).text((5, 7), "FILE REPLAY - NOT LIVE CAPTURE", fill="white")
            label.save(watermark)
        for index, (item, segment) in enumerate(zip(inputs, recipe.segments)):
            if check_active:
                check_active()
            path = work / f"part-{index}.mkv"
            filters = []
            if segment.crop:
                x, y, w, h = segment.crop
                filters.append(f"crop={w}:{h}:{x}:{y}")
            filters += [
                f"scale={recipe.width}:{recipe.height}:force_original_aspect_ratio=decrease",
                f"pad={recipe.width}:{recipe.height}:(ow-iw)/2:(oh-ih)/2:color=0x172522",
                "setsar=1",
                "setpts=PTS-STARTPTS",
                f"fps={recipe.fps}:eof_action=pass",
            ]
            marked = item.get("simulated") is not False
            graph = "[0:v]" + ",".join(filters)
            graph += (
                "[base];[base][1:v]overlay=x=(main_w-overlay_w)/2:y=main_h-34:format=auto[out]"
                if marked
                else "[out]"
            )
            duration = segment.duration_ms / 1000
            command = [
                "-y",
                "-threads",
                str(threads),
                "-ss",
                str(segment.start_ms / 1000),
                "-i",
                str(item["path"]),
            ]
            if marked:
                command += ["-loop", "1", "-framerate", str(recipe.fps), "-i", str(watermark)]
            audio = []
            if preserve:
                if item["facts"].get("audio_streams", 0):
                    audio_input = "0:a:0"
                else:
                    command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
                    audio_input = "2:a:0" if marked else "1:a:0"
                graph += f";[{audio_input}]atrim=duration={duration},asetpts=PTS-STARTPTS,aresample=48000,aformat=channel_layouts=stereo,apad=whole_dur={duration}[sound]"
                audio = ["-map", "[sound]", "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2"]
            else:
                audio = ["-an"]
            command += [
                "-filter_complex_threads",
                str(threads),
                "-filter_complex",
                graph,
                "-map",
                "[out]",
                *audio,
                "-frames:v",
                str(segment.duration_ms * recipe.fps // 1000),
                "-t",
                str(duration),
                "-c:v",
                "libx264",
                "-threads",
                str(threads),
                "-preset",
                "fast",
                "-crf",
                "22",
                "-r",
                str(recipe.fps),
                "-fps_mode",
                "cfr",
                "-enc_time_base",
                f"1:{recipe.fps}",
                "-pix_fmt",
                "yuv420p",
                str(path),
            ]
            ffmpeg(command, check_active=check_active)
            parts.append(path)
        listfile = work / "parts.txt"
        listfile.write_text(
            "".join(
                f"file '{p.name}'\nduration {segment.duration_ms / 1000}\n"
                for p, segment in zip(parts, recipe.segments)
            )
        )
        temporary = work / "output.mp4"
        audio = (
            [
                "-map",
                "0:a:0",
                "-af",
                f"atrim=duration={recipe.duration_ms / 1000},asetpts=PTS-STARTPTS",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
            ]
            if preserve
            else ["-an"]
        )
        ffmpeg(
            [
                "-y",
                "-f",
                "concat",
                "-safe",
                "1",
                "-i",
                str(listfile),
                "-map",
                "0:v",
                *audio,
                "-c:v",
                "copy",
                "-movflags",
                "+faststart",
                str(temporary),
            ],
            check_active=check_active,
        )
        facts = check_recipe_output(temporary, recipe, check_active=check_active)
        if facts["audio_streams"] != int(preserve):
            raise ValueError("expected source audio was not preserved")
        facts["audio_policy"] = recipe.audio
        facts["silent_audio_segments"] = [
            i
            for i, item in enumerate(inputs)
            if preserve and not item["facts"].get("audio_streams", 0)
        ]
        _publish_materialized(temporary, destination, check_active=check_active)
        return facts
