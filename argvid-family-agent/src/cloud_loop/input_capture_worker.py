"""Child-held capture leases; optional bounded PCM fan-out to one speech model."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import math
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--instance", required=True)
    parser.add_argument("--lease-fd", type=int, required=True)
    args = parser.parse_args()
    os.fstat(args.lease_fd)
    body = json.loads(args.config.read_text())
    if body["instance"] != args.instance or not isinstance(body["command"], list):
        raise ValueError("capture worker identity differs")
    capture_until = body.get("capture_until")
    if capture_until is not None and (
        type(capture_until) not in (int, float) or not math.isfinite(capture_until)
        or capture_until <= time.time()
    ):
        raise ValueError("capture deadline must be finite and in the future")
    child = None
    stopped = threading.Event()
    overflow = threading.Event()
    finished = threading.Event()
    pcm = queue.Queue(maxsize=100)  # At most ten seconds of 16 kHz mono PCM.
    voice_thread = None
    voice_result = {}
    read_metrics = {}
    read_progress = {}
    status_lock = threading.Lock()

    def publish_status():
        if not body.get("status_file"):
            return
        with status_lock:
            path = Path(body["status_file"])
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "speech": dict(voice_result),
                        "speech_overrun": overflow.is_set(),
                        "updated_monotonic": time.monotonic(),
                        **read_progress,
                    }
                )
            )
            temporary.replace(path)

    leases = []
    log = None
    received_bytes = dropped_bytes = 0
    stop_reason = None
    stop_requested_at = None
    forced_kill = False

    def stop(signum=None, frame=None):
        nonlocal stop_reason, stop_requested_at
        if stop_requested_at is None:
            stop_requested_at = time.time()
            stop_reason = "signal" if signum is not None else "cleanup"
        stopped.set()
        if child is not None and child.poll() is None:
            child.terminate()

    def pcm_source(config, root, ignored_stop):
        while True:
            if overflow.is_set():
                raise RuntimeError("shared PCM consumer fell behind; no partial command submitted")
            if stopped.is_set():
                return
            try:
                chunk = pcm.get(timeout=0.1)
            except queue.Empty:
                if finished.is_set():
                    return
                continue
            yield chunk

    def speech():
        try:
            from .voice_stream import VoiceStreamConfig, run

            cfg = VoiceStreamConfig.model_validate(body["voice_config"])
            voice_result.update(run(cfg, stopped, pcm_source=pcm_source))
        except BaseException as exc:
            voice_result["error_type"] = type(exc).__name__
        finally:
            voice_result["finished"] = True
            publish_status()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    started = time.monotonic()
    try:
        for filename in body.get("hardware_leases", []):
            path = Path(filename)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.resolve() != path:
                raise ValueError("capture device lease cannot be redirected")
            handle = path.open("a")
            leases.append(handle)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if body.get("stderr_file"):
            log = Path(body["stderr_file"]).open("xb")
        shared = bool(body.get("voice_config"))
        child = subprocess.Popen(
            body["command"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log or subprocess.DEVNULL,
        )
        if shared:
            body["voice_config"].update(
                recording_started_at=datetime.now(timezone.utc).isoformat(),
                recording_started_monotonic=time.monotonic(),
            )
            voice_thread = threading.Thread(target=speech, daemon=True)
            voice_thread.start()
        buffer = bytearray()
        read_thread = None

        def read_saved():
            from .recorded_audio import saved_pcm

            nonlocal received_bytes, dropped_bytes

            def progress(name):
                read_progress["last_read_file"] = name
                publish_status()

            try:
                for data in saved_pcm(
                    body["recording_folder"],
                    lambda: child.poll() is not None,
                    stopped,
                    read_metrics,
                    progress,
                ):
                    received_bytes += len(data)
                    buffer.extend(data)
                    while len(buffer) >= 3200:
                        block = bytes(buffer[:3200])
                        del buffer[:3200]
                        try:
                            if overflow.is_set() or voice_result.get("finished"):
                                raise queue.Full
                            pcm.put_nowait(block)
                        except queue.Full:
                            overflow.set()
                            dropped_bytes += len(block)
                            publish_status()
                if buffer and not overflow.is_set():
                    pcm.put_nowait(bytes(buffer))
            except BaseException as exc:
                read_progress["reader_error_type"] = type(exc).__name__
                overflow.set()
            finally:
                finished.set()
                publish_status()

        if shared:
            if not body.get("recording_folder"):
                raise ValueError("shared speech requires saved original recordings")
            read_thread = threading.Thread(target=read_saved, daemon=True)
            read_thread.start()
        while child.poll() is None:
            if capture_until is not None and time.time() >= capture_until and not stopped.is_set():
                stop()
                stop_reason = "capture_deadline"
            if stopped.is_set():
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    forced_kill = True
                    child.kill()
                break
            timeout = body.get("timeout_seconds")
            if timeout is not None and time.monotonic() - started > timeout:
                raise TimeoutError("capture worker exceeded configured duration")
            stopped.wait(0.05)
        child.wait(timeout=2)
        if shared:
            read_thread.join(timeout=15)
            finished.set()
            voice_thread.join(timeout=15)
        # This is the worker's intentional deadline receipt, not a claim that
        # the encoder wrote a trailer. Its actual exit code is retained below.
        return 0 if stop_reason == "capture_deadline" else child.returncode
    finally:
        stop()
        if child is not None:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                forced_kill = True
                child.kill()
                child.wait()
            if child.stdout:
                child.stdout.close()
        finished.set()
        if "read_thread" in locals() and read_thread and read_thread.is_alive():
            read_thread.join(timeout=2)
        if voice_thread and voice_thread.is_alive():
            voice_thread.join(timeout=2)
        if log:
            log.close()
        if body.get("result_file"):
            Path(body["result_file"]).write_text(
                json.dumps(
                    {
                        "capture_returncode": child.returncode if child else None,
                        "input_open_count": int(child is not None),
                        "pcm_received_bytes": received_bytes,
                        "pcm_dropped_bytes": dropped_bytes,
                        "speech_overrun": overflow.is_set(),
                        "speech": voice_result,
                        "speech_thread_finished": not voice_thread or not voice_thread.is_alive(),
                        "stopped": stopped.is_set(),
                        "stop_reason": stop_reason,
                        "stop_requested_at": stop_requested_at,
                        "forced_kill": forced_kill,
                        "saved_recording_read": read_metrics,
                        "elapsed_seconds": time.monotonic() - started,
                        "saved_first": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        for handle in leases:
            handle.close()
        os.close(args.lease_fd)


if __name__ == "__main__":
    sys.exit(main())
