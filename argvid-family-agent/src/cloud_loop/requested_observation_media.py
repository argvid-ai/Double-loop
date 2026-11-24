"""User-requested editing inputs. No automatic clipping from observation marks."""

from __future__ import annotations

import json
import shutil
import threading
import uuid
from pathlib import Path

import av

from .contracts import canonical
from .media import ffmpeg, file_hash, inspect_video, processor_versions
from .observation_attachment import requested_input_id
from .observation_media import _fingerprint
from .store import Conflict


class RequestedObservationMedia:
    def __init__(self, host):
        self.host, self.store = host, host.store
        self.root = self.store.root.resolve() / "requested-edit-inputs"
        self.lock = threading.Lock()
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS requested_edit_inputs (
                id TEXT PRIMARY KEY, principal TEXT NOT NULL, project_id TEXT NOT NULL,
                request TEXT NOT NULL,path TEXT NOT NULL,body TEXT NOT NULL)""")

    def _path(self, path):
        path = Path(path)
        if self.root.resolve() != self.root or path.resolve() != path or path.parent != self.root:
            raise Conflict("requested edit input path changed")
        return path

    def get(self, principal, ident):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM requested_edit_inputs WHERE id=? AND principal=?", (ident, principal)
            ).fetchone()
        if row is None:
            raise KeyError(ident)
        body = json.loads(row["body"])
        path = self._path(row["path"])
        if self.host.media_storage.expired(path) or not path.is_file():
            raise KeyError(ident)
        if file_hash(path) != body["sha256"]:
            raise Conflict("requested edit input bytes changed")
        return {**body, "path": path}

    def _source(self, principal, args):
        with self.store.connect() as db:
            row = db.execute(
                """SELECT m.body,t.config FROM observation_media m
                JOIN observation_tasks t ON t.task_id=m.task_id WHERE t.principal=?
                AND json_extract(t.config,'$.project_id')=? AND json_extract(m.body,'$.media_ref')=?
                AND m.kind='batch' LIMIT 1""",
                (principal, args.project_id, args.asset_id),
            ).fetchone()
            if row is None:
                raise Conflict("requested input is not captured in this user/project")
            batch, _cfg = json.loads(row["body"]), json.loads(row["config"])
            raw = db.execute(
                "SELECT path,body FROM managed_input_entries WHERE principal=? AND id=? AND kind=?",
                (principal, args.asset_id, "media"),
            ).fetchone()
        if raw:
            raw = self.host.input_media.resolve(principal, args.asset_id)
            path = raw["path"]
            offset = raw["file_start_ms"] - raw["start_ms"]
            available = (raw["start_ms"], raw["end_ms"])
        else:
            registry = self.host.observation_registry
            if registry is None:
                raise Conflict("historical replay source is not installed")
            path = registry.source_path(principal, batch["source_ref"])
            offset = 0
            spec = registry.config.sources[batch["source_ref"]]
            with av.open(str(path)) as source:
                video = source.streams.video[0]
                duration = int(video.duration * video.time_base * 1000)
            available = (spec.start_ms, spec.end_ms if spec.end_ms is not None else duration)
            if tuple(batch["source_fingerprint"]) != _fingerprint(path):
                raise Conflict("historical source no longer matches captured evidence")
        start = batch["start_ms"] if args.start_ms is None else args.start_ms
        end = batch["end_ms"] if args.end_ms is None else args.end_ms
        if not available[0] <= start < end <= available[1] or end - start > 60000:
            raise Conflict("requested span exceeds actual registered media coverage")
        return path, start + offset, end + offset, batch, start, end

    def ensure(self, principal, args, *, check_active=lambda: None):
        ident = requested_input_id(principal, args)
        path = self.root / (ident + ".mp4")
        with self.host.media_storage.pin(path):
            try:
                result = self.get(principal, ident)
                return {k: v for k, v in result.items() if k != "path"}
            except KeyError:
                pass
            with self.host.media_storage.reserve("requested-edit-inputs", self.host.settings.observation_input_file_bytes):
                result = self._ensure(principal, args, check_active=check_active)
                self.host.media_storage.register(path, "requested-edit-inputs")
                return result

    def _ensure(self, principal, args, *, check_active=lambda: None):
        ident = requested_input_id(principal, args)
        with self.lock, self.host.input_media.retention.lease(principal, args.asset_id):
            check_active()
            try:
                old = self.get(principal, ident)
                if old["project_id"] != args.project_id:
                    raise Conflict("requested input project changed")
                return {k: v for k, v in old.items() if k != "path"}
            except KeyError:
                pass
            path, start, end, batch, source_start, source_end = self._source(principal, args)
            original = _fingerprint(path)
            with av.open(str(path)) as source:
                if not source.streams.video:
                    raise Conflict(
                        "this editing executor requires video; audio-only input remains available to audio tasks"
                    )
                audio = bool(source.streams.audio)
            self.root.mkdir(exist_ok=True)
            destination = self._path(self.root / (ident + ".mp4"))
            temporary = self._path(self.root / (uuid.uuid4().hex + ".mp4"))
            settings = self.host.settings
            used = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
            if not getattr(self.store, "shared_media_storage", None) and (
                used + settings.observation_input_file_bytes
                > settings.observation_input_cache_bytes
            ):
                raise Conflict("requested editing input cache quota exhausted")

            def check():
                check_active()
                self._path(temporary)
                self._path(destination)
                if _fingerprint(path) != original:
                    raise Conflict("source changed during requested clipping")
                if (
                    temporary.exists()
                    and temporary.stat().st_size > settings.observation_input_file_bytes
                ):
                    raise Conflict("requested clip exceeds output bound")
                if shutil.disk_usage(self.root).free < settings.observation_input_min_free_bytes:
                    raise Conflict("requested clip free-space floor reached")

            try:
                # Explicit editing request: conversion is recorded, never called lossless.
                ffmpeg(
                    [
                        "-n",
                        "-seek_timestamp",
                        "1",
                        "-ss",
                        str(start / 1000),
                        "-i",
                        str(path),
                        "-t",
                        str((end - start) / 1000),
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0?",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-crf",
                        "18",
                        "-threads",
                        "2",
                        "-pix_fmt",
                        "yuv420p",
                        "-fps_mode",
                        "passthrough",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "192k",
                        "-fs",
                        str(settings.observation_input_file_bytes),
                        "-movflags",
                        "+faststart",
                        str(temporary),
                    ],
                    check_active=check,
                )
                facts = inspect_video(temporary, check_active=check)
                if abs(facts["duration_ms"] - (end - start)) > max(2, 1000 / facts["fps"]) or facts[
                    "audio_streams"
                ] != int(audio):
                    raise Conflict("requested input duration or original audio failed verification")
                check()
                manifest = {
                    "schema": "harness.requested-edit-input/1",
                    "source_media_ref": args.asset_id,
                    "source_ref": batch["source_ref"],
                    "requested_start_ms": source_start,
                    "requested_end_ms": source_end,
                    "source_file_start_ms": start,
                    "source_file_end_ms": end,
                    "source_fingerprint": list(original),
                    "output_start_ms": 0,
                    "actual_output_duration_ms": facts["duration_ms"],
                    "transformation": "bounded_video_audio_transcode",
                    "lossless": False,
                    "original_audio_present": audio,
                    "processors": processor_versions(),
                    "file_bytes_real": True,
                    "simulated": batch["simulated"],
                    "no_original_deleted": True,
                }
                body = {
                    "asset_id": ident,
                    "project_id": args.project_id,
                    "sha256": facts["sha256"],
                    "size_bytes": facts["size_bytes"],
                    "simulated": batch["simulated"],
                    "manifest": manifest,
                }
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    check()
                    if destination.exists():
                        raise Conflict("unregistered requested input already exists")
                    temporary.replace(destination)
                    db.execute(
                        "INSERT OR REPLACE INTO requested_edit_inputs VALUES (?,?,?,?,?,?)",
                        (
                            ident,
                            principal,
                            args.project_id,
                            canonical(args),
                            str(destination),
                            canonical(body),
                        ),
                    )
                return body
            finally:
                temporary.unlink(missing_ok=True)
