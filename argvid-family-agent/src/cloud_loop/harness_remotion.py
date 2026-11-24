"""Trusted Remotion composition behind the existing versioned media recipe."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

from .harness_media_render import check_recipe_output
from .media import _publish_materialized, file_hash


COMPOSITION = """
import React from 'react';
import {AbsoluteFill, Composition, OffthreadVideo, Sequence, registerRoot, staticFile} from 'remotion';
const Edit = ({segments, fps, audio, simulated}) => {
  let cursor = 0;
  return <AbsoluteFill style={{backgroundColor:'#172522'}}>
    {segments.map((s, i) => {
      const from = cursor; cursor += s.frames;
      return <Sequence key={i} from={from} durationInFrames={s.frames}>
        <OffthreadVideo src={staticFile(s.file)} trimBefore={s.start_ms * fps / 1000}
          muted={audio === 'mute'} style={{width:'100%',height:'100%',objectFit:'contain'}} />
      </Sequence>;
    })}
    {simulated && <div style={{position:'absolute',bottom:6,left:5,padding:'5px',color:'white',background:'#000a',fontSize:10}}>
      FILE REPLAY - NOT LIVE CAPTURE
    </div>}
  </AbsoluteFill>;
};
const Root = () => <Composition id="HarnessEdit" component={Edit} width={360} height={640}
  fps={25} durationInFrames={150}
  calculateMetadata={({props}) => ({width:props.width,height:props.height,fps:props.fps,
    durationInFrames:props.segments.reduce((n,s)=>n+s.frames,0)})} />;
registerRoot(Root);
"""


def render_remotion(inputs, recipe, destination, *, settings, check_active=None):
    runtime = Path(settings.remotion_runtime_dir).resolve()
    cli = runtime / "node_modules/@remotion/cli/remotion-cli.js"
    package = runtime / "node_modules/remotion/package.json"
    node = shutil.which("node")
    browser = settings.remotion_browser_executable
    if (
        not node
        or not cli.is_file()
        or not package.is_file()
        or not browser
        or not Path(browser).is_file()
    ):
        raise ValueError("configured Remotion runtime/browser is unavailable")
    version = json.loads(package.read_text())["version"]
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    def check():
        if check_active:
            check_active()

    check()
    with tempfile.TemporaryDirectory(prefix="remotion-", dir=destination.parent) as folder:
        work = Path(folder)
        (work / "public").mkdir()
        (work / "node_modules").symlink_to(runtime / "node_modules", target_is_directory=True)
        (work / "package.json").write_text('{"name":"harness-edit","private":true}')
        props = {
            "width": recipe.width,
            "height": recipe.height,
            "fps": recipe.fps,
            "audio": recipe.audio,
            "simulated": any(item.get("simulated") is not False for item in inputs),
            "segments": [],
        }
        if len(inputs) != len(recipe.segments):
            raise ValueError("recipe input count mismatch")
        for i, (item, segment) in enumerate(zip(inputs, recipe.segments)):
            check()
            if (
                segment.crop is not None
                or segment.start_ms + segment.duration_ms > item["facts"]["duration_ms"]
            ):
                raise ValueError("unsupported crop or range beyond input")
            target = work / "public" / f"input-{i}.mp4"
            shutil.copyfile(item["path"], target)
            if file_hash(target, check_active=check) != item["facts"]["sha256"]:
                raise ValueError("registered source bytes changed")
            props["segments"].append(
                {
                    "file": target.name,
                    "start_ms": segment.start_ms,
                    "frames": segment.duration_ms * recipe.fps // 1000,
                }
            )
        (work / "entry.tsx").write_text(COMPOSITION)
        (work / "props.json").write_text(json.dumps(props))
        output = work / "output.mp4"
        command = [
            node,
            str(cli),
            "render",
            str(work / "entry.tsx"),
            "HarnessEdit",
            str(output),
            "--props=" + str(work / "props.json"),
            "--codec=h264",
            "--audio-codec=aac",
            "--pixel-format=yuv420p",
            "--concurrency=1",
            "--browser-executable=" + browser,
        ]
        if recipe.audio == "mute":
            command.append("--muted")
        started = time.monotonic()
        with (work / "render.log").open("w+") as log:
            process = subprocess.Popen(
                command,
                cwd=work,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                env={
                    k: v
                    for k, v in os.environ.items()
                    if k in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}
                },
            )
            try:
                while process.poll() is None:
                    check()
                    if time.monotonic() - started > 180:
                        raise TimeoutError("Remotion render exceeded 180 seconds")
                    time.sleep(0.5)
                check()
                if process.returncode:
                    log.seek(0)
                    raise RuntimeError("Remotion render failed: " + log.read()[-1800:])
            except BaseException:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=3)
                raise
        facts = check_recipe_output(output, recipe, check_active=check)
        preserve = recipe.audio == "preserve" and any(
            i["facts"].get("audio_streams", 0) for i in inputs
        )
        if facts["audio_streams"] != int(preserve):
            raise ValueError("expected source audio was not preserved")
        facts.update(
            audio_policy=recipe.audio,
            silent_audio_segments=[
                i
                for i, item in enumerate(inputs)
                if preserve and not item["facts"].get("audio_streams", 0)
            ],
            renderer={
                "name": "remotion",
                "version": version,
                "seconds": time.monotonic() - started,
                "composition": "trusted_recipe_template",
                "concurrency": 1,
                "returncode": process.returncode,
                "command": command,
            },
        )
        _publish_materialized(output, destination, check_active=check)
        return facts
