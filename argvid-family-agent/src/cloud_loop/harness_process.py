"""Explicit project-local detached processes; no OS login/startup configuration."""

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx


def process_identity(pid):
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "args="], capture_output=True, text=True
    )
    if result.returncode or not result.stdout.strip():
        return None
    return hashlib.sha256(result.stdout.strip().encode()).hexdigest()


def _manage_locked(action, side, profile, profile_path, root):
    folder = root / "processes"
    folder.mkdir(parents=True, exist_ok=True)
    record = folder / (side + ".json")
    port = getattr(profile, side + "_port")
    saved = json.loads(record.read_text()) if record.exists() else None
    owned = bool(saved and process_identity(saved["pid"]) == saved["process_identity"])

    def health():
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1, trust_env=False)
            return {"reachable": True, "http_status": response.status_code, **response.json()}
        except (httpx.HTTPError, ValueError):
            return {"reachable": False}

    if action == "status":
        return {
            "side": side,
            "owned_process_alive": owned,
            "log": str(folder / (side + ".log")),
            "health": health(),
        }
    if action == "stop":
        if not owned:
            return {"side": side, "stopped": False, "reason": "no_matching_owned_process"}
        if saved.get("profile") != str(profile_path.resolve()):
            raise RuntimeError(
                "refusing to stop a service launched from a different profile; use its recorded profile"
            )
        os.kill(saved["pid"], signal.SIGTERM)
        for _ in range(100):
            if process_identity(saved["pid"]) != saved["process_identity"]:
                break
            time.sleep(0.1)
        else:
            raise RuntimeError(
                "owned process did not stop; inspect its log, no unrelated process was killed"
            )
        record.unlink(missing_ok=True)
        return {"side": side, "stopped": True, "data_preserved": True}
    if owned:
        current = health()
        if current.get("http_status") != 200:
            raise RuntimeError(
                "owned service is unhealthy; inspect its log and explicitly stop/start it"
            )
        from .harness_build import source_identity

        if (
            current.get("profile_digest") != profile.fingerprint
            or current.get("source_digest") != source_identity()["digest"]
        ):
            raise RuntimeError(
                "running service uses different code or configuration; explicitly stop before restarting"
            )
        return {"side": side, "already_running": True, "health": current}
    from .dual_cli import check_ports

    check_ports([port])
    ident = uuid.uuid4().hex
    env = dict(os.environ)
    env["CIX_HARNESS_INSTANCE"] = ident
    # Resolve the package being launched; a caller's relative PYTHONPATH must not
    # silently select a different checkout after the launcher exits.
    # Keep explicitly supplied dependency directories (e.g. board-only NumPy)
    # after the pinned package. Resolve them before detaching/changing cwd.
    extras = [
        str(Path(item).resolve()) for item in env.get("PYTHONPATH", "").split(os.pathsep) if item
    ]
    env["PYTHONPATH"] = os.pathsep.join(
        dict.fromkeys([str(Path(__file__).resolve().parents[1]), *extras])
    )
    command = [
        sys.executable,
        "-m",
        "cloud_loop.cli",
        "harness",
        side,
        "--profile",
        str(profile_path.resolve()),
    ]
    with (folder / (side + ".log")).open("ab") as log:
        child = subprocess.Popen(
            command,
            cwd=Path.cwd(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    fingerprint = process_identity(child.pid)
    if not fingerprint:
        raise RuntimeError("service exited during launch; inspect project log")
    saved = {
        "pid": child.pid,
        "process_identity": fingerprint,
        "instance_id": ident,
        "profile": str(profile_path.resolve()),
        "created": time.time(),
    }
    temporary = record.with_suffix(".tmp")
    temporary.write_text(json.dumps(saved, indent=2))
    temporary.replace(record)
    for _ in range(180):
        if child.poll() is not None:
            raise RuntimeError("service exited during startup; inspect project log")
        current = health()
        if (
            current.get("http_status") == 200
            and current.get("instance_id") == ident
            and current.get("worker_started")
        ):
            # macOS Python may re-exec from a shim to the framework executable.
            # Record identity only once the final server executable is ready.
            saved["process_identity"] = process_identity(child.pid)
            temporary.write_text(json.dumps(saved, indent=2))
            temporary.replace(record)
            return {
                "side": side,
                "started": True,
                "pid": child.pid,
                "log": str(folder / (side + ".log")),
                "health": current,
            }
        time.sleep(0.2)
    raise RuntimeError(
        "service startup timed out; status/stop can inspect or stop the recorded project process"
    )


def manage(action, side, profile, profile_path, root):
    import fcntl

    folder = root / "processes"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / (side + ".manager.lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _manage_locked(action, side, profile, profile_path, root)
