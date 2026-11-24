"""Optional local file-ASR adapter; outputs the same utterance contract as a future microphone.

Run in an environment with mlx-whisper==0.4.3. The core/board service does not
import MLX. Audio stays on the Mac; only text and provenance cross the node link.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import io
import json
import math
import os
import platform
import threading
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

from .contracts import digest

_asr_lock = threading.RLock()
_asr_directory_hashes = {}
_asr_cached_model = None


def _signature(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _file_snapshot(path):
    """Hash bounded chunks while checking that the named file stayed the same."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        before = _signature(os.fstat(stream.fileno()))
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after = _signature(os.fstat(stream.fileno()))
    if before != after or after != _signature(path.stat()):
        raise ValueError("ASR input changed while being verified")
    return digest.hexdigest(), before


def _model_snapshot(path):
    # Match the pinned SDK loader's actual file preference exactly.
    weight = path / "weights.safetensors"
    if not weight.is_file():
        weight = path / "weights.npz"
    files = [path / "config.json", weight]
    if not all(file.is_file() for file in files):
        raise ValueError("prepared ASR model is incomplete")
    return {file.name: _file_snapshot(file) for file in files}


def wait_for_reply(
    client, base_url, event_id, initial, *, timeout=180, render=print, render_notice=print
):
    """The file replay output adapter displays text before acknowledging it.

    This polls a receipt, never resubmits a user instruction. Without a successful
    render/ACK the edge followup gate remains closed. No TTS/speaker is implied.
    """
    if type(timeout) not in {float, int} or not math.isfinite(timeout) or not 0 < timeout <= 600:
        raise ValueError("reply timeout must be between 0 and 600 seconds")
    base_url = base_url.rstrip("/")
    deadline = time.monotonic() + timeout
    current = initial
    while True:
        if current.get("event_id") != event_id:
            raise ValueError("voice receipt identity mismatch")
        if current.get("status") == "delivered":
            return current
        if current.get("status") in {"ignored", "expired", "rejected", "failed", "legacy_local"}:
            if current["status"] in {"expired", "rejected", "failed"}:
                fallback = {
                    "expired": "这条输入在首次受理前已过期，未执行；请重新发出。",
                    "rejected": "这条输入未被受理，请检查输入状态。",
                    "failed": "输入投递或处理失败，请先查看原有状态。",
                }
                render_notice(current.get("text") or fallback[current["status"]])
                return {**current, "notice_displayed": True}
            return current
        if current.get("reply") is not None:
            reply = current["reply"]
            if (
                not isinstance(reply, dict)
                or reply.get("event_id") != event_id
                or not isinstance(reply.get("text"), str)
                or not current.get("reply_digest")
            ):
                raise ValueError("voice reply is incomplete")
            render(reply["text"])
            response = client.post(
                base_url + "/harness/voice/delivered",
                json={"event_id": event_id, "reply_digest": current["reply_digest"]},
                timeout=5,
            )
            response.raise_for_status()
            acknowledged = response.json()
            if acknowledged.get("event_id") != event_id:
                raise ValueError("voice delivery identity mismatch")
            return acknowledged
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            render_notice("本次等待已结束，尚未取得回复；请查询这条输入的原有状态。")
            return {
                **current,
                "reply_wait_timed_out": True,
                "audio_generated": False,
                "notice_displayed": True,
            }
        time.sleep(min(0.2, remaining))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        response = client.get(
            base_url + "/harness/voice/events/" + event_id, timeout=min(5, remaining)
        )
        response.raise_for_status()
        current = response.json()


def display_notifications(client, base_url, origin_event_id, *, render=print):
    """Display a bounded page of secondary results; do not reopen voice input."""
    base_url = base_url.rstrip("/")
    response = client.get(
        base_url + "/harness/voice/notifications",
        params={"origin_event_id": origin_event_id},
        timeout=5,
    )
    response.raise_for_status()
    items = response.json()
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError("invalid local notification page")
    shown = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid local voice notification")
        reply = item.get("reply")
        if (
            item.get("origin_event_id") != origin_event_id
            or item.get("status") != "reply_ready"
            or not isinstance(reply, dict)
            or reply.get("event_id") != item.get("event_id")
            or not isinstance(reply.get("text"), str)
            or not item.get("reply_digest")
            or digest(reply) != item["reply_digest"]
            or item.get("opens_followup_window") is not False
        ):
            raise ValueError("invalid local voice notification identity")
        render(reply["text"])
        ack = client.post(
            base_url + "/harness/voice/delivered",
            json={"event_id": item["event_id"], "reply_digest": item["reply_digest"]},
            timeout=5,
        )
        ack.raise_for_status()
        receipt = ack.json()
        if (
            not isinstance(receipt, dict)
            or receipt.get("event_id") != item["event_id"]
            or receipt.get("status") != "delivered"
            or receipt.get("opens_followup_window") is not False
        ):
            raise ValueError("notification display acknowledgement mismatch")
        shown.append(receipt)
    return shown


def transcribe_file(
    path,
    *,
    model="mlx-community/whisper-small-mlx",
    model_dir=None,
    model_revision=None,
    wake_word="Muse",
    recorded_at=None,
    event_id=None,
):
    global _asr_cached_model
    import mlx_whisper
    import numpy as np
    from mlx_whisper.transcribe import ModelHolder

    from .harness_contracts import VoiceUtterance

    path = Path(path).resolve()
    received = datetime.now(timezone.utc)
    before_audio = _signature(path.stat())
    with path.open("rb") as source:
        audio_bytes = source.read(2 * 1024 * 1024 + 1)
    if len(audio_bytes) > 2 * 1024 * 1024:
        raise ValueError("prepared 60-second WAV exceeds the bounded input size")
    if _signature(path.stat()) != before_audio:
        raise ValueError("ASR audio changed while being read")
    audio_hash = hashlib.sha256(audio_bytes).hexdigest()
    with wave.open(io.BytesIO(audio_bytes), "rb") as stream:
        if (
            stream.getnchannels() != 1
            or stream.getframerate() != 16000
            or stream.getsampwidth() != 2
        ):
            raise ValueError("adapter requires 16kHz mono 16-bit WAV; keep original separately")
        duration = stream.getnframes() / stream.getframerate()
        if not 0 < duration <= 60:
            raise ValueError("file utterance must be between 0 and 60 seconds")
        samples = stream.readframes(stream.getnframes())
        if len(samples) != stream.getnframes() * 2:
            raise ValueError("WAV data is shorter than its declared sample count")
        audio = np.frombuffer(samples, dtype="<i2").astype(np.float32) / 32768
    # No silent model download and no upload of user audio. Provision model
    # files separately; missing local weights produce an explicit failure.
    if model_dir is not None:
        model_path = Path(model_dir).resolve(strict=True)
        snapshot = None
    else:
        from huggingface_hub import snapshot_download

        model_path = Path(
            snapshot_download(
                model,
                revision=model_revision,
                local_files_only=True,
                allow_patterns=["config.json", "*.safetensors", "*.npz"],
            )
        )
        snapshot = model_path.name
    hint = f"系统唤醒词的英文拼写为 {wake_word}。" if wake_word else None
    started = time.monotonic()
    with _asr_lock:
        before_model = _model_snapshot(model_path)
        hashes = {name: record[0] for name, record in before_model.items()}
        previous = _asr_directory_hashes.get(str(model_path))
        if previous is not None and previous != hashes:
            raise ValueError("ASR model directory changed; select an immutable revision directory")
        reused = (
            ModelHolder.model is not None
            and ModelHolder.model is _asr_cached_model
            and ModelHolder.model_path == str(model_path)
            and previous == hashes
        )
        # SDK 0.4.3 caches only by path. An untracked cache cannot prove which
        # bytes it loaded. Reload it once; known unchanged content stays warm.
        if not reused:
            ModelHolder.model = None
            ModelHolder.model_path = None
            _asr_cached_model = None
        try:
            result = mlx_whisper.transcribe(
                audio,
                path_or_hf_repo=str(model_path),
                language="zh",
                temperature=0,
                condition_on_previous_text=False,
                initial_prompt=hint,
                word_timestamps=True,
                verbose=None,
            )
            if _model_snapshot(model_path) != before_model:
                raise ValueError("ASR model changed during transcription")
            if _file_snapshot(path) != (audio_hash, before_audio):
                raise ValueError("ASR audio changed during transcription")
            if ModelHolder.model is None or ModelHolder.model_path != str(model_path):
                raise ValueError("ASR model cache changed during transcription")
            _asr_directory_hashes[str(model_path)] = hashes
            _asr_cached_model = ModelHolder.model
        except BaseException:
            ModelHolder.model = None
            ModelHolder.model_path = None
            _asr_cached_model = None
            raise
    provenance = {
        "provider": "mlx-whisper",
        "version": importlib.metadata.version("mlx-whisper"),
        "model": model,
        "model_snapshot": snapshot,
        "declared_model_revision": model_revision,
        "model_storage": "prepared_directory" if model_dir is not None else "local_hub_cache",
        "model_files": hashes,
        "model_cache_reused": reused,
        "input_bytes_verified_unchanged": True,
        "processing_host": platform.node(),
        "audio_uploaded": False,
        "recognition_simulated": False,
        "initial_prompt": hint,
        "language": "zh",
        "temperature": 0,
        "duration_seconds": duration,
        "elapsed_seconds": time.monotonic() - started,
    }
    utterance = VoiceUtterance(
        event_id=event_id or "asr-" + uuid.uuid4().hex,
        transcript=result["text"].strip(),
        source_ref="file:" + path.name,
        received_at=datetime.now(timezone.utc),
        recorded_at=recorded_at,
        replayed_at=received,
        audio_sha256=audio_hash,
        asr=provenance,
    )
    return {
        "utterance": utterance.model_dump(mode="json"),
        "asr_raw_result": result,
        "boundary": "real local file ASR; replay input and text wake gate are simulated, not live acoustic wake detection",
        "recorded_at_known": recorded_at is not None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wav", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", default="mlx-community/whisper-small-mlx")
    parser.add_argument("--wake-word", default="Muse")
    parser.add_argument(
        "--model-dir", type=Path, help="Prepared local weights; never downloads implicitly"
    )
    parser.add_argument(
        "--model-revision",
        help="Pinned Hub revision or declared source revision for prepared weights",
    )
    parser.add_argument("--recorded-at", type=datetime.fromisoformat)
    parser.add_argument("--event-id")
    parser.add_argument("--send-to")
    parser.add_argument("--node-token-file", type=Path)
    parser.add_argument(
        "--wait-reply",
        action="store_true",
        help="Display returned text, then acknowledge delivery to open followup",
    )
    parser.add_argument("--reply-timeout", type=float, default=180)
    parser.add_argument(
        "--watch-notifications-seconds",
        type=float,
        default=0,
        help="After the first reply, display later task notifications for a bounded time; no TTS",
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; use a new evidence path")
    if bool(args.send_to) != bool(args.node_token_file):
        parser.error("send-to and node-token-file must be provided together")
    if args.wait_reply and not args.send_to:
        parser.error("wait-reply requires send-to")
    if (
        not math.isfinite(args.watch_notifications_seconds)
        or not 0 <= args.watch_notifications_seconds <= 600
    ):
        parser.error("watch-notifications-seconds must be between 0 and 600")
    if args.watch_notifications_seconds and not args.wait_reply:
        parser.error("watch-notifications-seconds requires wait-reply")
    if not math.isfinite(args.reply_timeout) or not 0 < args.reply_timeout <= 600:
        parser.error("reply-timeout must be between 0 and 600 seconds")
    if args.send_to:
        from urllib.parse import urlparse

        url = urlparse(args.send_to)
        if (
            url.scheme != "http"
            or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username
            or url.password
        ):
            parser.error("send-to must be a loopback node or SSH tunnel HTTP URL")
    value = transcribe_file(
        args.wav,
        model=args.model,
        model_dir=args.model_dir,
        model_revision=args.model_revision,
        wake_word=args.wake_word,
        recorded_at=args.recorded_at,
        event_id=args.event_id,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    if args.send_to:
        import httpx

        token = args.node_token_file.expanduser().read_text().strip()
        with httpx.Client(trust_env=False, timeout=20) as client:
            response = client.post(
                args.send_to.rstrip("/") + "/harness/voice/file-asr",
                json=value["utterance"],
                headers={"Authorization": "Bearer " + token},
            )
            response.raise_for_status()
            value["submission"] = response.json()
            args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            if args.wait_reply:
                client.headers["Authorization"] = "Bearer " + token
                value["reply_delivery"] = wait_for_reply(
                    client,
                    args.send_to,
                    value["utterance"]["event_id"],
                    value["submission"],
                    timeout=args.reply_timeout,
                    render=lambda text: print(text, flush=True),
                )
                if args.watch_notifications_seconds:
                    until = time.monotonic() + args.watch_notifications_seconds
                    value["notification_deliveries"] = []
                    while time.monotonic() < until:
                        value["notification_deliveries"].extend(
                            display_notifications(
                                client,
                                args.send_to,
                                value["utterance"]["event_id"],
                                render=lambda text: print(text, flush=True),
                            )
                        )
                        args.output.write_text(
                            json.dumps(value, ensure_ascii=False, indent=2) + "\n"
                        )
                        time.sleep(min(0.5, max(0, until - time.monotonic())))
        args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "transcript": value["utterance"]["transcript"],
                "output": str(args.output.resolve()),
                "submitted": "submission" in value,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
