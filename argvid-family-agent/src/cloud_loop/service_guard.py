"""Independent, maintenance-aware recovery of explicitly enrolled local services.

No camera commands, model calls, task mutations or credentials. Run as a short
systemd user timer job, so a wedged application loop cannot disable this check.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit


UNITS = {"muse-edge.service", "muse-cloud.service", "muse-clip.service"}


def validate(config):
    if type(config.get("desired_running")) is not bool:
        raise ValueError("explicit service running intent is required")
    services = config["services"]
    if not services or len({s["unit"] for s in services}) != len(services):
        raise ValueError("distinct enrolled services required")
    for service in services:
        url = urlsplit(service["health_url"])
        if (service["unit"] not in UNITS or url.scheme != "http"
                or url.hostname != "127.0.0.1" or url.username or url.password
                or url.query or url.fragment or url.path not in {"/health", "/props"}):
            raise ValueError("guard accepts only enrolled Muse units and loopback health endpoints")
    for key, minimum in (("failure_threshold", 2), ("cooldown_seconds", 30)):
        if type(config[key]) is not int or config[key] < minimum:
            raise ValueError("invalid recovery threshold")
    return config


def inspect_unit(unit):
    result = subprocess.check_output(
        ["systemctl", "--user", "show", unit, "-p", "ActiveState", "-p", "SubState",
         "-p", "MainPID"], text=True, timeout=5,
    )
    return dict(line.split("=", 1) for line in result.splitlines() if "=" in line)


def probe(service):
    # Do not inherit HTTP proxy variables for node-local checks.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(service["health_url"], timeout=2) as reply:
        value = json.load(reply)
    if service["health_url"].endswith("/props"):
        return value.get("read_only") is True and bool(value.get("instance_id"))
    return value.get("worker_started") is True and value.get("worker_failed") is False


def restart(unit):
    subprocess.run(["systemctl", "--user", "restart", "--no-block", unit],
                   check=True, capture_output=True, timeout=5)


def check(config, state, *, now=None, inspect=inspect_unit, health=probe, recover=restart):
    """Each target fails/retries independently; restart request is not recovery proof."""
    validate(config)
    now = time.time() if now is None else now
    state["checked_at"] = now
    if not config["desired_running"] or now < config.get("hold_until", 0):
        state["mode"] = "maintenance"
        # A deliberate maintenance interval must not count as failed probes.
        for record in state.get("services", {}).values():
            record["failures"] = 0
        return state
    state["mode"] = "monitoring"
    for service in config["services"]:
        unit = service["unit"]
        record = state.setdefault("services", {}).setdefault(unit, {"failures": 0, "next_recovery": 0})
        try:
            current = inspect(unit)
            record.update(observed_at=now, active_state=current.get("ActiveState"),
                          main_pid=current.get("MainPID"))
            if current.get("ActiveState") in {"activating", "deactivating", "reloading"}:
                # systemd's own finite start/stop deadlines handle transitions.
                record["status"] = "transitioning"
                continue
            healthy = current.get("ActiveState") == "active" and health(service)
            if healthy:
                record.update(failures=0, status="healthy", last_success=now)
                continue
            record["last_error"] = "health_unavailable"
        except Exception as exc:
            record["last_error"] = type(exc).__name__  # Never log provider/command text.
        record["failures"] += 1
        record["status"] = "retry_waiting"
        if record["failures"] < config["failure_threshold"] or now < record["next_recovery"]:
            continue
        record["next_recovery"] = now + config["cooldown_seconds"]
        try:
            recover(unit)
            record.update(status="restart_requested", last_restart_request=now)
        except Exception as exc:
            record.update(status="restart_failed", last_error=type(exc).__name__)
    return state


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".pending")
    with temporary.open("w") as out:
        json.dump(value, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["check", "status", "running", "stopped", "hold"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--seconds", type=int, default=180)
    args = parser.parse_args()
    path = args.config.resolve(strict=True)
    # An advisory lock also serializes maintenance intent against a recovery check.
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = validate(json.loads(path.read_text()))
        state_path = path.with_name("service-guard-state.json")
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        if args.action == "check":
            state = check(config, state)
            save(state_path, state)
        elif args.action in {"running", "stopped", "hold"}:
            if args.action == "hold":
                if not 1 <= args.seconds <= 3600:
                    raise ValueError("maintenance hold must be between 1 second and 1 hour")
                config["hold_until"] = time.time() + args.seconds
            else:
                config.update(desired_running=args.action == "running", hold_until=0)
            config["intent_updated_at"] = time.time()
            save(path, config)
        print(json.dumps({"desired_running": config["desired_running"],
                          "hold_until": config.get("hold_until"), "state": state}, ensure_ascii=False))


if __name__ == "__main__":
    main()
