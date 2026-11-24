"""A task-owned sequential decoder: bounded to one carried boundary frame.

Adjacent windows reuse the same decoder. Replay loops, historical reads, failed
preparation and discontinuous windows reset it; original media stays immutable.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path

import av

from .observation_media import IncompleteObservation, _fingerprint


class ContinuousVideoReader:
    def __init__(self, source, *, validate_source=None, open_stream=None):
        self.validate_source = validate_source
        self.open_stream = open_stream
        self.raw_stream = None
        self.source = Path(source).resolve(strict=True)
        self.fingerprint = _fingerprint(self.source)
        self.lock = threading.RLock()
        self.container = self.decoder = self.last_frame = None
        self.expected_start = None
        self.rotation = 0
        self.closed = False

    def reset(self):
        with self.lock:
            if self.container is not None:
                self.container.close()
            if self.raw_stream is not None:
                self.raw_stream.close()
                self.raw_stream = None
            self.container = self.decoder = self.last_frame = None
            self.expected_start = None
            self.rotation = 0

    def close(self):
        with self.lock:
            self.reset()
            self.closed = True

    def advance(self, end, check):
        """Decode available frames up to the next sample, retaining its boundary."""
        from .observation_media import FileReplayWindow

        timings = FileReplayWindow._new_timings()
        if self.last_frame is not None:
            frame = self.last_frame
            covered = (Fraction(frame.pts) + (frame.duration or 0)) * frame.time_base * 1000
            if covered >= end:
                return timings
        start = self.expected_start or 0
        with self.window(start, end, timings) as frames:
            for frame in frames:
                check()
                if frame.pts is None:
                    raise IncompleteObservation("image", "frame has no timestamp")
                covered = (Fraction(frame.pts) + (frame.duration or 0)) * frame.time_base * 1000
                if covered >= end:
                    break
        return timings

    def _frames(self, start, timings):
        if self.last_frame is not None:
            frame = self.last_frame
            pts = Fraction(frame.pts) * frame.time_base * 1000
            end = pts + Fraction(frame.duration or 0) * frame.time_base * 1000
            # A frame crossing a window boundary, or decoded just at that
            # boundary, must remain available to the following window.
            if pts >= start or end > start:
                yield frame
        while True:
            began = time.monotonic()
            try:
                frame = next(self.decoder)
            except StopIteration:
                timings["decode_seconds"] += time.monotonic() - began
                return
            timings["decode_seconds"] += time.monotonic() - began
            timings["decoded_frames"] += 1
            self.last_frame = frame
            yield frame

    @contextmanager
    def window(self, start, end, timings):
        with self.lock:
            if self.closed:
                raise ValueError("continuous video reader is closed")
            if self.validate_source:
                self.validate_source()
            elif _fingerprint(self.source) != self.fingerprint:
                self.reset()
                raise ValueError("registered source changed")
            try:
                forward_stream = (
                    self.raw_stream is not None
                    and self.expected_start is not None
                    and start > self.expected_start
                )
                if self.container is None or (self.expected_start != start and not forward_stream):
                    self.reset()
                    began = time.monotonic()
                    self.raw_stream = self.open_stream() if self.open_stream else None
                    self.container = (
                        av.open(
                            self.raw_stream,
                            format="matroska",
                            options={"probesize": "32", "analyzeduration": "0"},
                        )
                        if self.raw_stream
                        else av.open(str(self.source))
                    )
                    timings["opens"] += 1
                    if not self.container.streams.video:
                        raise IncompleteObservation("image", "no video track")
                    stream = self.container.streams.video[0]
                    if self.raw_stream is None:
                        self.container.seek(
                            int(Fraction(start, 1000) / stream.time_base),
                            stream=stream,
                            backward=True,
                        )
                        timings["seeks"] += 1
                    self.decoder = iter(self.container.decode(stream))
                    timings["open_seek_seconds"] += time.monotonic() - began
                yield self._frames(start, timings)
                self.expected_start = end
            except BaseException:
                self.reset()
                raise
