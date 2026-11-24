"""Lossless compressed model-call history with searchable SQLite envelopes."""

import gzip
import hashlib
import json
import os
from pathlib import Path

from .contracts import canonical
from .store import Conflict


def restore_payload(root, payload):
    value = json.loads(payload) if isinstance(payload, str) else payload
    reference = value.get("_archived_model_payload")
    if reference is None:
        return value
    folder = Path(root).resolve() / "audit-archive"
    path = folder / reference["file"]
    if path.resolve() != path or path.parent != folder or not path.is_file():
        raise Conflict("archived model payload is unavailable")
    size = reference["bytes"]
    if type(size) is not int or not 0 < size <= 128 * 1024**2:
        raise Conflict("invalid archived payload size")
    with gzip.open(path, "rb") as stream:
        data = stream.read(size + 1)
    if len(data) != size or hashlib.sha256(data).hexdigest() != reference["sha256"]:
        raise Conflict("archived model payload changed")
    return json.loads(data)


class AuditArchive:
    def __init__(self, store, settings):
        self.store, self.settings = store, settings
        self.root = store.root / "audit-archive"

    def run_once(self):
        if self.settings.audit_archive_after_seconds is None:
            return {"enabled": False, "archived": 0}
        cutoff = self.store.clock() - self.settings.audit_archive_after_seconds
        with self.store.connect() as db:
            rows = db.execute(
                """SELECT a.* FROM audit a
                WHERE a.kind IN ('model.request','model.response') AND a.created<?
                AND json_extract(a.payload,'$._archived_model_payload') IS NULL
                AND NOT EXISTS (SELECT 1 FROM harness_events e
                    WHERE e.id=json_extract(a.payload,'$.event_id') AND e.status IN ('queued','running'))
                ORDER BY a.seq LIMIT ?""",
                (cutoff, self.settings.audit_archive_batch_rows),
            ).fetchall()
        self.root.mkdir(exist_ok=True)
        if self.root.resolve() != self.root:
            raise Conflict("archive root changed")
        used = sum(p.stat().st_size for p in self.root.glob("*.json.gz") if p.is_file())
        archived, original_bytes, compressed_bytes = 0, 0, 0
        for row in rows:
            original = row["payload"].encode()
            if len(original) > 128 * 1024**2:
                continue
            checksum = hashlib.sha256(original).hexdigest()
            name = str(row["seq"]) + "-" + checksum + ".json.gz"
            destination = self.root / name
            packed = gzip.compress(original, compresslevel=3, mtime=0)
            already_present = destination.exists()
            if (
                used + (0 if already_present else len(packed))
                > self.settings.audit_archive_budget_bytes
            ):
                return {
                    "enabled": True,
                    "archived": archived,
                    "budget_exhausted": True,
                    "original_bytes": original_bytes,
                    "compressed_bytes": compressed_bytes,
                }
            temporary = destination.with_suffix(".pending")
            try:
                with temporary.open("wb") as output:
                    output.write(packed)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary, destination)
                value = json.loads(row["payload"])
                envelope = {
                    key: item
                    for key, item in value.items()
                    if isinstance(item, (str, int, float, bool)) and len(str(item)) <= 512
                }
                envelope["_archived_model_payload"] = {
                    "file": name,
                    "sha256": checksum,
                    "bytes": len(original),
                }
                with self.store.connect() as db:
                    changed = db.execute(
                        "UPDATE audit SET payload=? WHERE seq=? AND payload=?",
                        (canonical(envelope), row["seq"], row["payload"]),
                    ).rowcount
                if changed:
                    archived += 1
                    original_bytes += len(original)
                    compressed_bytes += len(packed)
                    if not already_present:
                        used += len(packed)
            finally:
                temporary.unlink(missing_ok=True)
        return {
            "enabled": True,
            "archived": archived,
            "budget_exhausted": False,
            "original_bytes": original_bytes,
            "compressed_bytes": compressed_bytes,
        }
