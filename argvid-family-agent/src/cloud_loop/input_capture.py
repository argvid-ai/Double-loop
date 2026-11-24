"""Owned CPU capture windows; discovery never starts recording.

Windows are independently encoded and explicitly discontinuous, not advertised
as gap-free real-time capture. Unpublished crash files remain in the capture
directory for recovery; originals are never automatically deleted.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import signal
import sys
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

import imageio_ffmpeg

from .contracts import canonical, digest
from .input_contracts import CaptureEndpoint as CaptureEndpoint
from .observation_process import birth, identity
from .store import Conflict


def capture_root(media, task_id):
    from pydantic import TypeAdapter

    from .observation_contracts import Reference

    TypeAdapter(Reference).validate_python(task_id)
    root = media.store.root.resolve() / "input-capture" / task_id
    if root.resolve() != root:
        raise Conflict("capture directory changed")
    root.mkdir(parents=True, exist_ok=True)
    return root


async def recover_capture(media, task_id):
    from .capture_recovery_files import freeze_stopped_capture_tails

    root = capture_root(media, task_id)
    record = root / "process.json"
    lease = (root / "child.lock").open("a")
    try:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            free = True
        except BlockingIOError:
            free = False
        if not record.exists():
            return free
        saved = json.loads(record.read_text())
        pid = saved["pid"]
        if saved.get("status") == "stopped" and free:
            await asyncio.to_thread(freeze_stopped_capture_tails, media, task_id, root)
            return True
        if type(pid) is not int or pid <= 1:
            raise Conflict("invalid capture owner")
        actual = await asyncio.to_thread(identity, pid, saved["instance"])
        born = await asyncio.to_thread(birth, pid)
        if actual and actual == saved["identity"] and born == saved["birth"]:
            os.kill(pid, signal.SIGTERM)
            for _ in range(60):
                if await asyncio.to_thread(identity, pid, saved["instance"]) != actual:
                    break
                await asyncio.sleep(0.05)
            else:
                return False
        elif born is not None and born == saved["birth"]:
            return False
        elif born is None:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            except PermissionError:
                return False
            else:
                return False
        if not free:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
        saved["status"] = "stopped"
        record.write_text(canonical(saved))
        await asyncio.to_thread(freeze_stopped_capture_tails, media, task_id, root)
        return True
    finally:
        lease.close()


class CaptureController:
    def __init__(self, media, spec, config, run, window_ms):
        self.media, self.spec, self.config, self.run = media, spec, config, run
        self.window_ms = window_ms
        self.root = capture_root(media, run.task_id)
        self.closed = False
        self.task = None
        self.child = None
        self.spawn_task = None
        self.ready = asyncio.Event()
        self.cleanup = None
        self.finalizer = None

    def current(self):
        with self.media.store.connect() as db:
            row = db.execute(
                "SELECT status,version,generation FROM observation_tasks WHERE task_id=? AND principal=?",
                (self.run.task_id, self.run.principal),
            ).fetchone()
        if (
            self.closed
            or row is None
            or row["status"] not in {"starting", "running"}
            or row["version"] != self.run.version
            or row["generation"] != self.run.generation
        ):
            raise Conflict("capture no longer owns this task generation")

    def command(self, path):
        endpoint = self.spec.capture
        if endpoint.driver == "v4l2" and endpoint.address.startswith("/dev/v4l/by-id/"):
            import re
            resolved = str(Path(endpoint.address).resolve(strict=True))
            if not re.fullmatch(r"/dev/video[0-9]+", resolved):
                raise Conflict("stable camera link does not point to a video device")
        command = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-n",
        ]
        if endpoint.driver == "file_replay":
            command += ["-re", "-i", endpoint.address]
        elif endpoint.driver == "rtsp":
            command += ["-rtsp_transport", "tcp", "-rw_timeout", "10000000", "-i", endpoint.address]
        else:
            if endpoint.audio_device:
                command += ["-thread_queue_size", "512"]
            command += ["-f", endpoint.driver]
            if endpoint.driver == "v4l2":
                for flag, value in (
                    ("-input_format", endpoint.input_format),
                    ("-video_size", endpoint.video_size),
                    ("-framerate", endpoint.framerate),
                ):
                    if value is not None:
                        command += [flag, str(value)]
            if endpoint.driver == "alsa":
                if endpoint.audio_channels is not None:
                    command += ["-channels", str(endpoint.audio_channels)]
                if endpoint.audio_sample_rate is not None:
                    command += ["-sample_rate", str(endpoint.audio_sample_rate)]
            command += ["-i", endpoint.address]
        if endpoint.audio_device:
            command += ["-thread_queue_size", "512", "-f", "alsa"]
            if endpoint.audio_channels is not None:
                command += ["-channels", str(endpoint.audio_channels)]
            if endpoint.audio_sample_rate is not None:
                command += ["-sample_rate", str(endpoint.audio_sample_rate)]
            command += ["-i", endpoint.audio_device]
        video = "image" in self.spec.binding.modalities
        audio = "audio" in self.spec.binding.modalities
        if video:
            command += [
                "-map",
                "0:v:0",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-threads",
                "2",
            ]
            if endpoint.video_bitrate_bps is None:
                command += ["-crf", "18"]
            else:
                # Encoder rate control is not a disk quota; source and storage
                # admission still enforce actual bytes independently.
                rate = endpoint.video_bitrate_bps
                command += ["-b:v", str(rate), "-maxrate", str(rate), "-bufsize", str(rate * 2)]
        else:
            command += ["-vn"]
        if audio:
            command += [
                "-map",
                "1:a:0" if endpoint.audio_device else "0:a:0",
                "-c:a",
                "aac" if video else "pcm_s16le",
            ]
        else:
            command += ["-an"]
        return [
            *command,
            "-t",
            str(self.window_ms / 1000),
            "-fs",
            str(self.spec.max_file_bytes),
            str(path),
        ]

    def worker_spec(self, path, instance):
        return {
            "instance": instance,
            "command": self.command(path),
            "timeout_seconds": self.window_ms / 1000 + 10,
        }

    async def _window(self, path):
        instance = "harness-capture-" + uuid.uuid4().hex
        private = self.root / (instance + ".json")
        body = self.worker_spec(path, instance)
        private.write_text(canonical(body))
        private.chmod(0o600)
        lease = (self.root / "child.lock").open("a")
        record = None
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.spawn_task = asyncio.create_task(
                asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "cloud_loop.input_capture_worker",
                    "--config",
                    str(private),
                    "--instance",
                    instance,
                    "--lease-fd",
                    str(lease.fileno()),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    pass_fds=(lease.fileno(),),
                )
            )
            try:
                self.child = await asyncio.shield(self.spawn_task)
            except asyncio.CancelledError:
                while not self.spawn_task.done():
                    try:
                        await asyncio.shield(self.spawn_task)
                    except asyncio.CancelledError:
                        continue
                self.child = self.spawn_task.result()
                raise
            finally:
                self.spawn_task = None
            record = {
                "pid": self.child.pid,
                "instance": instance,
                "identity": await asyncio.to_thread(identity, self.child.pid, instance),
                "birth": await asyncio.to_thread(birth, self.child.pid),
                "status": "running",
                "task_id": self.run.task_id,
                "generation": self.run.generation,
                "pending_file": str(path),
            }
            (self.root / "process.json").write_text(canonical(record))
            await asyncio.wait_for(
                self.child.wait(),
                (body["timeout_seconds"] + 2) if body["timeout_seconds"] is not None else None,
            )
            if self.child.returncode:
                raise Conflict("capture failed; no hardware success inferred")
            record["status"] = "stopped"
            (self.root / "process.json").write_text(canonical(record))
        finally:
            if self.child and self.child.returncode is None:
                self.child.terminate()
                try:
                    await asyncio.wait_for(self.child.wait(), 3)
                except TimeoutError:
                    self.child.kill()
                    await self.child.wait()
            if record is not None and self.child is not None and self.child.returncode is not None:
                record["status"] = "stopped"
                record["worker_returncode"] = self.child.returncode
                (self.root / "process.json").write_text(canonical(record))
            self.child = None
            lease.close()
            private.unlink(missing_ok=True)

    async def _loop(self):
        from .harness_input_store import InputFile

        while not self.closed:
            self.current()
            ident = uuid.uuid4().hex
            path = self.root / (
                ident + (".mkv" if "image" in self.spec.binding.modalities else ".wav")
            )
            started = datetime.now(timezone.utc)
            await self._window(path)
            self.current()
            event = InputFile(
                event_id="capture-" + ident,
                segment_id="capture-" + ident,
                start_ms=0,
                recorded_start_at=started,
                clock_authority="node_reported",
                capture_revision=digest(
                    [[self.run.task_id, self.run.version, self.run.generation]]
                ),
            )
            work = asyncio.create_task(
                asyncio.to_thread(
                    self.media.add_recording,
                    self.run.principal,
                    self.spec.binding.source_ref,
                    event,
                    path,
                    check_active=self.current,
                )
            )
            try:
                result = await asyncio.shield(work)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await asyncio.shield(work)
                raise
            # The committed pool owns a verified copy; this process owns only its staging file.
            path.unlink()
            if result["end_ms"] - result["start_ms"] < self.window_ms * 0.8:
                raise Conflict("capture ended early; partial original is retained")
            self.ready.set()

    async def start(self):
        if not await recover_capture(self.media, self.run.task_id):
            raise Conflict("previous capture ownership unconfirmed")
        self.task = asyncio.create_task(self._loop())
        waiter = asyncio.create_task(self.ready.wait())
        try:
            done, _ = await asyncio.wait(
                {waiter, self.task},
                timeout=self.window_ms / 1000 + 15,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self.task in done:
                self.task.result()
            if waiter not in done:
                raise TimeoutError("capture has not produced a verified first window")
        finally:
            waiter.cancel()
            with suppress(asyncio.CancelledError):
                await waiter

    async def _close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.finalizer:
            await asyncio.shield(self.finalizer)
        return self.child is None and self.spawn_task is None

    async def close(self):
        self.closed = True
        if self.cleanup is None:
            self.cleanup = asyncio.create_task(self._close())
        return await asyncio.shield(self.cleanup)
