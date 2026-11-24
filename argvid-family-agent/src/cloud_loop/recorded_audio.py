"""Incremental speech input read from the recorder's original, growing AV files.

No second microphone, raw PCM spool, or wait for the minute-long file to close.
Only bytes observed in the original file and synced before release are decoded.
"""

from __future__ import annotations

import csv
import io
import os
import time
from pathlib import Path

import av


class GrowingRecording(io.RawIOBase):
    def __init__(self, path, finished, stopped, metrics, check_active=lambda: None):
        super().__init__()
        self.check_active = check_active
        self.file = path.open("rb")
        self.finished, self.stopped, self.metrics = finished, stopped, metrics

    def readable(self):
        return True

    def read(self, size=-1):
        while not self.stopped.is_set():
            self.check_active()
            value = self.file.read(32768 if size < 0 else size)
            if value:
                started = time.monotonic()
                # A successful write/flush is not a persistence receipt. Sync the
                # same inode before these bytes can enter command recognition.
                os.fsync(self.file.fileno())
                elapsed = time.monotonic() - started
                self.metrics["sync_calls"] += 1
                self.metrics["sync_seconds"] += elapsed
                self.metrics["max_sync_seconds"] = max(self.metrics["max_sync_seconds"], elapsed)
                self.metrics["recording_bytes_read"] += len(value)
                return value
            if self.finished():
                return b""
            self.stopped.wait(0.005)
        return b""

    def close(self):
        self.file.close()
        super().close()


def saved_pcm(folder, capture_finished, stopped, metrics, progress):
    """Decode each original once; progress allows publication after read completion."""
    folder = Path(folder)
    manifest = folder / "segments.csv"
    index = 0
    metrics.update(sync_calls=0, sync_seconds=0.0, max_sync_seconds=0.0, recording_bytes_read=0)

    def sealed(name):
        if capture_finished():
            return True
        if not manifest.exists():
            return False
        text = manifest.read_text()
        return any(
            len(row) == 3 and Path(row[0]).name == name
            for row in csv.reader(text[: text.rfind("\n") + 1].splitlines())
        )

    while not stopped.is_set():
        path = folder / f"part-{index:06d}.mkv"
        if not path.exists():
            if capture_finished():
                return
            stopped.wait(0.005)
            continue
        if path.is_symlink() or path.resolve().parent != folder.resolve():
            raise ValueError("recording read escaped capture directory")
        with GrowingRecording(path, lambda: sealed(path.name), stopped, metrics) as source:
            with av.open(
                source, format="matroska", options={"probesize": "32", "analyzeduration": "0"}
            ) as container:
                if not container.streams.audio:
                    raise ValueError("saved recording has no audio stream")
                resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
                for frame in container.decode(audio=0):
                    if stopped.is_set():
                        return
                    for converted in resampler.resample(frame):
                        metrics.setdefault("first_saved_audio_monotonic", time.monotonic())
                        yield bytes(converted.planes[0])[: converted.samples * 2]
                for converted in resampler.resample(None):
                    yield bytes(converted.planes[0])[: converted.samples * 2]
        progress(path.name)
        index += 1
