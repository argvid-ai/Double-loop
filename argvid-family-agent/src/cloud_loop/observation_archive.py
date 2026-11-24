"""Protected export jobs and project media access using the existing assets index.

This is the storage boundary, not the model's keep/discard policy. Only already
captured ranges can be reserved here. Open-event merging, discard eligibility,
and future-tail protection belong to the observation decision manager.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .contracts import ID, canonical, digest
from .media import file_hash
from .observation_cache import free_space_floor, pending_disk_bytes
from .observation_contracts import ObservationRun
from .observation_export import copy_observation_span
from .observation_media import _fingerprint
from .observation_store import ObservationStore
from .store import Conflict


class ObservationArchive:
    def __init__(self, ledger: ObservationStore, managed_root: Path, source_resolver):
        self.ledger, self.store = ledger, ledger.store
        self.root = Path(managed_root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.source_resolver = source_resolver
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS observation_storage (
                    id TEXT PRIMARY KEY,principal TEXT NOT NULL,project_id TEXT NOT NULL,
                    root TEXT NOT NULL,quota_bytes INTEGER NOT NULL,min_free_bytes INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS observation_exports (
                    id TEXT PRIMARY KEY,task_id TEXT NOT NULL,principal TEXT NOT NULL,project_id TEXT NOT NULL,
                    binding_ref TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,
                    source_path TEXT NOT NULL,source_fingerprint TEXT NOT NULL,relative_path TEXT NOT NULL,
                    reserved_bytes INTEGER NOT NULL,claim_id TEXT,verified TEXT,created REAL NOT NULL,error TEXT
                );
                CREATE TABLE IF NOT EXISTS observation_export_requests (
                    principal TEXT NOT NULL,id TEXT NOT NULL,body TEXT NOT NULL,export_id TEXT NOT NULL,
                    PRIMARY KEY(principal,id)
                );
            """)
            if "staging_path" not in {
                row["name"] for row in db.execute("PRAGMA table_info(observation_exports)")
            }:
                db.execute("ALTER TABLE observation_exports ADD COLUMN staging_path TEXT")

    @staticmethod
    def _id(value):
        import re

        if not isinstance(value, str) or not re.fullmatch(ID, value):
            raise ValueError("invalid storage identity")
        return value

    def bind(self, binding_ref, principal, project_id, *, quota_bytes, min_free_bytes):
        for value in (binding_ref, principal, project_id):
            self._id(value)
        if (
            type(quota_bytes) is not int
            or type(min_free_bytes) is not int
            or quota_bytes <= 0
            or min_free_bytes < 0
        ):
            raise ValueError("invalid project storage budget")
        root = (self.root / project_id / binding_ref).resolve()
        if not root.is_relative_to(self.root):
            raise ValueError("storage binding escaped managed root")
        values = (binding_ref, principal, project_id, str(root), quota_bytes, min_free_bytes)
        with self.store.connect() as db:
            old = db.execute(
                "SELECT * FROM observation_storage WHERE id=?", (binding_ref,)
            ).fetchone()
            if old and tuple(old) != values:
                raise Conflict("storage bindings are immutable; create a new binding")
            db.execute("INSERT OR IGNORE INTO observation_storage VALUES (?,?,?,?,?,?)", values)
        root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _coverage(db, task_id, source_ref, segment_id, start, end):
        ranges = []
        for row in db.execute(
            """SELECT start_ms,end_ms FROM observation_media
            WHERE task_id=? AND source_ref=? AND segment_id=? AND kind='batch' ORDER BY start_ms""",
            (task_id, source_ref, segment_id),
        ):
            if ranges and row["start_ms"] <= ranges[-1][1]:
                ranges[-1][1] = max(ranges[-1][1], row["end_ms"])
            else:
                ranges.append([row["start_ms"], row["end_ms"]])
        return next((span for span in ranges if span[0] <= start and span[1] >= end), None)

    def reserve(
        self,
        run: ObservationRun,
        *,
        event_id,
        source_ref,
        segment_id,
        start_ms,
        end_ms,
        request_id,
        evidence_ref,
        inference_simulated,
        max_output_bytes,
        _protected_event=None,
    ):
        """Persist a protected selection before any file I/O; no automatic upload."""
        run = ObservationRun.model_validate(run.model_dump())
        for value in (event_id, source_ref, segment_id, request_id, evidence_ref):
            self._id(value)
        if (
            type(start_ms) is not int
            or type(end_ms) is not int
            or not 0 <= start_ms < end_ms
            or type(max_output_bytes) is not int
            or max_output_bytes <= 0
            or type(inference_simulated) is not bool
        ):
            raise ValueError("invalid protected export")
        body = {
            **run.model_dump(),
            "event_id": event_id,
            "source_ref": source_ref,
            "segment_id": segment_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "evidence_ref": evidence_ref,
            "inference_simulated": inference_simulated,
            "max_output_bytes": max_output_bytes,
        }
        identity = {
            key: body[key]
            for key in (
                "principal",
                "task_id",
                "version",
                "event_id",
                "source_ref",
                "segment_id",
                "start_ms",
                "end_ms",
            )
        }
        export_id = "retained-" + digest(identity)[:32]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute(
                "SELECT * FROM observation_export_requests WHERE principal=? AND id=?",
                (run.principal, request_id),
            ).fetchone()
            if prior:
                if prior["body"] != canonical(body):
                    raise Conflict("export request identity collision")
                return self._get(db, run.principal, prior["export_id"])
            task = self.ledger._task(db, run.principal, run.task_id)
            if _protected_event is None:
                self.ledger._guard(task, run, running=True)
                config = json.loads(task["config"])
            else:
                event = db.execute(
                    "SELECT * FROM observation_retention_events WHERE id=?", (_protected_event,)
                ).fetchone()
                if (
                    event is None
                    or event["task_id"] != run.task_id
                    or event["status"] not in {"exporting", "saved"}
                    or event["export_plan"] != canonical(body)
                ):
                    raise Conflict("export does not match a previously protected event plan")
                snapshot = db.execute(
                    """SELECT body FROM observation_commands WHERE principal=?
                    AND json_extract(body,'$.kind')='apply' AND json_extract(body,'$.config.task_id')=?
                    AND json_extract(body,'$.config.version')=? ORDER BY created LIMIT 1""",
                    (run.principal, run.task_id, run.version),
                ).fetchone()
                if snapshot is None:
                    raise Conflict("protected event configuration is unavailable")
                config = json.loads(snapshot["body"])["config"]
            if source_ref != config["source_ref"]:
                raise Conflict("selection source differs from current task")
            binding = db.execute(
                "SELECT * FROM observation_storage WHERE id=? AND principal=? AND project_id=?",
                (config["storage_binding_ref"], run.principal, config["project_id"]),
            ).fetchone()
            if binding is None:
                raise Conflict("project storage binding unavailable")
            if self._coverage(db, run.task_id, source_ref, segment_id, start_ms, end_ms) is None:
                raise Conflict("selected range was not continuously captured")
            old = db.execute(
                "SELECT * FROM observation_exports WHERE id=?", (export_id,)
            ).fetchone()
            if old is None:
                used = db.execute(
                    """SELECT COALESCE(SUM(CASE WHEN status='saved'
                    THEN json_extract(verified,'$.bytes') ELSE reserved_bytes END),0)
                    FROM observation_exports WHERE binding_ref=?""",
                    (binding["id"],),
                ).fetchone()[0]
                if used + max_output_bytes > binding["quota_bytes"]:
                    raise Conflict("project output quota exhausted")
                if (
                    shutil.disk_usage(Path(binding["root"])).free
                    < free_space_floor(db, binding["min_free_bytes"])
                    + pending_disk_bytes(db)
                    + max_output_bytes
                ):
                    raise Conflict("pending observation writes exceed free-space floor")
                source = Path(self.source_resolver(run.principal, source_ref)).resolve(strict=True)
                if not source.is_file():
                    raise ValueError("registered source is not a file")
                fingerprint = list(_fingerprint(source))
                captures = db.execute(
                    """SELECT body FROM observation_media WHERE task_id=? AND source_ref=?
                    AND segment_id=? AND kind='batch' AND start_ms<? AND end_ms>?""",
                    (run.task_id, source_ref, segment_id, end_ms, start_ms),
                ).fetchall()
                if not captures or any(
                    json.loads(row["body"]).get("source_fingerprint") != fingerprint
                    for row in captures
                ):
                    raise Conflict("source identity differs from captured media evidence")
                stamp = db.execute(
                    """SELECT body FROM observation_media WHERE task_id=? AND source_ref=?
                    AND segment_id=? AND kind='batch' AND start_ms<=? AND end_ms>? ORDER BY sequence LIMIT 1""",
                    (run.task_id, source_ref, segment_id, start_ms, start_ms),
                ).fetchone()
                captured = json.loads(stamp["body"])
                source_time = captured["captured_at"] - (captured["end_ms"] - start_ms) / 1000
                day = datetime.fromtimestamp(source_time, ZoneInfo("Asia/Shanghai")).strftime(
                    "%Y-%m-%d"
                )
                relative = str(Path("media") / run.task_id / day / (export_id + ".mp4"))
                body["simulated"] = captured["simulated"]
                db.execute(
                    """INSERT INTO observation_exports
                    (id,task_id,principal,project_id,binding_ref,body,status,source_path,source_fingerprint,
                     relative_path,reserved_bytes,claim_id,verified,created,error)
                    VALUES (?,?,?,?,?,?,'protected',?,?,?,?,NULL,NULL,?,NULL)""",
                    (
                        export_id,
                        run.task_id,
                        run.principal,
                        config["project_id"],
                        binding["id"],
                        canonical(body),
                        str(source),
                        canonical(fingerprint),
                        relative,
                        max_output_bytes,
                        self.store.clock(),
                    ),
                )
            # Request body deliberately excludes derived source fields for stable retry identity.
            request_body = {key: value for key, value in body.items() if key != "simulated"}
            db.execute(
                "INSERT INTO observation_export_requests VALUES (?,?,?,?)",
                (run.principal, request_id, canonical(request_body), export_id),
            )
            self.store._audit(
                db,
                run.task_id,
                "observation.export.protected",
                {"export_id": export_id, "selection": body},
            )
            return self._get(db, run.principal, export_id)

    @staticmethod
    def _get(db, principal, export_id):
        row = db.execute(
            "SELECT * FROM observation_exports WHERE id=? AND principal=?", (export_id, principal)
        ).fetchone()
        if row is None:
            raise KeyError(export_id)
        return {
            key: value
            for key, value in {
                **dict(row),
                "body": json.loads(row["body"]),
                "verified": json.loads(row["verified"]) if row["verified"] else None,
            }.items()
            if key not in {"source_path", "source_fingerprint", "claim_id", "staging_path"}
        }

    def get(self, principal, export_id):
        with self.store.connect() as db:
            return self._get(db, principal, export_id)

    def _path(self, binding, relative):
        root = Path(binding["root"]).resolve()
        path = (root / relative).resolve()
        if not root.is_relative_to(self.root) or not path.is_relative_to(root):
            raise Conflict("media path escaped project storage")
        return root, path

    @contextmanager
    def _writer(self, principal, export_id):
        self._id(export_id)
        with self.store.connect() as db:
            job = self._get(db, principal, export_id)
            binding = db.execute(
                "SELECT * FROM observation_storage WHERE id=?", (job["binding_ref"],)
            ).fetchone()
            root, _ = self._path(binding, job["relative_path"])
        locks = root / "state" / "export-locks"
        locks.mkdir(parents=True, exist_ok=True)
        if not locks.resolve().is_relative_to(root):
            raise Conflict("export lock directory escaped storage")
        with (locks / (export_id + ".lock")).open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Conflict("export has a live writer") from None
            yield

    def _remove_staging(self, row, binding):
        if not row["staging_path"]:
            return
        path = Path(row["staging_path"])
        root, _ = self._path(binding, row["relative_path"])
        if path.parent != root or not path.name.startswith(".retaining-") or path.is_symlink():
            raise Conflict("untrusted interrupted staging path")
        if Path(row["source_path"]).resolve().is_relative_to(path.resolve()):
            raise Conflict("staging cleanup would remove a source")
        if path.exists():
            shutil.rmtree(path)
        with self.store.connect() as db:
            db.execute("UPDATE observation_exports SET staging_path=NULL WHERE id=?", (row["id"],))

    def materialize(self, principal, export_id):
        with self._writer(principal, export_id):
            return self._materialize(principal, export_id)

    def _materialize(self, principal, export_id):
        """Finalize an existing protection even after observation stopped.

        A persisted verified receipt precedes publication. If the process dies
        after linking the file but before indexing it, a retry verifies that same
        file and completes indexing; it never overwrites or invents a success.
        Host scheduling must serialize materializers; task capture remains separate.
        """
        claim = uuid.uuid4().hex
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._get(db, principal, export_id)
            row = dict(
                db.execute("SELECT * FROM observation_exports WHERE id=?", (export_id,)).fetchone()
            )
            if row["status"] == "saved":
                return self._verify_saved(db, row)
            binding = dict(
                db.execute(
                    "SELECT * FROM observation_storage WHERE id=?", (row["binding_ref"],)
                ).fetchone()
            )
            effective_floor = free_space_floor(db, binding["min_free_bytes"]) + pending_disk_bytes(
                db, exclude_export=export_id
            )
            body = json.loads(row["body"])
            task = self.ledger._task(db, principal, row["task_id"])
            stopped = task["status"] == "stopped"
            available = self._coverage(
                db,
                row["task_id"],
                body["source_ref"],
                body["segment_id"],
                body["start_ms"],
                body["end_ms"],
            )
            if available is None:
                raise Conflict("protected media is no longer indexed")
            root, destination = self._path(binding, row["relative_path"])
            if destination.exists():
                if not row["verified"]:
                    raise Conflict("unverified file occupies export destination")
                info = json.loads(row["verified"])
                if file_hash(destination) != info["sha256"]:
                    raise Conflict("interrupted published file changed")
                self._index(db, row, info, destination)
                reused = True
            else:
                reused = False
            if not reused:
                db.execute(
                    "UPDATE observation_exports SET status='preparing',claim_id=?,error=NULL WHERE id=?",
                    (claim, export_id),
                )
        self._remove_staging(row, binding)
        if reused:
            return self.get(principal, export_id)

        def guard():
            with self.store.connect() as db:
                current = db.execute(
                    "SELECT status,claim_id FROM observation_exports WHERE id=?", (export_id,)
                ).fetchone()
            if (
                current is None
                or current["status"] not in {"preparing", "verified"}
                or current["claim_id"] != claim
            ):
                raise Conflict("export writer no longer owns this protection")

        def verified(info):
            guard()
            with self.store.connect() as db:
                changed = db.execute(
                    "UPDATE observation_exports SET status='verified',verified=? WHERE id=? AND claim_id=?",
                    (canonical(info), export_id, claim),
                ).rowcount
                if not changed:
                    raise Conflict("export writer superseded")

        def staging(path):
            guard()
            with self.store.connect() as db:
                db.execute(
                    "UPDATE observation_exports SET staging_path=? WHERE id=? AND claim_id=?",
                    (str(path), export_id, claim),
                )

        def publish(temporary, destination, info):
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT status,claim_id FROM observation_exports WHERE id=?", (export_id,)
                ).fetchone()
                if current["status"] != "verified" or current["claim_id"] != claim:
                    raise Conflict("export publication lost ownership")
                os.link(temporary, destination)
                directory = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
                self._index(db, row, info, destination)

        try:
            if list(_fingerprint(Path(row["source_path"]))) != json.loads(
                row["source_fingerprint"]
            ):
                raise Conflict("protected source identity changed")
            copy_observation_span(
                Path(row["source_path"]),
                root,
                row["relative_path"],
                start_ms=body["start_ms"],
                end_ms=body["end_ms"],
                available_start_ms=available[0],
                available_end_ms=available[1],
                source_ref=body["source_ref"],
                simulated=body["simulated"],
                max_output_bytes=row["reserved_bytes"],
                min_free_bytes=effective_floor,
                check_active=guard,
                on_staging=staging,
                on_verified=verified,
                publish=publish,
                allow_boundary_transcode=stopped,
                allow_capture_head_transcode=True,
            )
        except Exception as exc:
            with self.store.connect() as db:
                db.execute(
                    """UPDATE observation_exports SET status=CASE WHEN verified IS NOT NULL THEN 'verified' ELSE 'protected' END,
                    claim_id=NULL,error=? WHERE id=? AND claim_id=? AND status!='saved' """,
                    (type(exc).__name__, export_id, claim),
                )
            raise
        return self.get(principal, export_id)

    def _index(self, db, row, info, path):
        os.chmod(path, 0o444)
        manifest = {
            **info,
            "project_id": row["project_id"],
            "task_id": row["task_id"],
            "selection": json.loads(row["body"]),
            "asset": {
                "asset_id": row["id"],
                "sha256": info["sha256"],
                "size_bytes": info["bytes"],
                "file_fingerprint": list(_fingerprint(path)),
                **info["specification"],
                "duration_ms": round(
                    (info["output_video_end_seconds"] - info["output_video_start_seconds"]) * 1000
                ),
            },
        }
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='observation_retention_events'"
        ).fetchone():
            event = db.execute(
                "SELECT * FROM observation_retention_events WHERE export_id=?", (row["id"],)
            ).fetchone()
            if event:
                manifest["event_context"] = {
                    "event_id": event["id"],
                    "desired_start_ms": event["start_ms"],
                    "desired_end_ms": event["end_ms"],
                    "core_start_ms": event["core_start_ms"],
                    "core_end_ms": event["core_end_ms"],
                    "saved_start_ms": info["requested_start_ms"],
                    "saved_end_ms": info["requested_end_ms"],
                    "post_roll_complete": info["requested_end_ms"] >= event["end_ms"],
                    "missing_post_roll_ms": max(0, event["end_ms"] - info["requested_end_ms"]),
                }
        old = db.execute("SELECT path,info FROM assets WHERE id=?", (row["id"],)).fetchone()
        if old and (old["path"] != str(path) or old["info"] != canonical(manifest)):
            raise Conflict("media index identity collision")
        db.execute(
            "INSERT OR IGNORE INTO assets VALUES (?,?,?,?,?)",
            (row["id"], row["task_id"], str(path), canonical(manifest), "observation"),
        )
        db.execute(
            "UPDATE observation_exports SET status='saved',verified=?,claim_id=NULL,error=NULL WHERE id=?",
            (canonical(info), row["id"]),
        )
        self.store._audit(
            db,
            row["task_id"],
            "observation.export.saved",
            {
                "asset_id": row["id"],
                "sha256": info["sha256"],
                "simulated": info["simulated"],
                "cloud_upload": False,
                "cloud_model_invoked": False,
            },
        )

    def _verify_saved(self, db, row):
        asset = db.execute("SELECT path,info FROM assets WHERE id=?", (row["id"],)).fetchone()
        binding = db.execute(
            "SELECT * FROM observation_storage WHERE id=?", (row["binding_ref"],)
        ).fetchone()
        _, expected = self._path(binding, row["relative_path"])
        if asset is None or Path(asset["path"]) != expected or not expected.is_file():
            raise Conflict("saved media differs from its committed index")
        info = json.loads(asset["info"])
        fingerprint = list(_fingerprint(expected))
        if fingerprint != info["asset"].get("file_fingerprint"):
            if file_hash(expected) != json.loads(row["verified"])["sha256"]:
                raise Conflict("saved media differs from its committed index")
            if list(_fingerprint(expected)) != fingerprint:
                raise Conflict("saved media changed during verification")
            info["asset"]["file_fingerprint"] = fingerprint
            db.execute("UPDATE assets SET info=? WHERE id=?", (canonical(info), row["id"]))
        return self._get(db, row["principal"], row["id"])

    def recover_writer(self, principal, export_id):
        """Exclusive writer lock proves no earlier export callback is still active."""
        with self._writer(principal, export_id):
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._get(db, principal, export_id)
                db.execute(
                    """UPDATE observation_exports SET status=CASE WHEN verified IS NULL THEN 'protected' ELSE 'verified' END,
                    claim_id=NULL WHERE id=? AND status IN ('preparing','verified')""",
                    (export_id,),
                )
                return self._get(db, principal, export_id)

    def list_media(self, principal, project_id):
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT a.id,a.info,e.relative_path,e.created FROM assets a JOIN observation_exports e ON e.id=a.id
                WHERE e.principal=? AND e.project_id=? AND e.status='saved' ORDER BY e.created,a.id""",
                (principal, project_id),
            ).fetchall()
        return [{**dict(row), "info": json.loads(row["info"])} for row in rows]

    def media_path(self, principal, asset_id):
        with self.store.connect() as db:
            public = self._get(db, principal, asset_id)
            if public["status"] != "saved":
                raise KeyError(asset_id)
            row = dict(
                db.execute("SELECT * FROM observation_exports WHERE id=?", (asset_id,)).fetchone()
            )
            self._verify_saved(db, row)
            binding = db.execute(
                "SELECT * FROM observation_storage WHERE id=?", (row["binding_ref"],)
            ).fetchone()
            return self._path(binding, row["relative_path"])[1]
