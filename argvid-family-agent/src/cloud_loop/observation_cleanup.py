"""Retire only unreferenced, fully discarded derived AV caches.

Original media and retained videos are outside this managed root. Manifests and
actual-message evidence stay in SQLite after files are removed. A durable claim
and quarantine rename make cleanup recoverable without reviving stale payloads.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from .contracts import ID, canonical, digest
from .observation_decisions import _covered
from .observation_media import _fingerprint
from .observation_payloads import ObservationPayloads
from .store import Conflict


class ObservationCacheCleanup:
    def __init__(self, decisions, root, source_resolver, *, captured_resolver=None):
        self.decisions, self.store = decisions, decisions.store
        self.root = Path(root).resolve()
        self.source_resolver = source_resolver
        self.captured_resolver = captured_resolver
        self.payloads = ObservationPayloads(self.store, self.root)
        self.trash = self.root / ".retired"
        self.cursor = ""

    def _scope(self, body):
        directory = Path(body["directory"])
        batch = body["batch"]
        if (
            directory.resolve() != directory
            or not directory.is_relative_to(self.root)
            or len(directory.relative_to(self.root).parts) != 3
            or directory.parent.parent.name != batch["task_id"]
            or directory.name != batch["batch_id"]
            or not re.fullmatch(ID, directory.parent.name)
        ):
            raise Conflict("cache cleanup path is not an owned analysis batch")
        if self.root.resolve() != self.root or self.trash.resolve() != self.trash:
            raise Conflict("cache root changed")
        return directory

    def _eligible(self, db, body):
        batch = body["batch"]
        directory = self._scope(body)
        rows = db.execute(
            "SELECT id,principal,task_id FROM observation_payloads WHERE json_extract(body,'$.directory')=?",
            (str(directory),),
        ).fetchall()
        if not rows:
            return False
        for row in rows:
            if row["principal"] != batch["principal"] or row["task_id"] != batch["task_id"]:
                return False
            if not db.execute(
                "SELECT 1 FROM observation_payload_owners WHERE payload_id=?", (row["id"],)
            ).fetchone():
                return False
            if db.execute(
                "SELECT 1 FROM observation_payload_pins WHERE payload_id=?", (row["id"],)
            ).fetchone():
                return False
        captured = self.decisions.ledger._batch(db, batch["task_id"], batch["batch_id"])
        if captured["seen"] is None:
            return False
        if not db.execute(
            "SELECT 1 FROM observation_retention_policies WHERE task_id=? AND version=?",
            (batch["task_id"], batch["version"]),
        ).fetchone():
            return False
        marks = db.execute(
            """SELECT d.start_ms,d.end_ms,d.decision,r.disposition
            FROM observation_decisions d LEFT JOIN observation_candidate_reviews r
            ON r.marker_id=d.id AND r.revision=d.revision WHERE d.task_id=? AND d.batch_id=?""",
            (batch["task_id"], batch["batch_id"]),
        ).fetchall()
        disposable_images = bool(
            getattr(self.store, "shared_media_storage", None)
            and not body["manifest"].get("audio")
        )
        if not marks or (not disposable_images and any(
            row["decision"] != "discard" and row["disposition"] != "release" for row in marks
        )):
            return False
        if not _covered(
            [(r["start_ms"], r["end_ms"]) for r in marks], batch["start_ms"], batch["end_ms"]
        ):
            return False
        protected = self.decisions.protected_ranges(
            batch["principal"],
            batch["task_id"],
            batch["source_ref"],
            batch["segment_id"],
            window=(batch["start_ms"], batch["end_ms"]),
        )
        if not disposable_images and any(
            r["start_ms"] < batch["end_ms"] and r["end_ms"] > batch["start_ms"] for r in protected
        ):
            return False
        if disposable_images:
            # Processed, unpinned pictures are not the retained original. Their
            # disposal must not depend on an old camera still being installed,
            # nor on the original still being in the rolling recording queue.
            return bool(db.execute(
                "SELECT 1 FROM observation_cache_reservations WHERE path=?",
                (str(directory),),
            ).fetchone())
        if body["manifest"].get("source_fingerprint_kind") == "recording_identity":
            raw_state = db.execute(
                "SELECT body FROM managed_input_entries WHERE principal=? AND id=?",
                (batch["principal"], batch["media_ref"]),
            ).fetchone()
            if raw_state is None or json.loads(raw_state[0]).get("recording_state") != "sealed":
                return False  # A growing original is expected, not a cleanup failure.
        try:
            source = Path(
                self.captured_resolver(batch)
                if self.captured_resolver
                else self.source_resolver(batch["principal"], batch["source_ref"])
            ).resolve(strict=True)
        except (KeyError, FileNotFoundError):
            return False  # Unverifiable legacy AV must not block unrelated cleanup.
        if source.is_relative_to(self.root):
            return False
        if body["manifest"].get("source_fingerprint_kind") == "recording_identity":
            raw = db.execute(
                "SELECT body FROM managed_input_entries WHERE principal=? AND id=?",
                (batch["principal"], batch["media_ref"]),
            ).fetchone()
            if (
                raw is None
                or json.loads(raw[0])["source_fingerprint"] != batch["source_fingerprint"]
            ):
                return False
            from .recorded_video import validate_recording

            validate_recording(json.loads(raw[0]), source)
        elif list(_fingerprint(source)) != batch["source_fingerprint"]:
            return False
        return bool(
            db.execute(
                "SELECT 1 FROM observation_cache_reservations WHERE path=?", (str(directory),)
            ).fetchone()
        )

    def _files(self, directory, body, *, partial=False):
        manifest = body["manifest"]
        allowed = {
            item["file"]: item
            for item in [
                *manifest["frames"],
                *([manifest["audio"]] if manifest.get("audio") else []),
            ]
        }
        allowed_dirs = {"native-audio"}
        if manifest.get("source_kind") == "managed_capture":
            # A joined batch can cross a file boundary without sampling an
            # image in every part; those numbered directories may be empty.
            allowed_dirs.update(str(i) for i, _ in enumerate(manifest.get("raw_media_parts", [])))
        for name in allowed:
            relative = Path(name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative.as_posix() != name
                or not relative.name
            ):
                raise Conflict("analysis manifest file escaped its batch")
            allowed_dirs.update(
                parent.as_posix() for parent in relative.parents if parent != Path(".")
            )
        manifest_path = directory / "manifest.json"
        if (not partial or manifest_path.exists()) and json.loads(
            manifest_path.read_text()
        ) != manifest:
            raise Conflict("analysis manifest changed before cleanup")
        for name, record in allowed.items():
            path = directory / name
            if partial and not path.exists() and not path.is_symlink():
                continue
            if (
                not path.is_file()
                or path.is_symlink()
                or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]
            ):
                raise Conflict("analysis bytes changed before cleanup")
        total = 0
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise Conflict("cleanup cannot follow symlinks")
            relative = path.relative_to(directory)
            if path.is_dir():
                if relative.as_posix() not in allowed_dirs:
                    raise Conflict("unknown directory in analysis cache")
                continue
            if not path.is_file() or path.stat().st_nlink != 1:
                raise Conflict("cleanup requires ordinary unshared derived files")
            if relative.as_posix() in {*allowed, "manifest.json"}:
                pass
            elif relative.parent.as_posix() == "native-audio" and re.fullmatch(
                r"[0-9a-f]{64}\.wav", relative.name
            ):
                if hashlib.sha256(path.read_bytes()).hexdigest() != relative.stem:
                    raise Conflict("native audio cache changed")
            else:
                raise Conflict("untracked file in analysis cache")
            total += path.stat().st_size
        return total

    def _claim(self):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute(
                "SELECT * FROM observation_cache_evictions WHERE state IN ('claimed','quarantined') AND next_try<=? ORDER BY next_try,created LIMIT 1",
                (self.store.clock(),),
            ).fetchone()
            if pending:
                return dict(pending)
            rows = db.execute(
                """SELECT p.* FROM observation_payloads p WHERE p.id>?
                AND COALESCE(json_extract(p.body,'$.storage'),'disk')!='memory' AND NOT EXISTS (
                SELECT 1 FROM observation_cache_evictions e WHERE e.directory=json_extract(p.body,'$.directory'))
                ORDER BY p.id LIMIT 32""",
                (self.cursor,),
            ).fetchall()
            if not rows:
                self.cursor = ""
                return None
            for row in rows:
                self.cursor = row["id"]
                body = json.loads(row["body"])
                if not self._eligible(db, body):
                    continue
                directory = self._scope(body)
                size = self._files(directory, body)
                trash = self.trash / digest(str(directory))
                evidence = {
                    "payload": body,
                    "bytes": size,
                    "reason": "seen_complete_unpinned_shared_images"
                    if getattr(self.store, "shared_media_storage", None)
                    else "seen_complete_discard_expired_unpinned",
                }
                db.execute(
                    "INSERT INTO observation_cache_evictions(directory,trash_path,state,evidence,created) VALUES (?,?,?,?,?)",
                    (
                        str(directory),
                        str(trash),
                        "claimed",
                        canonical(evidence),
                        self.store.clock(),
                    ),
                )
                return dict(
                    db.execute(
                        "SELECT * FROM observation_cache_evictions WHERE directory=?",
                        (str(directory),),
                    ).fetchone()
                )
        return None

    def run_once(self):
        claim = self._claim()
        if claim is None:
            return False
        try:
            return self._run_claim(claim)
        except Exception as exc:
            with self.store.connect() as db:
                db.execute(
                    "UPDATE observation_cache_evictions SET next_try=?,error=? WHERE directory=?",
                    (self.store.clock() + 60, type(exc).__name__, claim["directory"]),
                )
            raise

    def _run_claim(self, claim):
        evidence = json.loads(claim["evidence"])
        body = evidence["payload"]
        directory = self._scope(body)
        trash = Path(claim["trash_path"])
        if trash != self.trash / digest(str(directory)) or trash.is_symlink():
            raise Conflict("cleanup quarantine changed")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT state FROM observation_cache_evictions WHERE directory=?", (str(directory),)
            ).fetchone()
            if current[0] == "deleted":
                return False
            if current[0] == "claimed":
                if not self._eligible(db, body):
                    if trash.exists():
                        self._files(trash, body)
                        if directory.exists():
                            raise Conflict("cannot restore cache over an existing directory")
                        os.rename(trash, directory)
                    db.execute(
                        "DELETE FROM observation_cache_evictions WHERE directory=?",
                        (str(directory),),
                    )
                    return False
                self.trash.mkdir(exist_ok=True)
                if directory.exists():
                    self._files(directory, body)
                    if trash.exists():
                        raise Conflict("cleanup quarantine already occupied")
                    os.rename(directory, trash)
                elif not trash.exists():
                    raise Conflict("claimed analysis cache disappeared")
                self._files(trash, body)
                db.execute(
                    "UPDATE observation_cache_evictions SET state='quarantined' WHERE directory=?",
                    (str(directory),),
                )
        # The rename committed logical eviction under the same write lock as
        # model pins/decisions. New keep decisions use original media, not this
        # retired derivative. A crash here resumes deletion of this quarantine.
        if trash.exists():
            # A previous delete may have stopped after removing some files.
            # Validate every survivor; quarantine is not permission to remove
            # new or modified files introduced since the durable claim.
            self._files(trash, body, partial=True)
            shutil.rmtree(trash)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM observation_cache_reservations WHERE path=?", (str(directory),))
            db.execute(
                "UPDATE observation_cache_evictions SET state='deleted',error=NULL,reclaimed_bytes=? WHERE directory=?",
                (evidence["bytes"], str(directory)),
            )
            self.store._audit(
                db,
                body["batch"]["task_id"],
                "observation.cache.deleted",
                {
                    "batch_id": body["batch"]["batch_id"],
                    "directory": str(directory),
                    "bytes": evidence["bytes"],
                    "source_ref": body["batch"]["source_ref"],
                    "original_deleted": False,
                    "retained_media_deleted": False,
                    "simulated": body["simulated"],
                    "reason": evidence["reason"],
                },
            )
        return {
            "status": "cache_deleted",
            "reclaimed_bytes": evidence["bytes"],
            "batch_id": body["batch"]["batch_id"],
        }
