"""Reviewed Skill script registry; run installed code, never arbitrary shell text."""

import asyncio
import json
import os
import sys
from hashlib import sha256
from pathlib import Path

from .store import Conflict

SCRIPTS = {"camera-workflow/candidate-summary": ("camera-workflow", "scripts/candidate_summary.py")}


def available_scripts(enabled):
    return [name for name, (skill, _) in SCRIPTS.items() if skill in enabled]


async def run_skill_script(host, args):
    if "strategy.harness" not in host.binding["capabilities"] or args[
        "script"
    ] not in available_scripts(host.components.active["enabled_skills"]):
        raise Conflict("reviewed Skill script not enabled")
    workspace = (host.store.root / "harness/workspace").resolve()
    path = (workspace / args["input_file"]).resolve()
    if not path.is_relative_to(workspace) or not path.is_file():
        raise Conflict("script input must be a workspace file")
    if path.stat().st_size > 1_000_000:
        raise Conflict("script input exceeds one megabyte")
    content = path.read_bytes()
    skill, relative = SCRIPTS[args["script"]]
    code = Path(__file__).parent / "skills" / skill / relative
    installed = workspace / "skills" / skill / relative
    if not installed.is_file() or installed.read_bytes() != code.read_bytes():
        raise Conflict("installed script differs from reviewed packaged source")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        str(code),
        cwd=workspace,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env={"PATH": os.defpath, "LANG": "en_US.UTF-8"},
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(content), timeout=5)
    except BaseException:
        if process.returncode is None:
            process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        raise Conflict(
            "invalid candidate manifest; requires unique IDs, source_ref and positive duration_ms"
        )
    return {
        "script": args["script"],
        "script_sha256": sha256(code.read_bytes()).hexdigest(),
        "input_file": str(path.relative_to(workspace)),
        "input_sha256": sha256(content).hexdigest(),
        "execution": "local_python_subprocess",
        "result": json.loads(stdout),
    }
