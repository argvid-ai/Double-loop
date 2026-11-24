"""One long-lived FFmpeg input, closed AV segments, optional shared PCM speech.

The original track stays inside each recording. Analysis cadence is independent
of recording duration. File replay is explicitly simulated as a physical source.
"""

from __future__ import annotations

import asyncio
import csv
import json
import time
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .contracts import digest
from .input_capture import CaptureController
from .input_contracts import InputFile
from .observation_inputs import InputInterruption
from .store import Conflict


def name_not_read(name, read_files):
    return Path(name).name not in read_files


class ContinuousCaptureController(CaptureController):
    def __init__(self, media, spec, config, run, window_ms):
        super().__init__(media, spec, config, run, spec.recording_segment_ms)
        self.analysis_window_ms = window_ms
        self.video_stop = threading.Event()
        self.video_read_files = set()
        self.video_metrics = {}
        self.video_task = None
        self.video_failed = False
        self.input_ended = False
        self.capture_id = "continuous-" + uuid.uuid4().hex
        self.folder = self.root / self.capture_id
        self.folder.mkdir()
        self.manifest = self.folder / "segments.csv"
        from .harness_health import RuntimeHealth

        self.health = RuntimeHealth(media.store)

    def command(self, path):
        # Reuse driver selection/encoding; replace the bounded single output.
        command = super().command(path)[:-5]
        if self.spec.replay_duration_ms is not None:
            command += ["-t", str(self.spec.replay_duration_ms / 1000)]
        if self.config.capture_until is not None:
            remaining = self.config.capture_until - self.media.store.clock()
            if remaining <= 0:
                raise Conflict("capture deadline reached before input opened")
            command += ["-t", str(remaining)]
        if "image" in self.spec.binding.modalities:
            command += ["-force_key_frames", f"expr:gte(t,n_forced*{self.window_ms / 1000})"]
        # PCM avoids encoder delay at segment boundaries; this is a declared conversion.
        if "-c:a" in command:
            command[command.index("-c:a") + 1] = "pcm_s16le"
        if "audio" in self.spec.binding.modalities:
            command += ["-af", "asetnsamples=n=256:p=0"]
        command += [
            "-f",
            "segment",
            "-segment_time",
            str(self.window_ms / 1000),
            "-segment_format_options",
            "cluster_time_limit=10:flush_packets=1",
            "-reset_timestamps",
            "0" if self.spec.incremental_video else "1",
            "-segment_list",
            str(self.manifest),
            "-segment_list_type",
            "csv",
            str(path),
        ]
        return command

    def worker_spec(self, path, instance):
        body = super().worker_spec(path, instance)
        body["timeout_seconds"] = None
        body["result_file"] = str(self.folder / "worker-result.json")
        body["recording_folder"] = str(self.folder)
        body["status_file"] = str(self.folder / "worker-status.json")
        body["stderr_file"] = str(self.folder / "capture.stderr")
        body["capture_until"] = self.config.capture_until
        endpoint = self.spec.capture
        endpoints = [(endpoint.driver, endpoint.address)]
        if endpoint.driver == "avfoundation":
            video, audio = endpoint.address.split(":")
            endpoints = []
            if video != "none":
                endpoints.append(("avfoundation", video + ":none"))
            if audio != "none":
                endpoints.append(("avfoundation", "none:" + audio))
        if endpoint.audio_device:
            endpoints.append(("alsa", endpoint.audio_device))
        # Numeric and stable names for the same device share the same reader lease.
        import re
        normalized = []
        for driver, address in endpoints:
            if driver == "v4l2":
                address = str(Path(address).resolve())
            if driver == "alsa":
                match = re.fullmatch(r"hw:([0-9]+)(?:,([0-9]+))?", address)
                if match:
                    identity = Path("/proc/asound") / ("card" + match[1]) / "id"
                    if identity.is_file():
                        card = identity.read_text().strip()
                        if re.fullmatch(r"[A-Za-z0-9_]+", card):
                            address = f"hw:CARD={card},DEV={match[2] or '0'}"
            normalized.append((driver, address))
        endpoints = normalized
        body["hardware_leases"] = [
            str(self.media.store.root / "capture-devices" / (digest(pair) + ".lock"))
            for pair in endpoints
        ]
        if self.spec.shared_voice_config_file:
            from .voice_stream import VoiceStreamConfig

            config = VoiceStreamConfig.model_validate_json(
                Path(self.spec.shared_voice_config_file).read_text()
            )
            if not config.shared_capture:
                raise Conflict("shared microphone config must explicitly select shared capture")
            expected = "file" if endpoint.driver == "file_replay" else "microphone"
            if config.source_kind != expected:
                raise Conflict("speech provenance differs from its actual capture input")
            body["voice_config"] = config.model_copy(
                update={"output_dir": str(self.folder / "voice")}
            ).model_dump(mode="json")
        return body

    async def _publish_closed(self, *, closing=False):
        data = self.manifest.read_text() if self.manifest.exists() else ""
        rows = list(csv.reader(data[: data.rfind("\n") + 1].splitlines()))
        if closing and self.spec.incremental_video:
            from .harness_input_store import probe_input

            names = {Path(r[0]).name for r in rows if len(r) == 3}
            for tail in sorted(self.folder.glob("part-*.mkv")):
                if tail.name in names or tail.name in self.committed or not tail.stat().st_size:
                    continue
                # The capture worker has been joined. Recover only actual decodable
                # bytes; this is not a fabricated encoder close receipt.
                with self.media.store.connect() as db:
                    record = db.execute(
                        "SELECT body FROM managed_input_entries WHERE path IN (SELECT owned_path FROM raw_recordings) AND source_ref=? AND json_extract(body,'$.event_id')=?",
                        (self.spec.binding.source_ref, self.capture_id + "-" + tail.stem),
                    ).fetchone()
                facts = await asyncio.to_thread(probe_input, tail)
                video = next((item for item in facts["streams"] if item["modality"] == "image"), {})
                video_end = video.get("decoded_end_ms", facts["duration_ms"])
                if record:
                    raw = json.loads(record[0])
                    end = (
                        video_end
                        if raw.get("file_timestamps") == "absolute"
                        else raw["start_ms"] + video_end
                    )
                    if end < raw["end_ms"]:
                        raise Conflict("recovered tail does not cover published frames")
                    rows.append([tail.name, str(raw["start_ms"] / 1000), str(end / 1000)])
                    self.media.store.audit(
                        self.run.task_id,
                        "capture.video.tail_recovered",
                        {
                            **self.run.model_dump(),
                            "start_ms": raw["start_ms"],
                            "end_ms": end,
                            "encoder_close_receipt": False,
                            "actual_decode_verified": True,
                        },
                    )
                else:
                    # Shutdown may precede the first publication of a new file.
                    # Recover its actual decoded timestamps, never a wall-clock
                    # estimate or the configured segment length.
                    left, right = (
                        video.get("decoded_start_ms", facts["decoded_start_ms"]),
                        video_end,
                    )
                    if left is None or left < self.previous_end or right <= left:
                        raise Conflict("unpublished tail has no trustworthy ordered timestamps")
                    rows.append([tail.name, str(left / 1000), str(right / 1000)])
                    self.media.store.audit(
                        self.run.task_id,
                        "capture.video.tail_recovered",
                        {
                            **self.run.model_dump(),
                            "start_ms": left,
                            "end_ms": right,
                            "encoder_close_receipt": False,
                            "actual_decode_verified": True,
                            "previously_published": False,
                        },
                    )
        for row in rows:
            if len(row) != 3 or row[0] in self.committed:
                continue
            name, start, end = row
            if (
                self.spec.incremental_video
                and not closing
                and not self.video_failed
                and name_not_read(row[0], self.video_read_files)
            ):
                continue
            if self.spec.shared_voice_config_file and not closing:
                status_file = self.folder / "worker-status.json"
                status = json.loads(status_file.read_text()) if status_file.exists() else {}
                # Keep the original path available until the incremental reader
                # has consumed this file, or has explicitly failed/stopped.
                if (
                    not status.get("speech", {}).get("finished")
                    and status.get("last_read_file", "") < name
                ):
                    continue
            path = self.folder / name
            if path.parent != self.folder or path.resolve() != path or not name.startswith("part-"):
                raise Conflict("encoder returned an unexpected segment path")
            left, right = round(float(start) * 1000), round(float(end) * 1000)
            if right <= left or left < self.previous_end:
                raise Conflict("encoder segment timeline overlaps or is empty")
            if left > self.previous_end:
                # Muxer timestamps need not join exactly, even at minute rollover.
                # Preserve their declared gap instead of pretending continuity or
                # rejecting every subsequent original file. This does not prove
                # a physical camera outage or missing audio samples.
                await asyncio.to_thread(
                    self.media.add_gap,
                    self.run.principal,
                    self.spec.binding.source_ref,
                    InputInterruption(
                        event_id=self.capture_id + f"-gap-{self.previous_end}-{left}",
                        sequence=0,
                        segment_id=self.capture_id,
                        start_ms=self.previous_end,
                        end_ms=left,
                        captured_at=self.began.timestamp() + self.previous_end / 1000,
                        reason="container_timestamp_gap",
                    ),
                    _capture_run=self.run,
                    _closing=closing,
                )
            event = InputFile(
                event_id=self.capture_id + "-" + str(len(self.committed)),
                segment_id=self.capture_id,
                start_ms=left,
                end_ms=right,
                file_start_ms=left if self.spec.incremental_video else 0,
                recorded_start_at=self.began + timedelta(milliseconds=left),
                clock_authority=getattr(self, "clock_authority", "node_reported"),
                capture_revision=digest(
                    [[self.run.task_id, self.run.version, self.run.generation]]
                ),
            )
            if self.spec.incremental_video:
                from .recorded_video import seal_video_recording

                if await asyncio.to_thread(seal_video_recording, self, path, left, right):
                    path.unlink(missing_ok=True)
                    self.committed.add(name)
                    self.previous_end = right
                    self.last_progress = time.monotonic()
                    (self.folder / "capture-receipt.json").write_text(
                        json.dumps(
                            {
                                "input_open_count": 1,
                                "source_simulated": self.spec.binding.simulated,
                                "segments_committed": len(self.committed),
                                "last_media_end_ms": right,
                                "recording_segment_ms": self.window_ms,
                                "incremental_video": True,
                                "saved_video_read": self.video_metrics,
                                "original_audio_in_segments": "audio"
                                in self.spec.binding.modalities,
                                "closed_tail_committed": closing,
                            }
                        )
                    )
                    if not closing:
                        room = await asyncio.to_thread(
                            self.media.retention.collect,
                            self.run.principal,
                            reserve_bytes=self.spec.max_file_bytes * 3,
                        )
                        if not room["writable"]:
                            raise Conflict("recording pool cannot reserve next segment")
                    continue
            operation = asyncio.create_task(
                asyncio.to_thread(
                    self.media.add_recording,
                    self.run.principal,
                    self.spec.binding.source_ref,
                    event,
                    path,
                    check_active=None if closing else self.current,
                    _closing_run=self.run if closing else None,
                )
            )
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                # If publication won the cancellation race, finish bookkeeping so
                # the closing pass cannot register the same bytes under another ID.
                await asyncio.shield(operation)
                path.unlink(missing_ok=True)
                self.committed.add(name)
                self.previous_end = right
                raise
            path.unlink()
            self.committed.add(name)
            self.previous_end = right
            self.last_progress = time.monotonic()
            self.ready.set()
            (self.folder / "capture-receipt.json").write_text(
                json.dumps(
                    {
                        "input_open_count": 1,
                        "source_simulated": self.spec.binding.simulated,
                        "segments_committed": len(self.committed),
                        "last_media_end_ms": right,
                        "original_audio_in_segments": "audio" in self.spec.binding.modalities,
                        "shared_speech": bool(self.spec.shared_voice_config_file),
                        "recording_segment_ms": self.window_ms,
                        "gap_free_verified": False,
                        "conversion": "H264 / PCM audio in Matroska; original source file untouched",
                        "closed_tail_committed": closing,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            if not closing:
                room = await asyncio.to_thread(
                    self.media.retention.collect,
                    self.run.principal,
                    reserve_bytes=self.spec.max_file_bytes * 3,
                )
                if not room["writable"]:
                    raise Conflict("recording pool cannot reserve the next segment; capture paused")

    def _recording_ready(self):
        """Confirm a decodable saved packet, without waiting for file rollover."""
        import av

        for path in sorted(self.folder.glob("part-*.mkv")):
            try:
                with av.open(
                    str(path), options={"probesize": "32768", "analyzeduration": "0"}
                ) as container:
                    for packet in container.demux():
                        if packet.size and packet.decode():
                            return True
            except (av.error.FFmpegError, FileNotFoundError):
                continue
        return False

    async def _loop(self):
        from contextlib import nullcontext
        shared = getattr(self.media.store, "shared_media_storage", None)
        with (shared.reserve(self.spec.max_file_bytes * 3) if shared else nullcontext()):
            return await self._recording_loop()

    async def _recording_loop(self):
        self.current()
        recording_policy = self.media.retention.configuration(self.run.principal)["policy"]
        shared = getattr(self.media.store, "shared_media_storage", None)
        free_floor = max(recording_policy["min_free_bytes"], shared.config.min_free_bytes if shared else 0)
        if not recording_policy["enabled"]:
            raise Conflict("continuous capture requires an enabled bounded recording policy")
        room = await asyncio.to_thread(
            self.media.retention.collect,
            self.run.principal,
            reserve_bytes=self.spec.max_file_bytes * 3,
        )
        if not room["writable"]:
            raise Conflict("recording pool cannot reserve writing and publishing space")
        self.began = datetime.now(timezone.utc)
        self.committed = set()
        self.previous_end = 0
        self.last_progress = time.monotonic()
        worker = asyncio.create_task(self._window(self.folder / "part-%06d.mkv"))
        if self.spec.incremental_video:
            from .recorded_video import saved_video_frontier

            self.video_task = asyncio.create_task(
                asyncio.to_thread(saved_video_frontier, self, worker.done, self.video_stop)
            )
        try:
            database_waiting = False
            while not self.closed:
                if (
                    self.config.capture_until is not None
                    and self.media.store.clock() >= self.config.capture_until
                ):
                    self.input_ended = True
                    break
                if self.video_task and self.video_task.done() and not self.video_failed:
                    try:
                        self.video_task.result()
                    except Exception as exc:
                        self.video_failed = True
                        self.health.record(
                            self.run.principal,
                            "visual_input",
                            "incremental_reader_failed",
                            task_id=self.run.task_id,
                            version=self.run.version,
                            generation=self.run.generation,
                            details={
                                "error_type": type(exc).__name__,
                                "recording_active": not worker.done(),
                                "analysis_mode": "sealed_files_only",
                            },
                            simulated=self.spec.binding.simulated,
                        )
                        self.media.store.audit(
                            self.run.task_id,
                            "capture.video.degraded",
                            {
                                **self.run.model_dump(),
                                "error_type": type(exc).__name__,
                                "recording_continues": True,
                                "analysis_mode": "sealed_files_only",
                                "simulated": self.spec.binding.simulated,
                            },
                        )
                self.current()
                try:
                    await self._publish_closed()
                except Exception as exc:
                    from .sqlite_writes import busy
                    if not busy(exc):
                        raise
                    # Existing segment files/CSV are the durable pending input.
                    # Do not create a second video cache or stop for a short DB lock.
                    staged_bytes = sum(p.stat().st_size for p in self.folder.glob("part-*.mkv")
                                       if p.name not in self.committed)
                    if staged_bytes > self.spec.max_file_bytes * 3:
                        raise Conflict("unregistered capture exceeds reserved disk space") from exc
                    import shutil
                    if shutil.disk_usage(self.folder).free < free_floor:
                        raise Conflict("capture reached the disk free-space floor") from exc
                    if worker.done():
                        worker.result()
                        raise Conflict("capture input ended while registration was waiting") from exc
                    if not database_waiting:
                        import logging
                        logging.getLogger(__name__).warning(
                            "capture registration waiting for database; original segments retained")
                        database_waiting = True
                    await asyncio.sleep(.1)
                    continue
                database_waiting = False
                if not self.ready.is_set() and await asyncio.to_thread(self._recording_ready):
                    self.ready.set()
                if self.spec.shared_voice_config_file:
                    status_path = self.folder / "worker-status.json"
                    status = json.loads(status_path.read_text()) if status_path.exists() else {}
                    speech = status.get("speech", {})
                    code = (
                        "speech_backlog"
                        if status.get("speech_overrun")
                        else "speech_failed"
                        if speech.get("error_type") or status.get("reader_error_type")
                        else "healthy"
                    )
                    self.health.record(
                        self.run.principal,
                        "speech",
                        code,
                        task_id=self.run.task_id,
                        version=self.run.version,
                        generation=self.run.generation,
                        details={
                            "recording_active": not worker.done(),
                            "error_type": speech.get("error_type")
                            or status.get("reader_error_type"),
                        },
                        simulated=self.spec.binding.simulated,
                    )

                if worker.done():
                    worker.result()
                    if self.spec.replay_duration_ms is not None or self.input_ended:
                        self.input_ended = True
                        break
                    raise Conflict(
                        "capture input ended; closed segments retained, reconnect creates a new run"
                    )
                staging = [
                    p for p in self.folder.glob("part-*.mkv") if p.name not in self.committed
                ]
                if (
                    any(p.stat().st_size > self.spec.max_file_bytes for p in staging)
                    or len(staging) > 2
                ):
                    raise Conflict("capture output exceeded reserved segment bound")
                if time.monotonic() - self.last_progress > self.window_ms / 1000 + 15:
                    raise Conflict("capture stream stopped producing closed segments")
                await asyncio.sleep(0.1)
        finally:
            # Deadline failure can enter teardown before Gateway calls close().
            # Its cancellation must not interrupt encoder shutdown or tail sealing.
            self.finalizer = asyncio.create_task(self._finish(worker))
            await asyncio.shield(self.finalizer)
        if self.input_ended and (self.folder / "unpublished-tail.json").exists():
            raise Conflict("bounded replay ended with an unpublished tail")

    async def _finish(self, worker):
        self.video_stop.set()
        if self.video_task:
            await asyncio.gather(self.video_task, return_exceptions=True)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        # FFmpeg may create the next output before receiving any packets.
        # A verified empty placeholder is not captured media.
        for path in self.folder.glob("part-*.mkv"):
            if not path.is_symlink() and path.stat().st_size == 0:
                path.unlink()
        try:
            await self._publish_closed(closing=True)
            if list(self.folder.glob("part-*.mkv")):
                raise Conflict("encoder left an unclosed tail without trustworthy timestamps")
        except Exception as exc:
            # Preserve bytes and an explicit incident; never invent a capture receipt.
            (self.folder / "unpublished-tail.json").write_text(
                json.dumps(
                    {
                        "error_type": type(exc).__name__,
                        "files": [p.name for p in self.folder.glob("part-*.mkv")],
                        "indexed": False,
                        "operator_attention_required": True,
                    }
                )
            )
            self.media.store.audit(
                self.run.task_id, "capture.tail.unpublished", {"error_type": type(exc).__name__}
            )
            if self.spec.incremental_video:
                from .recorded_video import interrupt_video_recording

                with self.media.store.connect() as db:
                    unfinished = db.execute(
                        "SELECT body FROM managed_input_entries WHERE source_ref=? AND json_extract(body,'$.segment_id')=? AND json_extract(body,'$.recording_state')='growing'",
                        (self.spec.binding.source_ref, self.capture_id),
                    ).fetchall()
                for row in unfinished:
                    event_id = json.loads(row[0])["event_id"]
                    path = self.folder / (event_id.removeprefix(self.capture_id + "-") + ".mkv")
                    await asyncio.to_thread(
                        interrupt_video_recording, self, path, type(exc).__name__
                    )
