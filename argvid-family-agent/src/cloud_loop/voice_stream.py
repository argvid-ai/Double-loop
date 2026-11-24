"""Configured recording -> one streaming ASR -> installed edge voice ingress.

File replay is explicit. Microphone capture is opt-in and owned by this process.
No local keyword result directly executes a task or selects a cloud conversation.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
import re
import select
import shutil
import signal
import subprocess
import threading
import time
import uuid
import wave
from datetime import datetime, timedelta, timezone
from contextlib import ExitStack
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field, field_validator, model_validator

from .contracts import Contract, InteractionSettings, digest
from .harness_asr import wait_for_reply
from .voice_contracts import VoiceUtterance
from .streaming_asr import StreamingRecognizer, StreamingRecognizerConfig


class VoiceStreamConfig(Contract):
    recognizer: StreamingRecognizerConfig
    source_ref: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$")
    source_kind: Literal["file", "microphone"]
    shared_capture: bool = False
    recording_started_at: datetime | None = None
    recording_started_monotonic: float | None = None
    max_command_lag_seconds: float = Field(default=15, gt=0, le=300)
    input_file: str | None = None
    capture_driver: Literal["alsa", "avfoundation"] | None = None
    capture_device: str | None = None
    capture_seconds: int | None = Field(default=None, strict=True, ge=1, le=3600)
    edge_url: str
    node_token_file: str
    output_dir: str
    paced_replay: bool = True
    wait_replies: bool = False
    quota_bytes: int = Field(default=128 * 1024**2, strict=True, ge=1024 * 1024, le=1024**3)
    min_free_bytes: int = Field(default=64 * 1024**2, strict=True, ge=1024 * 1024)

    @field_validator("edge_url")
    @classmethod
    def local_node(cls, value):
        return InteractionSettings.loopback(value)

    @field_validator("node_token_file", "output_dir", "input_file")
    @classmethod
    def absolute_path(cls, value):
        if value is not None and not Path(value).is_absolute():
            raise ValueError("voice paths must be explicit absolute paths")
        return value

    @model_validator(mode="after")
    def source(self):
        if self.shared_capture:
            if self.input_file or self.capture_driver or self.capture_device:
                raise ValueError(
                    "shared speech consumes its recorder PCM; it cannot open another input"
                )
            return self
        if self.source_kind == "file":
            if not self.input_file or self.capture_driver or self.capture_device:
                raise ValueError("file replay requires only an input file")
        else:
            pattern = (
                r"(?:default|hw:[0-9]+(?:,[0-9]+)?)"
                if self.capture_driver == "alsa"
                else r"none:[0-9]+"
            )
            if (
                self.input_file
                or not self.capture_driver
                or not self.capture_device
                or not re.fullmatch(pattern, self.capture_device)
            ):
                raise ValueError("microphone needs an explicit ALSA or AVFoundation audio device")
        return self


def save_json(path, value):
    with path.open("x") as output:
        path.chmod(0o600)
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def chunks(config, root, stop):
    if config.source_kind == "file":
        path = Path(config.input_file)
        before = path.stat()
        with wave.open(str(path), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                raise ValueError("replay requires mono PCM16 WAV at 16kHz")
            started, count = time.monotonic(), 0
            while not stop.is_set() and (pcm := audio.readframes(1600)):
                yield pcm
                count += len(pcm) // 2
                if config.paced_replay:
                    stop.wait(max(0, started + count / 16000 - time.monotonic()))
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (
            after.st_size,
            after.st_mtime_ns,
            after.st_ino,
        ):
            raise ValueError("replay file changed during input")
        return
    import imageio_ffmpeg

    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-f",
        config.capture_driver,
        "-i",
        config.capture_device,
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        "-f",
        "s16le",
        "pipe:1",
    ]
    with (root / "capture.stderr").open("xb") as log:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=log, stdin=subprocess.DEVNULL
        )
        save_json(root / "capture.json", {"pid": process.pid, "command": command})
        started, buffer = time.monotonic(), bytearray()
        try:
            while not stop.is_set():
                if config.capture_seconds and time.monotonic() - started >= config.capture_seconds:
                    break
                if not select.select([process.stdout], [], [], 0.2)[0]:
                    continue
                data = os.read(process.stdout.fileno(), 3200)
                if not data:
                    raise RuntimeError("microphone stream ended; capture is not confirmed")
                buffer.extend(data)
                while len(buffer) >= 3200:
                    yield bytes(buffer[:3200])
                    del buffer[:3200]
            if len(buffer) % 2:
                raise ValueError("capture ended with an incomplete PCM sample")
            if buffer:
                yield bytes(buffer)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            process.stdout.close()
            save_json(
                root / "capture-closed.json", {"pid": process.pid, "returncode": process.returncode}
            )


def send_event(config, path):
    """Persist before posting; network retries always reuse identical event bytes."""
    body = json.loads(path.read_text())
    token = Path(config.node_token_file).read_text().strip()
    if not token:
        raise ValueError("node credential is empty")
    with httpx.Client(
        base_url=config.edge_url,
        headers={"Authorization": "Bearer " + token},
        timeout=5,
        trust_env=False,
    ) as client:
        error = None
        for attempt in range(3):
            try:
                response = client.post("/harness/voice/streaming-asr", json=body)
                response.raise_for_status()
                result = response.json()
                if result.get("event_id") != body["event_id"]:
                    raise ValueError("edge receipt has a different event identity")
                save_json(path.with_suffix(".receipt.json"), result)
                save_json(
                    path.with_suffix(".delivery-timing.json"),
                    {
                        "edge_received_at": datetime.now(timezone.utc).isoformat(),
                        "receipt_monotonic": time.monotonic(),
                    },
                )
                return result
            except httpx.TransportError as exc:
                error = exc
                if attempt < 2:
                    time.sleep(0.25 * (attempt + 1))
        raise RuntimeError("voice delivery remains unconfirmed; original event retained") from error


def run(config, stop=None, *, recognizer_factory=StreamingRecognizer, pcm_source=None):
    if bool(pcm_source) != config.shared_capture:
        raise ValueError("shared capture requires an internal PCM producer")
    # One installed source has one local capture owner, irrespective of run folder.
    lock = Path(config.node_token_file).parent / ("voice-stream-" + config.source_ref + ".lock")
    if lock.resolve() != lock:
        raise ValueError("voice capture lease cannot be redirected")
    with lock.open("a") as handle:
        lock.chmod(0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("this streaming voice source already has a capture owner") from exc
        with ExitStack() as stack:
            if config.source_kind == "microphone" and not config.shared_capture:
                root = Path(config.node_token_file).parent / "edge" / "capture-devices"
                root.mkdir(parents=True, exist_ok=True)
                path = root / (digest([config.capture_driver, config.capture_device]) + ".lock")
                if path.resolve() != path:
                    raise ValueError("microphone device lease cannot be redirected")
                device = stack.enter_context(path.open("a"))
                fcntl.flock(device, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return _run(config, stop, recognizer_factory=recognizer_factory, pcm_source=pcm_source)


def _run(config, stop=None, *, recognizer_factory=StreamingRecognizer, pcm_source=None):
    stop = stop or threading.Event()
    root = Path(config.output_dir)
    if root.resolve() != root or root.exists():
        raise ValueError("use a new, non-redirected evidence directory")
    root.mkdir(parents=True, mode=0o700)
    # Establish source authority before opening any microphone or loading a model.
    token = Path(config.node_token_file).read_text().strip()
    if not token:
        raise ValueError("node credential is empty")
    with httpx.Client(
        headers={"Authorization": "Bearer " + token}, timeout=5, trust_env=False
    ) as client:
        response = client.get(config.edge_url.rstrip("/") + "/harness/voice/streaming-asr/config")
        response.raise_for_status()
        adapter = response.json()
        if (
            not adapter.get("available")
            or adapter.get("sources", {}).get(config.source_ref) != config.source_kind
        ):
            raise ValueError("edge has not installed this streaming interaction source")
    engine = recognizer_factory(config.recognizer)
    started = config.recording_started_at or datetime.now(timezone.utc)
    mono_started = time.monotonic()
    capture_mono = config.recording_started_monotonic or mono_started
    save_json(
        root / "run.json",
        {
            "source_ref": config.source_ref,
            "source_kind": config.source_kind,
            "source_simulated": config.source_kind == "file",
            "recognition_simulated": False,
            "model": engine.model,
            "model_sha256": engine.model_sha256,
            "model_loaded_once": True,
            "started_at": started.isoformat(),
            "config": config.model_dump(mode="json", exclude={"node_token_file"}),
        },
    )
    events, futures, frames, error = [], [], 0, None
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def completed(segment):
        if segment is None or not segment.pcm:
            return
        used = sum(p.stat().st_size for p in root.iterdir() if p.is_file())
        if used + len(segment.pcm) + 65536 > config.quota_bytes or shutil.disk_usage(
            root
        ).free < config.min_free_bytes + len(segment.pcm):
            raise RuntimeError("voice evidence quota reached; no existing recording is deleted")
        ident = "stream-" + uuid.uuid4().hex
        audio = root / (ident + ".wav")
        with wave.open(str(audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(segment.pcm)
        audio.chmod(0o600)
        record = {
            "transcript": segment.text,
            "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
            "start_frame": segment.start_frame,
            "end_frame": segment.end_frame,
            "reason": segment.reason,
            "audio_file": audio.name,
            "submission_cancelled": stop.is_set(),
            "recognized_at": datetime.now(timezone.utc).isoformat(),
            "recognition_completed_monotonic": time.monotonic(),
            "audio_end_to_recognition_seconds": time.monotonic()
            - capture_mono
            - segment.end_frame / 16000,
            "stale_for_execution": config.source_kind == "microphone"
            and time.monotonic() - capture_mono - segment.end_frame / 16000
            > config.max_command_lag_seconds,
        }
        save_json(root / (ident + ".recognition.json"), record)
        if not segment.text or stop.is_set() or record["stale_for_execution"]:
            return
        utterance = VoiceUtterance(
            event_id=ident,
            transcript=segment.text,
            source_ref=config.source_ref,
            received_at=datetime.now(timezone.utc),
            recorded_at=(started + timedelta(seconds=segment.start_frame / 16000))
            if config.source_kind == "microphone"
            else None,
            replayed_at=started if config.source_kind == "file" else None,
            audio_sha256=record["audio_sha256"],
            asr=engine.proof(segment, config.source_kind).model_dump(),
        )
        path = root / (ident + ".utterance.json")
        save_json(path, utterance.model_dump(mode="json"))
        if sum(not f.done() for f in futures) >= 8:
            raise RuntimeError("delivery backlog reached bound; unsubmitted event retained")
        events.append(path)
        futures.append(pool.submit(send_event, config, path))

    source = (pcm_source or chunks)(config, root, stop)
    try:
        for pcm in source:
            if any(f.done() and f.exception() is not None for f in futures):
                raise RuntimeError("voice delivery failed; capture stopped, event retained")
            frames += len(pcm) // 2
            completed(engine.feed(pcm))
        completed(engine.finish())
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        source.close()
        pool.shutdown(wait=True)
        save_json(
            root / "finished.json",
            {
                "audio_frames": frames,
                "audio_seconds": frames / 16000,
                "elapsed_seconds": time.monotonic() - mono_started,
                "stopped": stop.is_set(),
                "capture_closed": True,
                "error_type": error,
                "delivery_failures": [
                    type(f.exception()).__name__ for f in futures if f.exception()
                ],
            },
        )
    receipts = [future.result() for future in futures]
    if config.wait_replies:
        token = Path(config.node_token_file).read_text().strip()
        with httpx.Client(
            headers={"Authorization": "Bearer " + token}, timeout=5, trust_env=False
        ) as client:
            for path, receipt in zip(events, receipts, strict=True):
                ident = json.loads(path.read_text())["event_id"]
                delivery = wait_for_reply(client, config.edge_url, ident, receipt)
                save_json(root / (ident + ".delivery.json"), delivery)
    return {"output_dir": str(root), "events": len(events), "audio_frames": frames}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = VoiceStreamConfig.model_validate_json(args.config.read_text())
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    print(json.dumps(run(config, stop), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
