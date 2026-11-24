"""Run agent-authored Remotion projects through the existing media job queue."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import zipfile

from .contracts import digest
from .harness_media_contracts import MediaProject
from .media import _publish_materialized, file_hash, inspect_video
from .media_storage import render_workspace
from .store import Conflict


def relative_path(value):
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("project files must use relative paths")
    return path


def project_archive_path(host, project):
    name = relative_path(project["archive"])
    if len(name.parts) != 1:
        raise Conflict("project archive must be a registered filename")
    legacy = host.store.root / "edge-renders" / name
    current = host.store.root / "render-projects" / name
    return legacy if legacy.is_file() else current


def prepare_project(host, task, operation, value, workspace=None):
    if operation != "compose":
        raise Conflict("a Remotion project produces a composition")
    if task["constraints"].get("media_recipe"):
        raise Conflict("update the saved recipe task before submitting a different project")
    runtime = Path(host.settings.remotion_runtime_dir or "")
    if not (runtime / "node_modules/@remotion/cli/remotion-cli.js").is_file():
        raise Conflict("Remotion project runtime is not configured on this node")
    project = MediaProject.model_validate(value)
    workspace = Path(workspace or host.runtime.workspace).resolve()
    source = (workspace / project.directory).resolve()
    roots = [workspace, Path(host.runtime.workspace).resolve()]
    if hasattr(host.runtime, "root"):
        roots.append((host.runtime.root / "workspaces").resolve())
    if not any(source.is_relative_to(root) for root in roots) or not source.is_dir():
        raise Conflict("project directory must exist in the agent workspace")
    entry = relative_path(project.entry_point)
    available = {a["id"]: a for a in host.effects.assets(None, task["principal"])}
    inputs = []
    for asset_id in dict.fromkeys(project.asset_ids):
        if asset_id not in available:
            raise Conflict("project input is outside the available media catalogue")
        asset = host.store.asset(asset_id)
        facts = asset["info"].get("asset", {})
        if not facts.get("sha256"):
            raise Conflict("project input has no verified media identity")
        inputs.append({"asset_id": asset_id, "sha256": facts["sha256"]})

    files, size = [], 0
    folder = host.store.root / "render-projects"
    folder.mkdir(exist_ok=True)
    with host.media_storage.reserve("render-projects", host.settings.media_project_max_bytes):
        with tempfile.NamedTemporaryFile(dir=folder, suffix=".partial") as temporary:
            with zipfile.ZipFile(temporary.name, "w", zipfile.ZIP_DEFLATED) as archive:
                for path in sorted(source.rglob("*")):
                    relative = path.relative_to(source)
                    if set(relative.parts) & {"node_modules", ".git", "out", "dist"}:
                        continue
                    if path.is_symlink():
                        raise Conflict("project source cannot contain symbolic links")
                    if not path.is_file():
                        continue
                    if len(files) >= host.settings.media_project_max_files:
                        raise Conflict("project source exceeds configured file budget")
                    with path.open("rb") as stream:
                        data = stream.read(host.settings.media_project_max_bytes - size + 1)
                    size += len(data)
                    if size > host.settings.media_project_max_bytes:
                        raise Conflict("project source exceeds configured byte budget")
                    archive.writestr(relative.as_posix(), data)
                    files.append(
                        {"path": relative.as_posix(), "sha256": hashlib.sha256(data).hexdigest()}
                    )
            if entry.as_posix() not in {f["path"] for f in files}:
                raise Conflict("project entry point is missing")
            identity = digest({"files": files, "project": project.model_dump(), "inputs": inputs})
            destination = folder / ("project-" + identity + ".zip")
            if not destination.exists():
                shutil.copyfile(temporary.name, destination)
        host.media_storage.register(destination, "render-projects", owner=task["id"])
    return {
        "kind": "remotion_project",
        "project": {
            "archive": destination.name,
            "archive_sha256": file_hash(destination),
            "source_digest": identity,
            "entry_point": entry.as_posix(),
            "composition_id": project.composition_id,
            "files": files,
            "inputs": inputs,
        },
    }


def render_project(host, job, task, plan, *, check_active=None):
    """The official CLI executes the submitted project, with no generated edit template."""
    check = check_active or (lambda: None)
    project = plan["project"]
    runtime = Path(host.settings.remotion_runtime_dir).resolve()
    cli = runtime / "node_modules/@remotion/cli/remotion-cli.js"
    node = shutil.which("node")
    if not node or not cli.is_file():
        raise Conflict("Remotion runtime is unavailable")
    folder = host.store.root / "edge-renders"
    archive_path = project_archive_path(host, project)
    if file_hash(archive_path, check_active=check) != project["archive_sha256"]:
        raise Conflict("submitted project source changed")
    destination = folder / ("media-" + job["id"] + ".mp4")
    sources = []
    started = time.monotonic()
    with render_workspace(folder) as work:
        with zipfile.ZipFile(archive_path) as archive:
            for name in archive.namelist():
                relative_path(name)
            archive.extractall(work)
        for item in project["files"]:
            if file_hash(work / item["path"]) != item["sha256"]:
                raise Conflict("submitted project file differs from its receipt")
        public = work / "public"
        public.mkdir(exist_ok=True)
        for item in project["inputs"]:
            asset = host.store.asset(item["asset_id"])
            source = Path(asset["path"])
            target = public / (item["asset_id"] + ".mp4")
            if target.exists():
                raise Conflict("project file shadows a registered media input")
            checksum = hashlib.sha256()
            with source.open("rb") as incoming, target.open("wb") as output:
                for block in iter(lambda: incoming.read(1024 * 1024), b""):
                    check()
                    checksum.update(block)
                    output.write(block)
            if checksum.hexdigest() != item["sha256"]:
                raise Conflict("project media input changed")
            sources.append(
                {
                    "asset_id": asset["id"],
                    "sha256": item["sha256"],
                    "provenance": asset["info"].get("provenance"),
                    "simulated": asset["info"].get(
                        "simulated", asset["info"].get("asset", {}).get("simulated", False)
                    ),
                }
            )
        (work / "node_modules").symlink_to(runtime / "node_modules", target_is_directory=True)
        output = work / "rendered.mp4"
        command = [
            node,
            str(cli),
            "render",
            str(work / project["entry_point"]),
            project["composition_id"],
            str(output),
            "--codec=h264",
            "--audio-codec=aac",
            "--pixel-format=yuv420p",
            "--concurrency=" + str(host.settings.media_render_threads),
        ]
        if host.settings.remotion_browser_executable:
            command.append("--browser-executable=" + host.settings.remotion_browser_executable)
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
                    if time.monotonic() - started > host.profile.max_task_seconds:
                        raise TimeoutError("project render exceeded configured task time")
                    time.sleep(0.5)
                if process.returncode:
                    log.seek(0)
                    raise RuntimeError("Remotion render failed: " + log.read()[-3000:])
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
            log.seek(0)
            render_log = log.read()[-12000:]
        facts = inspect_video(output, check_active=check)
        duration = task["constraints"].get("duration_ms")
        if duration and abs(facts["duration_ms"] - duration) > 1000 / facts["fps"]:
            raise Conflict("render duration differs from the saved task constraint")
        check()
        _publish_materialized(output, destination, check_active=check)
    simulated = any(s["simulated"] for s in sources)
    manifest = {
        "schema_version": "cix.media-project/1",
        "task_id": task["id"],
        "task_version": task["version"],
        "asset": {
            **facts,
            "asset_id": "media-" + job["id"],
            "uri": host.side + "-asset://media-" + job["id"],
            "simulated": simulated,
            "storage_side": host.side,
            "execution_side": host.side,
            "local_copy_role": "original",
        },
        "project": project,
        "renderer": {
            "name": "remotion",
            "version": json.loads((runtime / "node_modules/remotion/package.json").read_text())[
                "version"
            ],
            "seconds": time.monotonic() - started,
            "log_tail": render_log,
        },
        "provenance": {
            "sources": sources,
            "input_relation": "declared_available_inputs",
            "materialization": "agent_authored_remotion_project",
        },
        "qc": {"technical_passed": True, "visual_quality_assessed": False},
        "simulated": simulated,
    }
    return destination, manifest
