"""Immutable audiovisual references carried through native NanoBot messages."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .contracts import canonical, digest
from .store import Conflict

MEDIA_TYPE = "cix_observation_media"


class ObservationPayloads:
    def __init__(self, store, allowed_root: Path, *, required_modalities=("image", "audio")):
        if tuple(required_modalities) not in {("image",), ("audio",), ("image", "audio")}:
            raise ValueError("unsupported payload requirements")
        self.required_modalities = tuple(required_modalities)
        self.store = store
        # Bytes belong to live calls only, never the durable journal/session.
        self.memory = {}
        self.root = Path(allowed_root).resolve()
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS observation_payloads (
                id TEXT PRIMARY KEY,principal TEXT NOT NULL,task_id TEXT NOT NULL,
                body TEXT NOT NULL,created REAL NOT NULL)""")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS observation_payload_owners (
                    payload_id TEXT NOT NULL,session_key TEXT NOT NULL,PRIMARY KEY(payload_id,session_key));
                CREATE TABLE IF NOT EXISTS observation_payload_pins (
                    principal TEXT NOT NULL,task_id TEXT NOT NULL,session_key TEXT NOT NULL,payload_id TEXT NOT NULL,
                    PRIMARY KEY(principal,task_id,session_key,payload_id));
                CREATE TABLE IF NOT EXISTS observation_cache_evictions (
                    directory TEXT PRIMARY KEY,trash_path TEXT NOT NULL,state TEXT NOT NULL,
                    evidence TEXT NOT NULL,created REAL NOT NULL,reclaimed_bytes INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS observation_payload_directory ON observation_payloads(json_extract(body,'$.directory'));
                CREATE INDEX IF NOT EXISTS observation_payload_pin_lookup ON observation_payload_pins(payload_id);
            """)
            columns = {
                row[1] for row in db.execute("PRAGMA table_info(observation_cache_evictions)")
            }
            if "next_try" not in columns:
                db.execute(
                    "ALTER TABLE observation_cache_evictions ADD COLUMN next_try REAL NOT NULL DEFAULT 0"
                )
            if "error" not in columns:
                db.execute("ALTER TABLE observation_cache_evictions ADD COLUMN error TEXT")

    def register(
        self, principal, task_id, batch, prepared, *, historical=False, owner_session=None
    ):
        directory = Path(prepared["directory"]).resolve()
        if not directory.is_relative_to(self.root):
            raise Conflict("analysis payload escaped registered cache")
        manifest = prepared["manifest"]
        if (
            manifest["source_ref"] != batch.source_ref
            or manifest["start_ms"] != batch.start_ms
            or manifest["end_ms"] != batch.end_ms
            or tuple(manifest["source_fingerprint"]) != batch.source_fingerprint
        ):
            raise Conflict("analysis payload differs from captured input")
        body = {
            "batch": batch.model_dump(mode="json"),
            "directory": str(directory),
            "manifest": manifest,
            "historical": historical,
            "simulated": batch.simulated,
        }
        if prepared.get("storage") == "memory":
            body["storage"] = "memory"
        ident = "av-" + digest(body)
        volatile = prepared.get("frame_bytes") if body.get("storage") == "memory" else None
        self._validate({**body, **({"frame_bytes": volatile} if volatile is not None else {})})
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._available(db, str(directory))
            db.execute(
                "INSERT OR IGNORE INTO observation_payloads VALUES (?,?,?,?,?)",
                (ident, principal, task_id, canonical(body), self.store.clock()),
            )
            if owner_session:
                db.execute(
                    "INSERT OR IGNORE INTO observation_payload_owners VALUES (?,?)",
                    (ident, owner_session),
                )
                db.execute(
                    "INSERT OR IGNORE INTO observation_payload_pins VALUES (?,?,?,?)",
                    (principal, task_id, owner_session, ident),
                )
        if volatile is not None:
            self.memory[ident] = volatile
        return {"type": MEDIA_TYPE, "id": ident}

    def resolve(self, principal, task_id, ident):
        body = self.describe(principal, task_id, ident)
        if body.get("storage") == "memory":
            if ident not in self.memory:
                raise Conflict("in-memory images released; reread the original for a new call")
            body["frame_bytes"] = self.memory[ident]
        self._validate(body)
        return body

    @staticmethod
    def image_bytes(body, frame):
        if body.get("storage") == "memory":
            return body["frame_bytes"][frame["file"]]
        return (Path(body["directory"]) / frame["file"]).read_bytes()

    def describe(self, principal, task_id, ident):
        """Read owned metadata for budgeting, never authorize media byte use."""
        with self.store.connect() as db:
            row = db.execute(
                "SELECT body FROM observation_payloads WHERE id=? AND principal=? AND task_id=?",
                (ident, principal, task_id),
            ).fetchone()
            if row is None:
                raise KeyError(ident)
            body = json.loads(row["body"])
            self._available(db, body["directory"])
        return body

    @staticmethod
    def _available(db, directory):
        if db.execute(
            "SELECT 1 FROM observation_cache_evictions WHERE directory=?", (directory,)
        ).fetchone():
            raise Conflict(
                "analysis cache was retired; read captured media to create a new payload"
            )

    def pin_session(self, principal, task_id, session_key, messages):
        references = set(self.references(messages))
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for ident in references:
                row = db.execute(
                    "SELECT body FROM observation_payloads WHERE id=? AND principal=? AND task_id=?",
                    (ident, principal, task_id),
                ).fetchone()
                if row is None:
                    raise KeyError(ident)
                self._available(db, json.loads(row["body"])["directory"])
            db.execute(
                "DELETE FROM observation_payload_pins WHERE principal=? AND task_id=? AND session_key=?",
                (principal, task_id, session_key),
            )
            for ident in references:
                db.execute(
                    "INSERT OR IGNORE INTO observation_payload_owners VALUES (?,?)",
                    (ident, session_key),
                )
                db.execute(
                    "INSERT INTO observation_payload_pins VALUES (?,?,?,?)",
                    (principal, task_id, session_key, ident),
                )
            pinned = {row[0] for row in db.execute(
                "SELECT DISTINCT payload_id FROM observation_payload_pins"
            )}
        for ident in tuple(self.memory):
            if ident not in pinned:
                self.memory.pop(ident, None)

    def _validate(self, body):
        directory = Path(body["directory"]).resolve()
        if not directory.is_relative_to(self.root):
            raise Conflict("media cache binding changed")
        manifest = body["manifest"]
        if (
            set(manifest["missing_modalities"]) & set(self.required_modalities)
            or ("image" in self.required_modalities and not manifest["frames"])
            or ("audio" in self.required_modalities and not manifest.get("audio"))
        ):
            raise Conflict("complete required media payload is required")
        for item in [*manifest["frames"], *([manifest["audio"]] if manifest.get("audio") else [])]:
            if body.get("storage") == "memory":
                data = body.get("frame_bytes", {}).get(item["file"])
                if (not isinstance(data, bytes) or len(data) != item["bytes"]
                        or hashlib.sha256(data).hexdigest() != item["sha256"]):
                    raise Conflict("in-memory media payload bytes changed or were released")
                continue
            path = (directory / item["file"]).resolve()
            if not path.is_relative_to(directory):
                raise Conflict("media payload path escaped its directory")
            if (
                path.stat().st_size != item["bytes"]
                or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]
            ):
                raise Conflict("media payload bytes changed")

    @staticmethod
    def references(messages):
        references = []
        for message in messages:
            content = message.get("content")
            if isinstance(content, list):
                references.extend(
                    block["id"] for block in content if block.get("type") == MEDIA_TYPE
                )
            elif message.get("role") == "tool" and isinstance(content, str):
                try:
                    result = json.loads(content)
                except (ValueError, TypeError):
                    continue
                block = result.get("observation_media") if isinstance(result, dict) else None
                if isinstance(block, dict) and block.get("type") == MEDIA_TYPE:
                    references.append(block["id"])
            elif (
                message.get("role") == "user"
                and isinstance(content, str)
                and content.startswith("[程序影音输入，非用户发言]\n")
            ):
                body = json.loads(content.split("\n", 1)[1])
                block = body.get("observation_media")
                if isinstance(block, dict) and block.get("type") == MEDIA_TYPE:
                    references.append(block["id"])
        return references

    def validate_messages(self, principal, task_id, messages):
        return {
            ident: self.resolve(principal, task_id, ident)
            for ident in dict.fromkeys(self.references(messages))
        }
