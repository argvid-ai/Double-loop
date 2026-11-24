"""Node-owned raw input files and immutable source/time manifests.

This is the capture boundary, not an inference engine. Input registration never
starts observation, changes a user conversation or deletes an original. Device
drivers and bounded uploads use the same committed manifests as file inputs.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path

import av

from .contracts import canonical, digest
from .input_contracts import InputFile as InputFile
from .input_contracts import InputSourceSpec as InputSourceSpec
from .media import file_hash
from .observation_contracts import Reference
from .observation_inputs import InputInterruption, InputWindow
from .observation_media import _fingerprint
from .store import Conflict


def probe_input(path: Path) -> dict:
    """Probe closed, bounded input; decode timestamps when no duration exists.

    Browser MediaRecorder containers often omit duration. That is handled by a
    bounded decode, never by trusting the client's elapsed wall clock or padding.
    """
    import time

    started = time.monotonic()
    facts = {"modalities": [], "duration_ms": 0, "streams": [], "probe": "headers_and_first_frames"}
    try:
        with av.open(str(path), timeout=10) as container:
            format_name = container.format.name
            for stream in container.streams:
                if stream.type not in {"video", "audio"}:
                    continue
                modality = "image" if stream.type == "video" else "audio"
                codec = stream.codec_context
                if modality == "image" and (codec.width * codec.height > 16_777_216):
                    raise ValueError("input frame exceeds decode bound")
                if modality == "audio" and (
                    codec.sample_rate > 192000 or len(codec.layout.channels) > 8
                ):
                    raise ValueError("input audio exceeds decode bound")
                if modality not in facts["modalities"]:
                    facts["modalities"].append(modality)
                duration = (
                    int(stream.duration * stream.time_base * 1000)
                    if stream.duration is not None
                    else None
                )
                facts["streams"].append(
                    {"modality": modality, "codec": codec.name, "duration_ms": duration}
                )
            if container.duration is not None:
                facts["duration_ms"] = int(container.duration / 1000)
            else:
                facts["duration_ms"] = max(
                    (s["duration_ms"] or 0 for s in facts["streams"]), default=0
                )
        if not facts["modalities"]:
            raise ValueError("input has no usable media")
        needs_duration = not facts["duration_ms"]
        decoded_end = 0
        decoded_start = None
        for modality in facts["modalities"]:
            count = 0
            stream_start, stream_end = None, 0
            with av.open(str(path), timeout=10) as container:
                stream = (
                    container.streams.video[0]
                    if modality == "image"
                    else container.streams.audio[0]
                )
                for frame in container.decode(stream):
                    count += 1
                    if time.monotonic() - started > 10 or count > 40000:
                        raise ValueError("input duration probe exceeded work bound")
                    if frame.pts is not None:
                        stamp = round(float(frame.pts * frame.time_base) * 1000)
                        decoded_start = (
                            min(decoded_start, stamp) if decoded_start is not None else stamp
                        )
                        stream_start = (
                            min(stream_start, stamp) if stream_start is not None else stamp
                        )
                    if not needs_duration:
                        break
                    if frame.pts is None:
                        raise ValueError("durationless input has no frame timestamps")
                    end = float(frame.pts * frame.time_base)
                    decoded_start = (
                        min(decoded_start, round(end * 1000))
                        if decoded_start is not None
                        else round(end * 1000)
                    )
                    if modality == "audio":
                        end += frame.samples / frame.sample_rate
                    else:
                        rate = float(stream.average_rate or stream.guessed_rate or 0)
                        if frame.duration:
                            end += float(frame.duration * frame.time_base)
                        elif rate > 0:
                            end += 1 / rate
                        else:
                            raise ValueError("input has no frame duration")
                    decoded_end = max(decoded_end, round(end * 1000))
                    stream_end = max(stream_end, round(end * 1000))
                    if decoded_end - decoded_start > 61000:
                        raise ValueError("durationless input exceeds a capture window")
            if not count:
                raise ValueError("input stream cannot be decoded")
            if needs_duration:
                description = next(
                    item for item in facts["streams"] if item["modality"] == modality
                )
                description.update(decoded_start_ms=stream_start, decoded_end_ms=stream_end)
        if needs_duration:
            facts["duration_ms"] = decoded_end
            facts["probe"] = "bounded_full_timestamp_decode"
        facts["decoded_start_ms"] = decoded_start
        if not facts["duration_ms"]:
            raise ValueError("input has no usable duration")
        video = "image" in facts["modalities"]
        formats = {
            "wav": "audio/wav",
            "mp3": "audio/mpeg",
            "flac": "audio/flac",
            "aac": "audio/aac",
            "ogg": "video/ogg" if video else "audio/ogg",
            "matroska": "video/x-matroska" if video else "audio/webm",
            "mov": "video/mp4" if video else "audio/mp4",
            "avi": "video/x-msvideo",
        }
        facts["media_type"] = formats.get(format_name.split(",")[0], "application/octet-stream")
    except av.FFmpegError:
        raise ValueError("input cannot be decoded by the installed media backend") from None
    facts.update(bytes=path.stat().st_size, sha256=file_hash(path))
    return facts


class InputMediaStore:
    def __init__(self, store):
        self.store = store
        store.input_media = self
        self._verified_bytes = {}
        self.root = store.root.resolve() / "input-media"
        if self.root.resolve() != self.root:
            raise Conflict("raw input root escaped node state")
        self.root.mkdir(exist_ok=True)
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS managed_inputs (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, spec TEXT NOT NULL,
                    sealed INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS managed_input_entries (
                    id TEXT PRIMARY KEY, principal TEXT NOT NULL, source_ref TEXT NOT NULL,
                    sequence INTEGER NOT NULL, event_id TEXT NOT NULL, kind TEXT NOT NULL,
                    body TEXT NOT NULL, path TEXT, bytes INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(source_ref,sequence), UNIQUE(source_ref,event_id));
                CREATE TABLE IF NOT EXISTS managed_input_subscriptions (
                    task_id TEXT NOT NULL, source_ref TEXT NOT NULL, first_sequence INTEGER NOT NULL,
                    ledger_origin INTEGER NOT NULL, PRIMARY KEY(task_id,source_ref));
                CREATE TABLE IF NOT EXISTS input_clients (
                    principal TEXT NOT NULL,source_ref TEXT NOT NULL,client_id TEXT NOT NULL,purpose TEXT NOT NULL,
                    recording INTEGER NOT NULL,tracks_active INTEGER NOT NULL,connected INTEGER NOT NULL,updated REAL NOT NULL,
                    PRIMARY KEY(principal,source_ref,client_id,purpose));
            """)
            if "dataset_start" not in {
                r[1] for r in db.execute("PRAGMA table_info(managed_inputs)")
            }:
                db.execute(
                    "ALTER TABLE managed_inputs ADD COLUMN dataset_start INTEGER NOT NULL DEFAULT 0"
                )
            if "segment_epoch" not in {
                r[1] for r in db.execute("PRAGMA table_info(managed_input_subscriptions)")
            }:
                db.execute(
                    "ALTER TABLE managed_input_subscriptions ADD COLUMN segment_epoch INTEGER NOT NULL DEFAULT 0"
                )

        from .raw_retention import RawRetention

        self.retention = RawRetention(self)

    def register(self, principal, spec: InputSourceSpec):
        spec = InputSourceSpec.model_validate(spec.model_dump())
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM managed_inputs WHERE id=?", (spec.binding.source_ref,)
            ).fetchone()
            if old and (
                old["principal"] != principal
                or canonical(InputSourceSpec.model_validate_json(old["spec"])) != canonical(spec)
            ):
                raise Conflict(
                    "input source identity is already bound; use a new versioned identity"
                )
            db.execute(
                "INSERT OR IGNORE INTO managed_inputs(id,principal,spec,sealed,created) VALUES (?,?,?,0,?)",
                (spec.binding.source_ref, principal, canonical(spec), self.store.clock()),
            )
        return self.source(principal, spec.binding.source_ref)

    def source(self, principal, source_ref):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM managed_inputs WHERE id=? AND principal=?", (source_ref, principal)
            ).fetchone()
        if row is None:
            raise KeyError(source_ref)
        return {
            "spec": json.loads(row["spec"]),
            "sealed": bool(row["sealed"]),
            "dataset_start": row["dataset_start"],
            "simulated": json.loads(row["spec"])["binding"]["simulated"],
        }

    def list(self, principal):
        with self.store.connect() as db:
            ids = [
                r[0]
                for r in db.execute("SELECT id FROM managed_inputs WHERE principal=?", (principal,))
            ]
        return [self.source(principal, ref) for ref in ids]

    def seal(self, principal, source_ref):
        source = self.source(principal, source_ref)
        if not source["spec"]["binding"]["finite"]:
            raise Conflict("live source EOF must be an interruption, not finite completion")
        with self.store.connect() as db:
            db.execute(
                "UPDATE managed_inputs SET sealed=1 WHERE id=? AND principal=?",
                (source_ref, principal),
            )
        return self.source(principal, source_ref)

    def begin_dataset(self, principal, source_ref):
        source = self.source(principal, source_ref)
        if not source["spec"]["binding"]["finite"]:
            raise Conflict("live sources do not reset finite datasets")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            table = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='observation_tasks'"
            ).fetchone()
            active = (
                table
                and db.execute(
                    """SELECT 1 FROM observation_tasks WHERE principal=? AND status!='stopped'
                AND json_extract(config,'$.source_ref')=?""",
                    (principal, source_ref),
                ).fetchone()
            )
            if active:
                raise Conflict("stop the previous observation before importing a new dataset")
            next_sequence = db.execute(
                "SELECT COALESCE(MAX(sequence),-1)+1 FROM managed_input_entries WHERE source_ref=?",
                (source_ref,),
            ).fetchone()[0]
            db.execute(
                "UPDATE managed_inputs SET sealed=0,dataset_start=? WHERE id=? AND principal=?",
                (next_sequence, source_ref, principal),
            )
        return self.source(principal, source_ref)

    def active_revision(self, principal, source_ref, db=None):
        if db is None:
            with self.store.connect() as connection:
                return self.active_revision(principal, source_ref, connection)
        table = db.execute("SELECT 1 FROM sqlite_master WHERE name='observation_tasks'").fetchone()
        rows = (
            db.execute(
                """SELECT task_id,version,generation FROM observation_tasks WHERE principal=?
            AND status IN ('starting','running') AND json_extract(config,'$.source_ref')=? ORDER BY task_id""",
                (principal, source_ref),
            ).fetchall()
            if table
            else []
        )
        return digest([list(row) for row in rows]) if rows else None

    def heartbeat(
        self, principal, source_ref, client_id, purpose, *, recording, tracks_active, connected
    ):
        self.source(principal, source_ref)
        from pydantic import TypeAdapter

        TypeAdapter(Reference).validate_python(client_id)
        with self.store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO input_clients VALUES (?,?,?,?,?,?,?,?)",
                (
                    principal,
                    source_ref,
                    client_id,
                    purpose,
                    int(recording),
                    int(tracks_active),
                    int(connected),
                    self.store.clock(),
                ),
            )

    def source_run_started(self, principal, task_id, generation):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT payload,created FROM audit WHERE task_id=? AND kind='observation.start.accepted' AND json_extract(payload,'$.generation')=? ORDER BY seq DESC LIMIT 1",
                (task_id, generation),
            ).fetchone()
            if row:
                return json.loads(row["payload"]).get("occurred_at", row["created"])
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='observation_tasks'"
            ).fetchone():
                return self.store.clock()
            task = db.execute(
                "SELECT created FROM observation_tasks WHERE task_id=? AND principal=?",
                (task_id, principal),
            ).fetchone()
        return task["created"] if task else 0

    def client_connected(self, principal, source_ref):
        source = self.source(principal, source_ref)
        freshness = source["spec"].get("idle_timeout_seconds", 30)
        with self.store.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM input_clients WHERE principal=? AND source_ref=? AND purpose='observation' AND connected=1 AND updated>=? LIMIT 1",
                    (principal, source_ref, self.store.clock() - freshness),
                ).fetchone()
                is not None
            )

    def client_stop_confirmed(self, principal, source_ref, *, since=0, client_id=None):
        with self.store.connect() as db:
            if client_id is not None:
                row = db.execute(
                    "SELECT recording,tracks_active,updated FROM input_clients WHERE principal=? AND source_ref=? AND purpose='observation' AND client_id=?",
                    (principal, source_ref, client_id),
                ).fetchone()
                return bool(
                    row
                    and row["updated"] >= since
                    and not row["recording"]
                    and not row["tracks_active"]
                )
            rows = db.execute(
                "SELECT recording,tracks_active FROM input_clients WHERE principal=? AND source_ref=? AND purpose='observation' AND updated>=?",
                (principal, source_ref, since),
            ).fetchall()
        return all(not row["recording"] and not row["tracks_active"] for row in rows)

    def _append(
        self,
        principal,
        source_ref,
        event_id,
        kind,
        body,
        path=None,
        size=0,
        *,
        rolling=False,
        closing_run=None,
    ):
        ident = "input-media-" + digest([principal, source_ref, event_id])[:40]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            source = db.execute(
                "SELECT * FROM managed_inputs WHERE id=? AND principal=?", (source_ref, principal)
            ).fetchone()
            if source is None:
                raise KeyError(source_ref)
            old = db.execute("SELECT * FROM managed_input_entries WHERE id=?", (ident,)).fetchone()
            if old:
                if old["body"] != canonical(body) or old["kind"] != kind:
                    raise Conflict("input event identity reused with different content")
                return {
                    **json.loads(old["body"]),
                    "media_ref": ident,
                    "sequence": old["sequence"],
                    "duplicate": True,
                }
            if source["sealed"]:
                raise Conflict("finite input has already been sealed")
            spec = InputSourceSpec.model_validate_json(source["spec"])
            if not spec.binding.finite:
                table = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='observation_tasks'"
                ).fetchone()
                active = (
                    table
                    and db.execute(
                        """SELECT 1 FROM observation_tasks WHERE principal=?
                    AND status IN ('starting','running') AND json_extract(config,'$.source_ref')=?""",
                        (principal, source_ref),
                    ).fetchone()
                )
                if closing_run is not None:
                    # Only an internal recorder can finish its closed tail. A newer
                    # generation or changed source fences even this final commit.
                    task = db.execute(
                        "SELECT * FROM observation_tasks WHERE task_id=? AND principal=?",
                        (closing_run.task_id, principal),
                    ).fetchone()
                    if (
                        not task
                        or closing_run.principal != principal
                        or (task["version"], task["generation"])
                        != (closing_run.version, closing_run.generation)
                        or json.loads(task["config"])["source_ref"] != source_ref
                    ):
                        raise Conflict("final recording belongs to a superseded capture generation")
                    expected = digest(
                        [[closing_run.task_id, closing_run.version, closing_run.generation]]
                    )
                else:
                    if not active:
                        raise Conflict("live input subscription ended before media commit")
                    expected = self.active_revision(principal, source_ref, db)
                if (closing_run is not None or body.get("capture_revision")) and body[
                    "capture_revision"
                ] != expected:
                    raise Conflict("old capture generation cannot enter a new observation run")
            last = db.execute(
                "SELECT * FROM managed_input_entries WHERE source_ref=? ORDER BY sequence DESC LIMIT 1",
                (source_ref,),
            ).fetchone()
            sequence = last["sequence"] + 1 if last else 0
            if last:
                previous = json.loads(last["body"])
                if (
                    previous["segment_id"] == body["segment_id"]
                    and previous["end_ms"] != body["start_ms"]
                ):
                    raise Conflict("input needs an explicit gap or new continuous segment")
                if (
                    previous["segment_id"] != body["segment_id"]
                    and db.execute(
                        "SELECT 1 FROM managed_input_entries WHERE source_ref=? AND json_extract(body,'$.segment_id')=?",
                        (source_ref, body["segment_id"]),
                    ).fetchone()
                ):
                    raise Conflict("cannot reopen an earlier continuous input segment")
            used = db.execute(
                "SELECT COALESCE(SUM(bytes),0) FROM managed_input_entries WHERE source_ref=? AND id NOT IN (SELECT media_ref FROM raw_recordings WHERE state='expired')",
                (source_ref,),
            ).fetchone()[0]
            if (
                used + size > spec.quota_bytes
                and not (
                    rolling and not spec.protect_recordings
                    and self.retention.configuration(principal)["policy"]["enabled"]
                )
            ) or shutil.disk_usage(self.root).free < spec.min_free_bytes:
                raise Conflict("input storage quota or free-space floor exceeded")
            db.execute(
                "INSERT INTO managed_input_entries VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    principal,
                    source_ref,
                    sequence,
                    event_id,
                    kind,
                    canonical(body),
                    str(path) if path else None,
                    size,
                ),
            )
            if rolling:
                self.retention.register(db, ident, principal, body, path)
        return {**body, "media_ref": ident, "sequence": sequence, "duplicate": False}

    def add_file(
        self,
        principal,
        source_ref,
        event: InputFile,
        path: Path,
        *,
        check_active=None,
        _rolling=False,
        _closing_run=None,
    ):
        """Called by an authenticated adapter with an owned, complete local file."""
        spec = InputSourceSpec.model_validate(self.source(principal, source_ref)["spec"])
        if check_active:
            check_active()
        event = InputFile.model_validate(event.model_dump())
        path = Path(path).resolve(strict=True)
        if not path.is_file() or path.stat().st_size > spec.max_file_bytes:
            raise Conflict("input file exceeds configured bound or is not a regular file")
        before = _fingerprint(path)
        facts = probe_input(path)
        if check_active:
            check_active()
        if _fingerprint(path) != before:
            raise Conflict("input changed during probe")
        if event.end_ms is None:
            event = InputFile.model_validate(
                {
                    **event.model_dump(),
                    "end_ms": event.start_ms + facts["duration_ms"] - event.file_start_ms,
                }
            )
        duration = event.end_ms - event.start_ms
        if duration > (
            spec.max_file_duration_ms
            if spec.binding.finite or spec.continuous_capture
            else spec.max_window_ms
        ):
            raise Conflict("input window exceeds configured maximum")
        if set(facts["modalities"]) - set(spec.binding.modalities):
            raise Conflict("actual input modalities exceed installed source declaration")
        if event.file_start_ms + duration > facts["duration_ms"] + 1:
            raise Conflict("input range exceeds actual media duration")
        body = {
            **event.model_dump(mode="json"),
            "facts": facts,
            "source_fingerprint": list(before),
            "captured_at": self.store.clock(),
            "simulated": spec.binding.simulated,
            "location_ref": spec.location_ref,
            "source_ref": source_ref,
        }
        # Retry timestamps do not alter the original authoritative receipt.
        ident = "input-media-" + digest([principal, source_ref, event.event_id])[:40]
        with self.store.connect() as db:
            old = db.execute(
                "SELECT body FROM managed_input_entries WHERE id=? AND principal=?",
                (ident, principal),
            ).fetchone()
        if old:
            previous = json.loads(old["body"])
            body["captured_at"] = previous["captured_at"]
            # New optional probe detail must not change a persisted event's
            # identity. Bytes, fingerprint and range retain strict comparison.
            if "decoded_start_ms" not in previous.get("facts", {}):
                body["facts"].pop("decoded_start_ms", None)
        return self._append(
            principal,
            source_ref,
            event.event_id,
            "media",
            body,
            path,
            facts["bytes"],
            rolling=_rolling,
            closing_run=_closing_run,
        )

    def add_owned_file(
        self,
        principal,
        source_ref,
        event: InputFile,
        path: Path,
        *,
        check_active=None,
        _rolling=False,
        _closing_run=None,
    ):
        """Commit uploaded/driver bytes. Never overwrite an existing raw file."""
        self.source(principal, source_ref)
        if check_active:
            check_active()
        ident = "input-media-" + digest([principal, source_ref, event.event_id])[:40]
        with self.store.connect() as db:
            old = db.execute(
                "SELECT * FROM managed_input_entries WHERE id=? AND principal=?",
                (ident, principal),
            ).fetchone()
        if old:
            with self.store.connect() as db:
                availability = self.retention.availability(db, principal, ident)
            if availability["state"] != "available":
                previous = json.loads(old["body"])
                supplied = event.model_dump(mode="json")
                if supplied["end_ms"] is None:
                    supplied["end_ms"] = (
                        event.start_ms + previous["facts"]["duration_ms"] - event.file_start_ms
                    )
                if (
                    any(supplied[key] != previous[key] for key in supplied)
                    or file_hash(Path(path), check_active=check_active)
                    != previous["facts"]["sha256"]
                ):
                    raise Conflict("expired input identity reused with different content")
                return {
                    **previous,
                    "media_ref": ident,
                    "sequence": old["sequence"],
                    "duplicate": True,
                    "availability": availability,
                }
            if file_hash(Path(old["path"]), check_active=check_active) != file_hash(
                Path(path), check_active=check_active
            ):
                raise Conflict("input retry contains different media bytes")
            return self.add_file(
                principal, source_ref, event, Path(old["path"]), check_active=check_active
            )
        directory = self.root / "raw"
        directory.mkdir(exist_ok=True)
        if directory.resolve() != directory:
            raise Conflict("raw input directory escaped node state")
        destination = directory / (uuid.uuid4().hex + ".media")
        try:
            from contextlib import nullcontext
            shared = getattr(self.store, "shared_media_storage", None)
            with (shared.reserve(Path(path).stat().st_size) if shared else nullcontext()), \
                    Path(path).open("rb") as source, destination.open("xb") as output:
                while chunk := source.read(65536):
                    if check_active:
                        check_active()
                    output.write(chunk)
            destination.chmod(0o600)
            return self.add_file(
                principal,
                source_ref,
                event,
                destination,
                check_active=check_active,
                _rolling=_rolling,
                _closing_run=_closing_run,
            )
        except BaseException:
            # This request owns only its unpublished copy, never an original.
            with self.store.connect() as db:
                committed = db.execute(
                    "SELECT 1 FROM managed_input_entries WHERE path=?", (str(destination),)
                ).fetchone()
            if not committed:
                destination.unlink(missing_ok=True)
            raise

    def add_recording(
        self, principal, source_ref, event, path, *, check_active=None, _closing_run=None
    ):
        """Only capture adapters may opt owned new copies into rolling deletion."""
        with self.retention.lock:
            with self.store.connect() as db:
                existing = db.execute(
                    "SELECT id FROM managed_input_entries WHERE principal=? AND source_ref=? AND event_id=?",
                    (principal, source_ref, event.event_id),
                ).fetchone()
            if not existing:
                room = self.retention.collect(principal, reserve_bytes=Path(path).stat().st_size)
                if not room["writable"]:
                    raise Conflict("protected or busy recording pool is full; capture must pause")
            return self.add_owned_file(
                principal,
                source_ref,
                event,
                path,
                check_active=check_active,
                _rolling=True,
                _closing_run=_closing_run,
            )

    def add_gap(
        self, principal, source_ref, event: InputInterruption, *, _capture_run=None, _closing=False
    ):
        self.source(principal, source_ref)
        event = InputInterruption.model_validate(event.model_dump())
        body = event.model_dump(mode="json")
        if _capture_run is not None:
            body["capture_revision"] = digest(
                [[_capture_run.task_id, _capture_run.version, _capture_run.generation]]
            )
        return self._append(
            principal,
            source_ref,
            event.event_id,
            "gap",
            body,
            closing_run=_capture_run if _closing else None,
        )

    def _check_bytes(self, path, expected, check_active):
        fingerprint = _fingerprint(path)
        key = (str(path), fingerprint, expected)
        if key not in self._verified_bytes:
            if (
                file_hash(path, check_active=check_active) != expected
                or _fingerprint(path) != fingerprint
            ):
                raise Conflict("registered raw input bytes changed")
            self._verified_bytes[key] = True
            if len(self._verified_bytes) > 32:
                self._verified_bytes.pop(next(iter(self._verified_bytes)), None)

    def resolve(self, principal, media_ref, *, check_active=None, allow_growing=False):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM managed_input_entries WHERE id=? AND principal=?",
                (media_ref, principal),
            ).fetchone()
        if row is None:
            raise KeyError(media_ref)
        with self.store.connect() as db:
            if self.retention.availability(db, principal, media_ref)["state"] != "available":
                raise Conflict("raw recording expired; no playable original remains")
        body = json.loads(row["body"])
        if row["kind"] != "media":
            raise Conflict("gap has no media bytes")
        path = Path(row["path"]).resolve(strict=True)
        if body.get("source_fingerprint_kind") == "recording_identity":
            from .recorded_video import validate_recording

            validate_recording(body, path)
            if body.get("recording_state") == "growing":
                if not allow_growing:
                    raise Conflict("recording is still growing; sealed media required")
            else:
                self._check_bytes(path, body["facts"]["sha256"], check_active)
        else:
            if tuple(body["source_fingerprint"]) != _fingerprint(path):
                raise Conflict("registered raw input bytes changed")
            self._check_bytes(path, body["facts"]["sha256"], check_active)
        return {**body, "media_ref": row["id"], "sequence": row["sequence"], "path": path}

    def entries(self, principal, source_ref, after=-1, limit=100):
        self.source(principal, source_ref)
        if type(limit) is not int or not 1 <= limit <= 1000 or type(after) is not int or after < -1:
            raise ValueError("invalid input page")
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT * FROM managed_input_entries WHERE principal=? AND source_ref=?
                AND sequence>? ORDER BY sequence LIMIT ?""",
                (principal, source_ref, after, limit),
            ).fetchall()
            availability = {
                r["id"]: self.retention.availability(db, principal, r["id"]) for r in rows
            }
        return [
            {
                **json.loads(r["body"]),
                "media_ref": r["id"],
                "sequence": r["sequence"],
                "kind": r["kind"],
                "availability": availability[r["id"]],
            }
            for r in rows
        ]

    def window(self, principal, source_ref, item, *, sequence=None, check_active=None):
        with self.retention.lease(principal, item["media_ref"]):
            return self._window(
                principal, source_ref, item, sequence=sequence, check_active=check_active
            )

    def _window(self, principal, source_ref, item, *, sequence=None, check_active=None):
        self.source(principal, source_ref)
        raw = self.resolve(
            principal, item["media_ref"], check_active=check_active, allow_growing=True
        )
        if raw["source_ref"] != source_ref:
            raise Conflict("raw input source mismatch")
        with self.store.connect() as db:
            first = db.execute(
                "SELECT body FROM managed_input_entries WHERE source_ref=? AND json_extract(body,'$.segment_id')=? ORDER BY sequence LIMIT 1",
                (source_ref, raw["segment_id"]),
            ).fetchone()
        return InputWindow(
            event_id=raw["event_id"],
            sequence=item["sequence"] if sequence is None else sequence,
            segment_id=raw["segment_id"],
            start_ms=raw["start_ms"],
            end_ms=raw["end_ms"],
            captured_at=raw["captured_at"],
            recorded_start_at=raw["recorded_start_at"],
            media_ref=raw["media_ref"],
            modalities=tuple(raw["facts"]["modalities"]),
            source_fingerprint=tuple(raw["source_fingerprint"]),
            segment_start_ms=json.loads(first["body"])["start_ms"],
        )
