"""Explicit preparation, process supervision and real-model demo verification."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import uvicorn

from .contracts import load_profile
from .media import prepare_clips
from .plugins import require_profile


def add_parser(commands):
    dual = commands.add_parser("dual", help="双端 Agent、真实视频回放与网页")
    actions = dual.add_subparsers(dest="dual_command", required=True)
    for name in ["prepare", "serve", "cloud", "edge", "verify", "verify-mock"]:
        parser = actions.add_parser(name)
        parser.add_argument("--profile", type=Path, default=Path("profiles/demo-bridge.json"))
        if name == "verify":
            parser.add_argument("--suite", choices=["acceptance"], default="acceptance")


def root_for(profile):
    root = Path(profile.data_root).resolve()
    if root == Path.cwd().resolve() or not root.is_relative_to(Path.cwd().resolve()):
        raise ValueError("demo data_root must be a subdirectory of this project")
    root.mkdir(parents=True, exist_ok=True)
    return root


def local_token(root):
    path = root / "node-token"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return path.read_text().strip()
    else:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
        return path.read_text().strip()


def download_source(root):
    """Public licensed asset, fixed source and integrity metadata. No provider key used."""
    config = json.loads(
        (Path(__file__).resolve().parents[2] / "examples" / "media-source.json").read_text()
    )
    folder = root / "source"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "cat-track.webm"
    if not path.exists():
        temporary = path.with_suffix(".partial")
        result = subprocess.run(
            [
                "curl",
                "--fail",
                "--location",
                "--retry",
                "2",
                "--retry-delay",
                "15",
                "--connect-timeout",
                "15",
                "--max-time",
                "90",
                "--user-agent",
                "CIXCloudLoopDemo/0.1 (personal prototype)",
                "--output",
                str(temporary),
                config["download_url"],
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError("licensed video download failed; source not replaced")
        if (
            temporary.stat().st_size != config["expected_size_bytes"]
            or hashlib.sha1(temporary.read_bytes()).hexdigest() != config["expected_sha1"]
        ):
            raise RuntimeError("downloaded source differs from published original")
        temporary.replace(path)
    if (
        path.stat().st_size != config["expected_size_bytes"]
        or hashlib.sha1(path.read_bytes()).hexdigest() != config["expected_sha1"]
    ):
        raise RuntimeError("existing original source failed integrity check")
    metadata_path = folder / "source.json"
    if not metadata_path.exists():
        from .contracts import utcnow

        record = {k: v for k, v in config.items() if not k.startswith("expected_")}
        record.update(
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            size_bytes=path.stat().st_size,
            downloaded_at=utcnow().isoformat(),
        )
        metadata_path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    selected = folder / "selection.json"
    template = Path(__file__).resolve().parents[2] / "examples" / "media-selection.json"
    if not selected.exists() and template.exists():
        selected.write_text(template.read_text())
    if not selected.exists():
        raise RuntimeError(
            "download complete; source requires visual selection before clip preparation"
        )


def check_ports(ports):
    probes = []
    try:
        for port in ports:
            sock = socket.socket()
            probes.append(sock)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
    finally:
        for sock in probes:
            sock.close()


def serve(profile, profile_path, root):
    check_ports((profile.cloud_port, profile.edge_port))
    for side, kind in (("cloud", "wide"), ("edge", "closeup")):
        if not (root / side / "fixtures" / f"{kind}.mp4").exists():
            raise RuntimeError("run dual prepare before serve")
    local_token(root)
    children, logs = [], []
    try:
        for side in ("cloud", "edge"):
            log = (root / f"{side}.log").open("a")
            logs.append(log)
            children.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "cloud_loop.dual_cli",
                        side,
                        "--profile",
                        str(profile_path.resolve()),
                    ],
                    stdout=log,
                    stderr=log,
                )
            )
        (root / "processes.json").write_text(
            json.dumps(
                {
                    "supervisor_pid": os.getpid(),
                    "children": {side: p.pid for side, p in zip(("cloud", "edge"), children)},
                    "profile": str(profile_path.resolve()),
                    "started_at": time.time(),
                },
                indent=2,
            )
        )
        ready = set()
        deadline = time.monotonic() + 20
        with httpx.Client(trust_env=False, timeout=0.5) as client:
            while len(ready) < 2 and time.monotonic() < deadline:
                if any(p.poll() is not None for p in children):
                    raise RuntimeError("demo startup failed; inspect project-local logs")
                for side, port, child in zip(
                    ("cloud", "edge"), (profile.cloud_port, profile.edge_port), children
                ):
                    try:
                        response = client.get(f"http://127.0.0.1:{port}/health")
                        response.raise_for_status()
                        if response.json().get("pid") == child.pid:
                            ready.add(side)
                    except (httpx.HTTPError, ValueError):
                        pass
                if len(ready) < 2:
                    time.sleep(0.15)
        if len(ready) < 2:
            raise RuntimeError("demo processes did not become healthy in time")
        print(
            f"网页: http://127.0.0.1:{profile.cloud_port}/；Ctrl-C 停止本次两侧进程。", flush=True
        )
        while all(p.poll() is None for p in children):
            time.sleep(0.5)
        raise RuntimeError("a demo child exited; inspect the project-local logs")
    except KeyboardInterrupt:
        pass
    finally:
        for p in children:
            if p.poll() is None:
                p.terminate()
        for p in children:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        for log in logs:
            log.close()


async def verify(profile, root):
    """Advance only scene facts; completion must come from the real edge host."""
    scenario = json.loads(
        (Path(__file__).resolve().parents[2] / "examples" / "cat-replay.json").read_text()
    )
    async with (
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{profile.cloud_port}", trust_env=False, timeout=30
        ) as cloud,
        httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{profile.edge_port}", trust_env=False, timeout=30
        ) as edge,
    ):
        status = (await cloud.get("/api/status")).json()
        if not status["node"]["online"]:
            raise RuntimeError("edge is offline")
        response = await edge.post("/simulation/events", json={"stage": "S0"})
        response.raise_for_status()
        response = await cloud.post("/tasks", json=scenario)
        response.raise_for_status()
        task_id = response.json()["id"]
        folder = root / "evidence" / task_id
        folder.mkdir(parents=True, exist_ok=True)
        observations = []
        started = time.monotonic()
        edge_id, last_run, stage = None, 0, "S0"
        try:
            while time.monotonic() - started < profile.max_task_seconds:
                task_response = await cloud.get(f"/tasks/{task_id}")
                task_response.raise_for_status()
                task = task_response.json()
                if task["edge_tasks"]:
                    edge_id = task["edge_tasks"][0]["id"]
                if task["status"] in {
                    "failed",
                    "unsupported",
                    "needs_input",
                    "expired",
                    "cancelled",
                }:
                    # A new wake can legitimately be queued immediately after this run.
                    if not any(e["status"] == "queued" for e in task["events"]):
                        raise RuntimeError(f"cloud task ended as {task['status']}")
                if edge_id:
                    er = await edge.get(f"/tasks/{edge_id}")
                    if er.status_code == 200:
                        local = er.json()
                        if local["status"] == "failed":
                            raise RuntimeError("edge model run failed")
                        if (
                            stage in {"S0", "S1"}
                            and local["status"] == "waiting"
                            and len(local["runs"]) > last_run
                        ):
                            if local["assets"]:
                                raise RuntimeError("edge produced an asset while blocked")
                            observations.append(
                                {
                                    "stage": stage,
                                    "status": "waiting",
                                    "assets": 0,
                                    "runs": len(local["runs"]),
                                }
                            )
                            last_run = len(local["runs"])
                            stage = "S1" if stage == "S0" else "S2"
                            r = await edge.post("/simulation/events", json={"stage": stage})
                            r.raise_for_status()
                            print(f"scene={stage}", flush=True)
                if task["status"] == "completed":
                    break
                await asyncio.sleep(0.4)
            else:
                raise TimeoutError("real-model scenario timed out")
            assets = [a for a in task["assets"] if a["kind"] == "deliverable"]
            if len(assets) != 1 or len(observations) != 2:
                raise RuntimeError("missing deliverable or waiting-stage evidence")
            content = await cloud.get(f"/assets/{assets[0]['id']}")
            content.raise_for_status()
            sha = hashlib.sha256(content.content).hexdigest()
            if sha != assets[0]["info"]["asset"]["sha256"]:
                raise RuntimeError("HTTP-delivered file checksum mismatch")
            events = (await cloud.get(f"/tasks/{task_id}/events")).json()
            completion = next(e["payload"] for e in events if e["kind"] == "bridge.completed")
            duplicate = await cloud.post(
                "/gateway/events",
                json=completion,
                headers={"Authorization": f"Bearer {local_token(root)}"},
            )
            duplicate.raise_for_status()
            if not duplicate.json().get("duplicate"):
                raise RuntimeError("duplicate completion was not ignored")
            (folder / "result.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "scope": "real deferred media scenario",
                        "task_id": task_id,
                        "waiting_stages": observations,
                        "duplicate_completion": duplicate.json(),
                        "download_sha256": sha,
                        "deliverable": assets[0],
                        "elapsed_seconds": time.monotonic() - started,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            print(f"真实 deferred 场景通过；证据: {folder}", flush=True)
        except Exception as exc:
            (folder / "failure.json").write_text(
                json.dumps(
                    {
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "reason": str(exc),
                        "waiting_stages": observations,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            raise
        finally:
            (folder / "cloud.json").write_text(
                json.dumps(
                    (await cloud.get(f"/tasks/{task_id}")).json(), ensure_ascii=False, indent=2
                )
            )
            (folder / "events.json").write_text(
                json.dumps(
                    (await cloud.get(f"/tasks/{task_id}/events")).json(),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            if edge_id:
                (folder / "edge.json").write_text(
                    json.dumps(
                        (await edge.get(f"/tasks/{edge_id}")).json(), ensure_ascii=False, indent=2
                    )
                )


def dispatch(args):
    profile = load_profile(args.profile)
    if profile.interaction is not None and args.dual_command != "prepare":
        raise ValueError("use cloud-loop interaction for an edge-owned dialogue profile")
    require_profile(profile)
    root = root_for(profile)
    if args.dual_command == "prepare":
        download_source(root)
        print(json.dumps(prepare_clips(root), ensure_ascii=False, indent=2))
    elif args.dual_command == "serve":
        serve(profile, args.profile, root)
    elif args.dual_command == "verify":
        asyncio.run(verify(profile, root))
    elif args.dual_command == "verify-mock":
        from .o6n_verify import verify as verify_mock

        asyncio.run(verify_mock(profile, root))
    else:
        token = local_token(root)
        if args.dual_command == "cloud":
            from .dual_app import create_cloud_app

            app, port = create_cloud_app(profile, root, token), profile.cloud_port
        else:
            from .edge import create_edge_app

            app, port = create_edge_app(profile, root, token), profile.edge_port
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=3,
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("dual_command", choices=["cloud", "edge"])
    parser.add_argument("--profile", type=Path, required=True)
    dispatch(parser.parse_args())
