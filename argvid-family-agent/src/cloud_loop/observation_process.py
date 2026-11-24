"""Own exactly one project-native model server, separately from model readiness.

Identity requires a unique CLI alias plus OS process fingerprint. A child-held
lease survives host failure. No process name search or broad termination is used.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

from .store import Conflict

_controllers = {}


def identity(pid, instance):
    if type(pid) is not int or pid <= 1:
        return None
    probe = subprocess.run(
        ["ps", "-ww", "-p", str(pid), "-o", "lstart=", "-o", "args="],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        timeout=2,
    )
    if probe.returncode or instance not in probe.stdout.split():
        return None
    return hashlib.sha256(probe.stdout.strip().encode()).hexdigest()


def birth(pid):
    if type(pid) is not int or pid <= 1:
        return None
    if sys.platform.startswith("linux"):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            return Path("/proc/sys/kernel/random/boot_id").read_text().strip() + ":" + fields[19]
        except (OSError, IndexError):
            return None
    probe = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        timeout=2,
    )
    return probe.stdout.strip() if not probe.returncode and probe.stdout.strip() else None


class NativeModelProcess:
    def __init__(
        self, root, command, port, *, startup_timeout=20, stop_timeout=2, log_bytes=1024**2
    ):
        if os.name != "posix":
            raise ValueError("native process ownership requires POSIX")
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("invalid native model port")
        if not command or any(not isinstance(x, str) or not x or "\0" in x for x in command):
            raise ValueError("invalid native command")
        if not Path(command[0]).is_absolute() or not Path(command[0]).is_file():
            raise ValueError("native executable must be an existing absolute file")
        if any(flag in command for flag in ("--host", "--port", "--alias", "-a", "--log-file")):
            raise ValueError("model listener, identity and logs are owned by process manager")
        if type(log_bytes) is not int or log_bytes < 1024:
            raise ValueError("invalid diagnostic log bound")
        if any(
            type(v) not in {float, int} or not 0 < v <= 300 for v in (startup_timeout, stop_timeout)
        ):
            raise ValueError("invalid process deadline")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.command, self.port = tuple(command), port
        self.startup_timeout, self.stop_timeout, self.log_bytes = (
            startup_timeout,
            stop_timeout,
            log_bytes,
        )
        self.key = str(self.root)
        if self.key in _controllers:
            raise Conflict("native model already has a controller")
        self.controller = (self.root / "controller.lock").open("a")
        try:
            fcntl.flock(self.controller, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.controller.close()
            raise Conflict("native model already has a controller") from None
        _controllers[self.key] = self
        self.record_path = self.root / "process.json"
        self.client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}", timeout=0.3, trust_env=False
        )
        self.lock = asyncio.Lock()
        self.child = None
        self.spawn_task = None
        self.child_lease = None
        self.log_task = None
        self.cleanup = None
        self.closing = False
        self.released = False
        self.log_error = None

    @property
    def endpoint(self):
        return f"http://127.0.0.1:{self.port}"

    def _read(self):
        if not self.record_path.exists():
            return None
        value = json.loads(self.record_path.read_text())
        if value.get("root") != str(self.root) or value.get("port") != self.port:
            raise Conflict("native process record belongs to another binding")
        return value

    def _save(self, value):
        path = self.record_path.with_suffix(".tmp")
        descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output)
            output.flush()
            os.fsync(output.fileno())
        path.replace(self.record_path)

    def _lease(self):
        handle = (self.root / "child.lock").open("a")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        return handle

    async def _health(self, instance):
        try:
            response = await self.client.get("/health")
            response.raise_for_status()
            value = response.json()
            if (
                value.get("instance_id") != instance
                or value.get("receipt_protocol") != "cix-simplex/1"
                or value.get("engine") != "comni"
                or type(value.get("pid")) is not int
            ):
                return None
            return value
        except (httpx.HTTPError, ValueError, AttributeError):
            return None

    async def status(self):
        """Read-only listener evidence, never a model-loading claim or restart."""
        record = self._read()
        if not record:
            return {"state": "not_started", "server_reachable": False}
        if record["status"] == "stopped":
            return {"state": "stopped", "server_reachable": False}
        pid = record.get("pid")
        if not pid:
            return {"state": "starting", "server_reachable": None}
        fingerprint = await asyncio.to_thread(identity, pid, record["instance"])
        if fingerprint is None:
            return {"state": "exited", "server_reachable": False}
        if fingerprint != record.get("identity"):
            return {"state": "unconfirmed", "server_reachable": None}
        health = await self._health(record["instance"]) if not self.released else None
        if self._read() != record:
            return {"state": "changing", "server_reachable": None}
        if health is None or health["pid"] != pid:
            return {"state": "unreachable", "server_reachable": False}
        return {"state": "server_ready", "server_reachable": True, "engine": health["engine"]}

    async def _logs(self, stream):
        path = self.root / "native.log"
        backup = self.root / "native.previous.log"
        while True:
            data = await stream.read(16384)
            if not data:
                return
            try:
                # Diagnostic tail is bounded; authoritative model/tool evidence
                # is stored by the Harness, not reconstructed from stdout.
                if path.exists() and path.stat().st_size + len(data) > self.log_bytes:
                    path.replace(backup)
                with path.open("ab") as output:
                    output.write(data[-self.log_bytes :])
            except OSError as exc:
                self.log_error = type(
                    exc
                ).__name__  # keep draining, never deadlock inference on a full pipe

    async def _stop(self):
        if self.released or _controllers.get(self.key) is not self:
            return False
        record = self._read()
        if self.spawn_task and self.child is None:
            try:
                self.child = await asyncio.shield(self.spawn_task)
            except Exception:
                self.spawn_task = None
        if self.child is not None and self.log_task is None:
            self.log_task = asyncio.create_task(self._logs(self.child.stdout))
        if not record:
            if self.child is not None:
                return False
            if self.child_lease:
                self.child_lease.close()
                self.child_lease = None
            free = self._lease()
            if free is None:
                return False
            free.close()
            return True
        pid = record.get("pid")
        fingerprint = record.get("identity")
        instance = record["instance"]
        if pid is None and self.child is not None:
            pid = self.child.pid
            fingerprint = await asyncio.to_thread(identity, pid, instance)
            record.update(pid=pid, identity=fingerprint, birth=await asyncio.to_thread(birth, pid))
        if pid is None:
            # Recover a host crash after spawning but before committing PID.
            health = await self._health(instance)
            if health:
                pid = health["pid"]
                fingerprint = await asyncio.to_thread(identity, pid, instance)
                if fingerprint:
                    record.update(
                        pid=pid, identity=fingerprint, birth=await asyncio.to_thread(birth, pid)
                    )
                    self._save(record)
        current = await asyncio.to_thread(identity, pid, instance) if pid else None
        if pid and current is None and self.child is not None and self.child.returncode is None:
            deadline = time.monotonic() + self.stop_timeout
            while current is None and self.child.returncode is None and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
                current = await asyncio.to_thread(identity, pid, instance)
        if current is not None and fingerprint is None:
            # The PID was journaled while exec/readiness was still settling.
            # The per-spawn unguessable CLI alias identifies that intended child.
            fingerprint = current
            record["identity"] = fingerprint
        if current is not None:
            same_birth = bool(
                record.get("birth") and record["birth"] == await asyncio.to_thread(birth, pid)
            )
            if not fingerprint or (current != fingerprint and not same_birth):
                return False
            fingerprint = current
            if self.released or _controllers.get(self.key) is not self:
                return False
            try:
                if os.getpgid(pid) != pid:
                    return False
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + self.stop_timeout
            while time.monotonic() < deadline:
                if self.child and self.child.pid == pid and self.child.returncode is not None:
                    break
                present = await asyncio.to_thread(identity, pid, instance)
                if present is None:
                    break
                await asyncio.sleep(0.025)
            present = await asyncio.to_thread(identity, pid, instance)
            still_owned = present is not None and (
                present == fingerprint
                or bool(
                    record.get("birth") and record["birth"] == await asyncio.to_thread(birth, pid)
                )
            )
            if still_owned and not self.released and _controllers.get(self.key) is self:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        if self.child:
            try:
                await asyncio.wait_for(self.child.wait(), self.stop_timeout)
            except TimeoutError:
                return False
        # Close our descriptor without LOCK_UN: any surviving descendant keeps
        # the shared child lease held and prevents a false reclamation receipt.
        if self.child_lease:
            self.child_lease.close()
            self.child_lease = None
        free = self._lease()
        if free is None:
            return False
        free.close()
        if pid and await asyncio.to_thread(identity, pid, instance) is not None:
            return False
        if self.log_task:
            try:
                await asyncio.wait_for(asyncio.shield(self.log_task), self.stop_timeout)
            except TimeoutError:
                return False
            self.log_task = None
        self.child = None
        self.spawn_task = None
        record.update(status="stopped", stopped_at=time.time())
        try:
            self._save(record)
        except OSError as exc:
            self.log_error = type(exc).__name__
        return True

    async def _finish_cleanup(self):
        if self.cleanup:
            task = self.cleanup
            try:
                await asyncio.shield(task)
            finally:
                if task.done():
                    self.cleanup = None

    async def reset(self):
        async with self.lock:
            if self.closing:
                raise Conflict("native controller is closed")
            await self._finish_cleanup()
            if not await self._stop():
                raise Conflict("previous native process still owns resources")
            with socket.socket() as check:
                check.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    check.bind(("127.0.0.1", self.port))
                except OSError:
                    raise Conflict("native model port is already occupied") from None
            self.child_lease = self._lease()
            if self.child_lease is None:
                raise Conflict("native child lease is still held")
            instance = "cix-native-" + uuid.uuid4().hex
            command = [
                *self.command,
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--alias",
                instance,
            ]
            record = {
                "root": str(self.root),
                "port": self.port,
                "instance": instance,
                "pid": None,
                "identity": None,
                "command": command,
                "status": "starting",
                "created": time.time(),
            }
            environment = {**os.environ, "CIX_NATIVE_INSTANCE": instance}
            try:
                self._save(record)
                self.spawn_task = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        *command,
                        cwd=self.root,
                        env=environment,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        start_new_session=True,
                        pass_fds=(self.child_lease.fileno(),),
                    )
                )
                self.child = await asyncio.shield(self.spawn_task)
                self.log_task = asyncio.create_task(self._logs(self.child.stdout))
                record.update(
                    pid=self.child.pid,
                    identity=await asyncio.to_thread(identity, self.child.pid, instance),
                    birth=await asyncio.to_thread(birth, self.child.pid),
                )
                self._save(record)
                deadline = time.monotonic() + self.startup_timeout
                while time.monotonic() < deadline:
                    if self.closing:
                        raise Conflict("native startup cancelled by close")
                    if self.child.returncode is not None:
                        raise Conflict("native process exited during startup")
                    if not record["identity"]:
                        record["identity"] = await asyncio.to_thread(
                            identity, self.child.pid, instance
                        )
                        if record["identity"]:
                            record["birth"] = await asyncio.to_thread(birth, self.child.pid)
                            self._save(record)
                    health = await self._health(instance)
                    if health and health["pid"] == self.child.pid:
                        # Some Python test/runtime launchers re-exec in place.
                        # Freeze identity only after the final listener replies.
                        record["identity"] = await asyncio.to_thread(
                            identity, self.child.pid, instance
                        )
                        if not record["identity"]:
                            raise Conflict("native server identity disappeared during readiness")
                        record["status"] = "server_ready"
                        self._save(record)
                        return True
                    await asyncio.sleep(0.05)
                raise TimeoutError("owned native server did not become ready")
            except BaseException:
                self.cleanup = asyncio.create_task(self._stop())
                await asyncio.shield(self.cleanup)
                raise

    async def abort(self):
        async with self.lock:
            if self.released or _controllers.get(self.key) is not self:
                return False
            await self._finish_cleanup()
            return await self._stop()

    async def close(self):
        self.closing = True
        async with self.lock:
            if self.released:
                return True
            await self._finish_cleanup()
            if not await self._stop():
                return False
            await self.client.aclose()
            self.controller.close()
            self.released = True
            if _controllers.get(self.key) is self:
                del _controllers[self.key]
            return True
