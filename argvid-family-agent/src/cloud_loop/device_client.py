"""Device-side camera capture and bounded incremental delivery to a paired Muse input."""

import argparse
import asyncio
import csv
import fcntl
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import signal
import ssl
import subprocess
import time
from urllib.parse import urlsplit
import uuid

import httpx
import imageio_ffmpeg
from pydantic import Field, model_validator

from .contracts import Contract
from .media import file_hash
from .observation_contracts import Reference
from .observation_process import birth, identity


class DeviceClientConfig(Contract):
    endpoint: str
    source_ref: Reference
    key_file: str
    ca_file: str | None = None
    client_id: Reference
    video_name: str
    audio_name: str
    width: int = Field(default=1280, ge=160, le=3840)
    height: int = Field(default=720, ge=120, le=2160)
    fps: int = Field(default=30, ge=1, le=60)
    spool_bytes: int = Field(default=128 * 1024**2, ge=1024**2)
    request_timeout_seconds: int = Field(default=20, ge=1, le=120)
    retry_seconds: float = Field(default=2, gt=0, le=60)
    reconnect: bool = True

    @model_validator(mode="after")
    def local_or_secure(self):
        url = urlsplit(self.endpoint)
        if (
            (
                url.scheme != "https"
                and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost"})
            )
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("use HTTPS or a local test receiver without URL credentials")
        return self


class UploadSpoolFull(RuntimeError):
    pass


def discover_devices():
    if platform.system() != "Darwin":
        raise ValueError("this device client requires macOS AVFoundation")
    process = subprocess.run(
        [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-hide_banner",
            "-f",
            "avfoundation",
            "-list_devices",
            "true",
            "-i",
            "",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    found = {"video": [], "audio": []}
    kind = None
    for line in process.stderr.splitlines():
        if "AVFoundation video devices:" in line:
            kind = "video"
        elif "AVFoundation audio devices:" in line:
            kind = "audio"
        elif kind:
            match = re.search(r"\] \[(\d+)\] (.+)$", line)
            if match:
                found[kind].append({"index": int(match[1]), "name": match[2]})
    return found


def resolve_device(devices, kind, name):
    matches = [item for item in devices[kind] if item["name"] == name]
    if len(matches) != 1:
        raise ValueError(f"saved {kind} device is absent or ambiguous; select the device again")
    return matches[0]["index"]


def capture_command(config, folder, segment_ms, duration, devices):
    video = resolve_device(devices, "video", config.video_name)
    audio = resolve_device(devices, "audio", config.audio_name)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostats",
        "-f",
        "avfoundation",
        "-framerate",
        str(config.fps),
        "-video_size",
        f"{config.width}x{config.height}",
        "-pixel_format",
        "uyvy422",
        "-i",
        f"{video}:{audio}",
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "22",
        "-pix_fmt",
        "yuv420p",
        "-threads",
        "2",
        "-bf",
        "0",
        "-fps_mode",
        "cfr",
        "-r",
        str(config.fps),
        "-force_key_frames",
        f"expr:gte(t,n_forced*{segment_ms / 1000})",
        "-c:a",
        "pcm_s16le",
        "-af",
        "aresample=async=1:first_pts=0,asetnsamples=n=256:p=0",
    ]
    if duration is not None:
        command += ["-t", str(duration)]
    return [
        *command,
        "-f",
        "segment",
        "-segment_time",
        str(segment_ms / 1000),
        "-segment_format",
        "matroska",
        "-segment_format_options",
        "live=1:cluster_time_limit=250:flush_packets=1",
        "-reset_timestamps",
        "0",
        "-segment_list",
        str(folder / "segments.csv"),
        "-segment_list_type",
        "csv",
        str(folder / "part-%06d.mkv"),
    ]


class DeviceClient:
    def __init__(self, config, root):
        self.config, self.root = config, Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.stop = asyncio.Event()
        self.child = None
        self.state = None
        self.connected = False
        self.capture_stop_sent = False
        self.capture_stopper = None
        self.status = {
            "uploaded_bytes": 0,
            "sealed_parts": 0,
            "retries": 0,
            "max_request_seconds": 0,
            "capture_open_count": 0,
        }

    def save_status(self):
        target = self.root / "status.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {**self.status, "updated_at": datetime.now(timezone.utc).isoformat()},
                ensure_ascii=False,
                indent=2,
            )
        )
        temporary.replace(target)

    async def request(self, method, path, **kwargs):
        started = time.monotonic()
        response = await self.http.request(method, path, **kwargs)
        response.raise_for_status()
        self.status["max_request_seconds"] = max(
            self.status["max_request_seconds"], time.monotonic() - started
        )
        return response.json()

    async def heartbeat(self):
        running = self.child is not None and self.child.returncode is None
        self.state = await self.request(
            "POST",
            "heartbeat",
            json={
                "client_id": self.config.client_id,
                "connected": self.connected,
                "recording": running,
                "tracks_active": running,
            },
        )
        return self.state

    def stop_capture(self):
        if self.child and self.child.returncode is None and not self.capture_stop_sent:
            self.capture_stop_sent = True
            self.capture_stopper = asyncio.create_task(self._stop_process(self.child))

    async def _stop_process(self, process):
        try:
            process.stdin.write(b"q\n")
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        try:
            await asyncio.wait_for(process.wait(), 5)
            return
        except TimeoutError:
            self.status["forced_capture_stop"] = True
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 3)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def capture_log(self, reader, path):
        output = path.open("wb")
        try:
            while block := await reader.read(4096):
                if output.tell() + len(block) > 1024 * 1024:
                    output.close()
                    path.replace(path.with_suffix(".previous.log"))
                    output = path.open("wb")
                output.write(block)
        finally:
            output.close()

    async def recover_previous(self):
        record = self.root / "capture-process.json"
        if not record.exists():
            return
        saved = json.loads(record.read_text())
        if saved.get("status") != "running":
            return
        pid, instance = saved["pid"], saved["instance"]
        current = await asyncio.to_thread(identity, pid, instance)
        born = await asyncio.to_thread(birth, pid)
        if current is None or born != saved["birth"]:
            saved["status"] = "previous_process_gone"
        else:
            if current != saved["identity"] or os.getpgid(pid) != pid:
                raise RuntimeError("previous camera process ownership could not be verified")
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(pid, sig)
                except ProcessLookupError:
                    break
                for _ in range(30):
                    if await asyncio.to_thread(identity, pid, instance) != current:
                        break
                    await asyncio.sleep(0.1)
                else:
                    continue
                break
            if await asyncio.to_thread(identity, pid, instance) == current:
                raise RuntimeError("previous camera process still owns its device")
            saved["status"] = "recovered"
            self.status["recovered_previous_capture"] = True
        record.write_text(json.dumps(saved, indent=2))

    async def pulse(self, done):
        while not done.is_set():
            try:
                state = await self.heartbeat()
                if self.child and self.child.returncode is None and not state["active"]:
                    self.stop_capture()
            except (httpx.HTTPError, OSError) as exc:
                self.status["heartbeat_error"] = type(exc).__name__
            try:
                await asyncio.wait_for(done.wait(), 2)
            except TimeoutError:
                pass

    def spool_size(self):
        return sum(path.stat().st_size for path in self.root.glob("session-*/part-*.mkv"))

    async def send_saved(self, recording, folder, began):
        part, offset = 0, 0
        revision = self.state["capture_revision"]
        manifest_offset = 0
        closed = {}
        while True:
            if self.stop.is_set() and self.child.returncode is None:
                self.stop_capture()
            if self.spool_size() > self.config.spool_bytes:
                raise UploadSpoolFull(
                    "upload spool reached its configured limit; unacknowledged files retained"
                )
            manifest = folder / "segments.csv"
            if manifest.exists():
                with manifest.open() as stream:
                    stream.seek(manifest_offset)
                    while line := stream.readline():
                        if not line.endswith("\n"):
                            break
                        manifest_offset += len(line.encode())
                        row = next(csv.reader([line]))
                        closed[Path(row[0]).name] = row
            path = folder / f"part-{part:06d}.mkv"
            metadata = {
                "session_id": recording["session_id"],
                "capture_revision": revision,
                "client_id": self.config.client_id,
                "part": part,
                "offset": offset,
                "recorded_start_at": began,
            }
            if path.exists() and path.stat().st_size > offset:
                with path.open("rb") as source:
                    source.seek(offset)
                    data = source.read(recording["chunk_bytes"])
                try:
                    receipt = await self.request(
                        "POST",
                        "recording/append",
                        content=data,
                        headers={
                            "Content-Type": "application/octet-stream",
                            "x-media-event": json.dumps(metadata),
                        },
                    )
                except (httpx.TransportError, httpx.TimeoutException):
                    if self.stop.is_set():
                        raise
                    self.status["retries"] += 1
                    await asyncio.sleep(self.config.retry_seconds)
                    continue
                if receipt["offset"] != offset + len(data):
                    raise RuntimeError("receiver acknowledged a different recording offset")
                offset += len(data)
                self.status["uploaded_bytes"] += len(data)
                self.save_status()
                continue
            if path.name in closed:
                row = closed[path.name]
                if (float(row[2]) - float(row[1])) * 1000 < recording[
                    "recording_segment_ms"
                ] - 1000 and self.child.returncode is None:
                    await asyncio.sleep(0.1)
                    continue
                final = (
                    self.child.returncode is not None
                    and not (folder / f"part-{part + 1:06d}.mkv").exists()
                )
                seal = {
                    **metadata,
                    "size_bytes": path.stat().st_size,
                    "sha256": await asyncio.to_thread(file_hash, path),
                    "start_ms": round(float(row[1]) * 1000),
                    "end_ms": round(float(row[2]) * 1000),
                    "final": final,
                }
                for attempt in range(3):
                    try:
                        receipt = await self.request("POST", "recording/seal", json=seal)
                        break
                    except (httpx.TransportError, httpx.TimeoutException):
                        if attempt == 2:
                            raise
                        self.status["retries"] += 1
                        await asyncio.sleep(self.config.retry_seconds)
                if receipt["sha256"] != seal["sha256"]:
                    raise RuntimeError("receiver did not confirm the camera file identity")
                with (folder / "receipts.jsonl").open("a") as log:
                    log.write(json.dumps(receipt) + "\n")
                path.unlink()
                closed.pop(path.name)
                self.status["sealed_parts"] += 1
                self.save_status()
                part, offset = part + 1, 0
                continue
            if self.child.returncode is not None:
                if path.exists() and path.stat().st_size:
                    raise RuntimeError("encoder did not close its last segment; bytes retained")
                if self.child.returncode not in {0, 255}:
                    raise RuntimeError("camera process failed; see its local capture log")
                break
            await asyncio.sleep(0.1)
        await self.request(
            "POST",
            "recording/finish",
            json={
                "session_id": recording["session_id"],
                "capture_revision": revision,
                "client_id": self.config.client_id,
            },
        )

    async def session(self, duration=None):
        if self.spool_size() >= self.config.spool_bytes:
            raise UploadSpoolFull("retained upload files exhaust the spool budget")
        devices = await asyncio.to_thread(discover_devices)
        resolve_device(devices, "video", self.config.video_name)
        resolve_device(devices, "audio", self.config.audio_name)
        self.connected = True
        self.status["state"] = "waiting_for_task"
        self.save_status()
        done = asyncio.Event()
        pulse = None
        log_task = None
        try:
            while not self.stop.is_set():
                state = await self.heartbeat()
                if state["active"] and state.get("recording"):
                    break
                await asyncio.sleep(1)
            if self.stop.is_set():
                return
            recording = state["recording"]
            if state["source"]["simulated"]:
                raise ValueError("physical camera cannot be reported through a simulated source")
            if recording["next_part"] or recording["offset"]:
                raise RuntimeError(
                    "receiver already owns unfinished media; reconnect requires its recovery"
                )
            folder = self.root / ("session-" + uuid.uuid4().hex)
            folder.mkdir()
            began = datetime.now(timezone.utc).isoformat()
            command = capture_command(
                self.config, folder, recording["recording_segment_ms"], duration, devices
            )
            instance = "muse_capture=" + uuid.uuid4().hex
            command[-1:-1] = ["-metadata", instance]
            self.child = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            self.capture_stop_sent = False
            self.capture_stopper = None
            log_task = asyncio.create_task(
                self.capture_log(self.child.stderr, folder / "capture.log")
            )
            fingerprint, born = await asyncio.gather(
                asyncio.to_thread(identity, self.child.pid, instance),
                asyncio.to_thread(birth, self.child.pid),
            )
            if fingerprint is None or born is None:
                raise RuntimeError("camera process exited before ownership was recorded")
            owner = {
                "pid": self.child.pid,
                "instance": instance,
                "birth": born,
                "identity": fingerprint,
                "status": "running",
            }
            (self.root / "capture-process.json").write_text(json.dumps(owner, indent=2))
            self.status.update(
                state="streaming",
                capture_open_count=self.status["capture_open_count"] + 1,
                session_id=recording["session_id"],
                folder=str(folder),
            )
            self.save_status()
            pulse = asyncio.create_task(self.pulse(done))
            await self.send_saved(recording, folder, began)
        finally:
            if self.child and self.child.returncode is None:
                self.stop_capture()
            if self.capture_stopper:
                await self.capture_stopper
            if log_task:
                await log_task
            process_record = self.root / "capture-process.json"
            if self.child and self.child.returncode is not None and process_record.exists():
                saved = json.loads(process_record.read_text())
                if saved["pid"] == self.child.pid:
                    saved.update(status="stopped", returncode=self.child.returncode)
                    process_record.write_text(json.dumps(saved, indent=2))
            self.connected = False
            for _ in range(40):
                try:
                    state = await self.heartbeat()
                    if state.get("resources_released"):
                        break
                except httpx.HTTPError:
                    break
                await asyncio.sleep(0.5)
            done.set()
            if pulse:
                await pulse
            self.status.update(
                state="stopped",
                recording=False,
                resources_released=bool(self.state and self.state.get("resources_released")),
            )
            self.save_status()

    async def run(self, duration=None):
        with (self.root / "controller.lock").open("a") as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            await self.recover_previous()
            await self._run(duration)

    async def _run(self, duration=None):
        context = ssl.create_default_context(cafile=self.config.ca_file)
        key = Path(self.config.key_file).read_text().strip()
        async with httpx.AsyncClient(
            base_url=self.config.endpoint.rstrip("/")
            + "/harness/capture/"
            + self.config.source_ref
            + "/",
            headers={"Authorization": "Capture " + key},
            verify=context,
            timeout=self.config.request_timeout_seconds,
        ) as self.http:
            backoff = self.config.retry_seconds
            while not self.stop.is_set():
                try:
                    await self.session(duration)
                    if duration is not None or not self.config.reconnect:
                        return
                    backoff = self.config.retry_seconds
                except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
                    self.status.update(
                        state="disconnected", error_type=type(exc).__name__, error=str(exc)
                    )
                    self.save_status()
                    if (
                        duration is not None
                        or not self.config.reconnect
                        or self.stop.is_set()
                        or isinstance(exc, UploadSpoolFull)
                    ):
                        raise
                if not self.stop.is_set():
                    await asyncio.sleep(backoff)
                    backoff = min(30, backoff * 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["discover", "configure", "run"])
    parser.add_argument("--config", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--endpoint")
    parser.add_argument("--source")
    parser.add_argument("--key-file")
    parser.add_argument("--ca-file")
    parser.add_argument("--video")
    parser.add_argument("--audio")
    parser.add_argument("--duration", type=float)
    args = parser.parse_args()
    if args.action == "discover":
        print(json.dumps(discover_devices(), ensure_ascii=False, indent=2))
        return
    if args.config is None:
        parser.error("--config is required")
    if args.action == "configure":
        devices = discover_devices()
        resolve_device(devices, "video", args.video)
        resolve_device(devices, "audio", args.audio)
        config = DeviceClientConfig(
            endpoint=args.endpoint,
            source_ref=args.source,
            key_file=str(Path(args.key_file).resolve()),
            ca_file=str(Path(args.ca_file).resolve()) if args.ca_file else None,
            client_id="device-" + uuid.uuid4().hex,
            video_name=args.video,
            audio_name=args.audio,
        )
        args.config.parent.mkdir(parents=True, exist_ok=True)
        with args.config.open("x") as output:
            os.chmod(args.config, 0o600)
            output.write(config.model_dump_json(indent=2))
        print("Device selection and pairing references saved")
        return
    if args.state is None or (args.duration is not None and args.duration <= 0):
        parser.error("--state and a positive optional --duration are required")
    config = DeviceClientConfig.model_validate_json(args.config.read_text())
    client = DeviceClient(config, args.state)

    async def run():
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            loop.add_signal_handler(signum, client.stop.set)
        await client.run(args.duration)

    asyncio.run(run())


if __name__ == "__main__":
    main()
