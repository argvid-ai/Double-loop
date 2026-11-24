"""Read only the configured recorder's fresh audio. Never open a microphone."""

import hashlib
import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import av

from .harness_contracts import VoiceUtterance
from .recorded_audio import GrowingRecording
from .streaming_asr import StreamingRecognizer


def capture_snapshot(edge_state, source_ref, principal):
    """Bounded read-only adapter for the existing local recorder registry."""
    edge_state = Path(edge_state).resolve()
    with sqlite3.connect((edge_state / "edge.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("""SELECT t.* FROM observation_defaults d JOIN observation_tasks t
            ON t.task_id=json_extract(d.body,'$.task_id') AND t.principal=d.principal
            WHERE d.principal=? AND json_extract(t.config,'$.source_ref')=?""", (principal, source_ref)).fetchone()
        if row is None or row["status"] != "running":
            return None
        source = db.execute("SELECT spec FROM managed_inputs WHERE id=? AND principal=?",
                            (source_ref, principal)).fetchone()
        spec = json.loads(source[0])
        if spec["binding"].get("simulated") or spec.get("capture", {}).get("driver") not in {"v4l2", "alsa"}:
            raise ValueError("not_an_authorized_live_microphone")
        entry = db.execute("""SELECT body FROM managed_input_entries WHERE principal=?
            AND source_ref=? AND path IS NOT NULL ORDER BY sequence DESC LIMIT 1""",
                           (principal, source_ref)).fetchone()
    root = edge_state / "input-capture" / row["task_id"]
    process = json.loads((root / "process.json").read_text())
    folder = Path(process["pending_file"]).parent
    if process.get("status") != "running" or process["generation"] != row["generation"]:
        return None
    if folder.resolve() != folder or not folder.is_relative_to(root) or not entry:
        raise ValueError("recording_path_unavailable")
    body = json.loads(entry[0])
    if body.get("segment_id") != folder.name or body.get("file_timestamps") != "absolute":
        return None
    started = datetime.fromisoformat(body["recorded_start_at"]).timestamp() - body["file_start_ms"] / 1000
    return {"task_id": row["task_id"], "generation": row["generation"], "folder": folder,
            "started": started, "latest_audio_at": started + body["end_ms"] / 1000}


class LiveVoiceInput:
    def __init__(self, runtime, *, recognizer=StreamingRecognizer):
        self.runtime, self.recognizer_factory = runtime, recognizer
        self.state = {"state": "starting", "ready": False, "last_audio_at": None, "error": None}
        self.engine = None
        self.configuration_key = None
        self.discarded = 0

    def reset(self):
        if self.engine is not None:
            self.engine.recognizer.reset(self.engine.stream)
            self.engine.pcm.clear()

    def ready_to_listen(self):
        return (self.runtime.preferences()["enabled"]
                and not self.runtime.speaking.is_set() and time.time() >= self.runtime.listen_after
                and self.runtime.configuration_ready)

    def feed(self, pcm, recorded_at):
        self.state["last_audio_at"] = recorded_at
        if not self.ready_to_listen() or abs(time.time() - recorded_at) > self.runtime.config.max_input_lag_seconds:
            self.reset()
            return
        for offset in range(0, len(pcm), 3200):
            result = self.engine.feed(pcm[offset:offset + 3200])
            if result is None or not result.text.strip():
                continue
            admitted = self.runtime.should_admit(result.text)
            self.state["last_recognition"] = {
                "transcript": result.text[:500], "recognized_at": time.time(),
                "recorded_at": recorded_at, "admitted": admitted,
                "wake_word": self.runtime.voice.match(result.text, self.runtime.wake_words)[0],
            }
            if not admitted:
                self.discarded += 1
                continue
            # The digest is of the actual samples consumed, not a model claim.
            utterance = VoiceUtterance(
                event_id="board-voice-" + uuid.uuid4().hex, transcript=result.text,
                source_ref=self.runtime.config.interaction_source,
                received_at=datetime.now(timezone.utc),
                recorded_at=datetime.fromtimestamp(recorded_at - len(result.pcm) / 32000, timezone.utc),
                audio_sha256=hashlib.sha256(result.pcm).hexdigest(),
                asr=self.engine.proof(result, "microphone").model_dump(),
            )
            self.runtime.admit_live(utterance)

    def consume(self, capture):
        folder = capture["folder"]
        files = sorted(folder.glob("part-*.mkv"))
        if not files:
            return
        path = files[-1]  # Never replay old files as fresh commands after startup.
        while not self.runtime.stopping.is_set():
            if path.resolve() != path or path.parent != folder:
                raise ValueError("recording_file_redirected")
            following = path.with_name(f"part-{int(path.stem.split('-')[1]) + 1:06d}.mkv")
            metrics = {"sync_calls": 0, "sync_seconds": 0.0, "max_sync_seconds": 0.0, "recording_bytes_read": 0}
            checked = [0.0]

            def active():
                if time.monotonic() - checked[0] < 2:
                    return
                checked[0] = time.monotonic()
                now = capture_snapshot(self.runtime.config.edge_state, self.runtime.config.recording_source,
                                       self.runtime.config.principal)
                if (not now or now["folder"] != folder or
                        not self.runtime.preferences()["enabled"] or
                        self.configuration_key != tuple(self.runtime.wake_words)):
                    raise RuntimeError("live_input_changed")
                if time.time() - now["latest_audio_at"] > self.runtime.config.max_input_lag_seconds:
                    raise RuntimeError("recording_audio_stale")

            with GrowingRecording(path, following.exists, self.runtime.stopping, metrics, active) as source:
                with av.open(source, format="matroska", options={"probesize": "32", "analyzeduration": "0"}) as media:
                    if not media.streams.audio:
                        raise ValueError("recording_has_no_audio")
                    converter = av.AudioResampler(format="s16", layout="mono", rate=16000)
                    for frame in media.decode(audio=0):
                        active()
                        if frame.pts is None:
                            continue
                        recorded_at = capture["started"] + float(frame.pts * frame.time_base)
                        # Skip history before sending anything through recognition.
                        if abs(time.time() - recorded_at) > self.runtime.config.max_input_lag_seconds:
                            self.reset()
                            continue
                        for converted in converter.resample(frame):
                            pcm = bytes(converted.planes[0])[:converted.samples * 2]
                            self.feed(pcm, recorded_at)
            path = following

    def run(self):
        while not self.runtime.stopping.is_set():
            try:
                key = tuple(self.runtime.wake_words)
                if self.engine is None or key != self.configuration_key:
                    self.state.update(state="loading", ready=False, error=None)
                    config = self.runtime.config.recognizer.model_copy(update={"keywords": list(key)})
                    self.engine = self.recognizer_factory(config)
                    self.configuration_key = key
                self.state.update(ready=True, error=None)
                if not self.runtime.preferences()["enabled"]:
                    self.state["state"] = "disabled"
                elif not self.runtime.configuration_ready:
                    self.state["state"] = "waiting_configuration"
                else:
                    capture = capture_snapshot(self.runtime.config.edge_state, self.runtime.config.recording_source,
                                               self.runtime.config.principal)
                    if capture:
                        self.state["state"] = "listening"
                        self.consume(capture)
                    else:
                        self.state["state"] = "waiting_recording"
            except Exception as exc:
                self.reset()
                self.state.update(state="retrying", error=type(exc).__name__)
            self.runtime.stopping.wait(2)
