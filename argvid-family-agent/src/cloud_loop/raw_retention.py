"""Space/age retention for explicitly owned recordings, never imported originals.

Publication and eviction share SQLite transactions. File leases cover actual
readers, including cancelled-but-still-running inference. Tombstones preserve
coverage and survive a crash between recording intent and unlinking bytes.
"""

from __future__ import annotations

import fcntl
import json
import shutil
import threading
from contextlib import ExitStack, contextmanager
from datetime import datetime
from functools import wraps
from pathlib import Path

from pydantic import Field, model_validator

from .contracts import ID, Contract, canonical
from .observation_media import _fingerprint
from .store import Conflict


class RawRecordingExpired(Conflict):
    """An indexed range exists, but its owned original has expired."""


class RawRetentionPolicy(Contract):
    enabled: bool = False
    quota_bytes: int | None = Field(default=100_000_000_000, strict=True, gt=0)
    max_age_seconds: int | None = Field(default=None, strict=True, gt=0)
    high_water: float = Field(default=0.95, gt=0, le=1)
    low_water: float = Field(default=0.85, gt=0, lt=1)
    review_water: float = Field(default=0.90, gt=0, lt=1)
    review_task_id: str | None = Field(
        default=None,
        pattern=ID,
        description="主助理已存在的长期goal事项ID；达到整理水位时唤醒此事项审阅原片保护。空值不安排模型整理。",
    )
    min_free_bytes: int = Field(default=1024**3, strict=True, ge=0)
    allow_unanalysed_expiry: bool = True

    @model_validator(mode="after")
    def bounded(self):
        if self.enabled and self.quota_bytes is None and self.max_age_seconds is None:
            raise ValueError("rolling recording requires capacity or age retention")
        if self.low_water >= self.high_water:
            raise ValueError("low water must be below high water")
        if self.review_task_id and not self.low_water < self.review_water < self.high_water:
            raise ValueError("review water must be between low and high water")
        return self


class ConfigureRawRetention(Contract):
    expected_revision: int = Field(strict=True, ge=0)
    policy: RawRetentionPolicy


class RawRetention:
    def __init__(self, media):
        self.media, self.store = media, media.store
        from .harness_health import RuntimeHealth

        self.health = RuntimeHealth(self.store)
        self.last_usage = None
        from .observation_candidates import CandidateReviews

        self.candidate_reviews = CandidateReviews(self.store)
        self.lock = threading.RLock()
        self.leases = media.root / "leases"
        self.leases.mkdir(exist_ok=True)
        if self.leases.resolve() != self.leases:
            raise Conflict("raw lease directory escaped node state")
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS raw_retention_policy (
                    principal TEXT PRIMARY KEY,revision INTEGER NOT NULL,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS raw_recordings (
                    media_ref TEXT PRIMARY KEY,principal TEXT NOT NULL,state TEXT NOT NULL,
                    owned_path TEXT NOT NULL,recorded REAL,ingested REAL NOT NULL,
                    protected INTEGER NOT NULL DEFAULT 0,reason TEXT,expired REAL);
                CREATE INDEX IF NOT EXISTS raw_recordings_lifecycle
                    ON raw_recordings(principal,state,expired);
            """)

    def configuration(self, principal):
        shared = getattr(self.store, "shared_media_storage", None)
        if shared:
            c = shared.config
            return {
                "revision": 1, "owner": "shared_media_storage",
                "policy": RawRetentionPolicy(
                    enabled=True, quota_bytes=c.raw_bytes, min_free_bytes=c.min_free_bytes,
                    low_water=c.target_bytes/c.raw_bytes,
                    review_water=c.review_bytes/c.raw_bytes,
                    high_water=c.collect_bytes/c.raw_bytes,
                    review_task_id=c.review_task_id,
                ).model_dump(),
                "shared_total_bytes": c.total_bytes,
                "review_grace_seconds": c.review_grace_seconds,
                "candidate_markers_alone_protect_raw": False,
            }
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM raw_retention_policy WHERE principal=?", (principal,)
            ).fetchone()
        return {
            "revision": row["revision"] if row else 0,
            "policy": json.loads(row["body"]) if row else RawRetentionPolicy().model_dump(),
        }

    def history(self, principal):
        """Read committed expirations without collecting or deleting anything."""
        with self.store.connect() as db:
            totals = db.execute(
                """SELECT COUNT(*) AS files,COALESCE(SUM(e.bytes),0) AS bytes
                FROM raw_recordings r JOIN managed_input_entries e ON e.id=r.media_ref
                WHERE r.principal=? AND r.state='expired'""",
                (principal,),
            ).fetchone()
            rows = db.execute(
                """SELECT r.media_ref,r.reason,r.expired,e.body,e.bytes
                FROM raw_recordings r JOIN managed_input_entries e ON e.id=r.media_ref
                WHERE r.principal=? AND r.state='expired' ORDER BY r.expired DESC,r.media_ref LIMIT 20""",
                (principal,),
            ).fetchall()
        recent = []
        for row in rows:
            body = json.loads(row["body"])
            recent.append(
                {
                    "media_ref": row["media_ref"],
                    "reason": row["reason"],
                    "expired_at": row["expired"],
                    "bytes": row["bytes"],
                    "source_ref": body["source_ref"],
                    "start_ms": body["start_ms"],
                    "end_ms": body["end_ms"],
                }
            )
        return {
            "expired_files": totals["files"],
            "expired_bytes": totals["bytes"],
            "recent": recent,
            "recent_limit": 20,
        }

    def configure(self, principal, config):
        if getattr(self.store, "shared_media_storage", None):
            raise Conflict("shared recording policy belongs to the enrolled shared storage configuration")
        config = ConfigureRawRetention.model_validate(config.model_dump())
        with self.lock, self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM raw_retention_policy WHERE principal=?", (principal,)
            ).fetchone()
            revision = old["revision"] if old else 0
            # Safe retry of an already committed version; mismatched stale writes fail.
            if (
                old
                and config.expected_revision + 1 == revision
                and old["body"] == canonical(config.policy)
            ):
                return {"applied": True, **self.configuration(principal)}
            if revision != config.expected_revision:
                raise Conflict("raw retention revision changed")
            db.execute(
                "INSERT OR REPLACE INTO raw_retention_policy VALUES (?,?,?)",
                (principal, revision + 1, canonical(config.policy)),
            )
        return {"applied": True, **self.configuration(principal)}

    def register(self, db, ident, principal, body, path):
        path = Path(path)
        if path.parent != self.media.root / "raw" or path.resolve() != path or path.is_symlink():
            raise Conflict("only node-owned raw copies can enter rolling retention")
        recorded = body.get("recorded_start_at")
        recorded = datetime.fromisoformat(recorded).timestamp() if recorded else None
        source = db.execute(
            "SELECT spec FROM managed_inputs WHERE id=? AND principal=?",
            (body["source_ref"], principal),
        ).fetchone()
        if source is None:
            raise Conflict("owned recording has no registered source")
        protected = bool(json.loads(source["spec"]).get("protect_recordings", False))
        db.execute(
            "INSERT INTO raw_recordings(media_ref,principal,state,owned_path,recorded,ingested,protected) VALUES (?,?,?,?,?,?,?)",
            (ident, principal, "available", str(path), recorded, body["captured_at"], int(protected)),
        )

    def availability(self, db, principal, ident):
        row = db.execute(
            "SELECT state,reason,expired FROM raw_recordings WHERE principal=? AND media_ref=?",
            (principal, ident),
        ).fetchone()
        return dict(row) if row else {"state": "available", "reason": None, "expired": None}

    @contextmanager
    def lease(self, principal, ident, *, exclusive=False):
        from pydantic import TypeAdapter
        from .observation_contracts import Reference

        TypeAdapter(Reference).validate_python(ident)
        with (self.leases / (ident + ".lock")).open("a") as handle:
            fcntl.flock(handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            if not exclusive:
                with self.store.connect() as db:
                    if self.availability(db, principal, ident)["state"] != "available":
                        raise RawRecordingExpired(
                            "raw recording expired; original media is no longer available"
                        )
            yield

    def protect(self, principal, ident, value=True):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM raw_recordings WHERE principal=? AND media_ref=?",
                (principal, ident),
            ).fetchone()
            if row is None or row["state"] != "available":
                raise Conflict("only available managed recordings can be protected")
            db.execute(
                "UPDATE raw_recordings SET protected=? WHERE media_ref=?", (int(value), ident)
            )

    @staticmethod
    def _analysis(db, ident, body, *, include_protection=True):
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "observation_media" not in tables:
            return False, False
        protected = False
        if include_protection and "observation_decisions" in tables:
            protected = bool(
                db.execute(
                    """SELECT 1 FROM observation_decisions d JOIN observation_media m
                ON m.task_id=d.task_id AND m.entry_id=d.batch_id
                LEFT JOIN observation_candidate_reviews r ON r.marker_id=d.id AND r.revision=d.revision
                WHERE (json_extract(m.body,'$.media_ref')=? OR EXISTS (SELECT 1 FROM json_each(m.body,'$.media_parts') WHERE value=?)) AND d.decision IN ('keep','uncertain')
                AND COALESCE(r.disposition,'retain')!='release' LIMIT 1""",
                    (ident, ident),
                ).fetchone()
            )
        if include_protection and not protected and "observation_retention_events" in tables:
            from .managed_observation_source import segment_ref

            segment = segment_ref(body["source_ref"], body["segment_id"])
            protected = bool(
                db.execute(
                    """SELECT 1 FROM observation_retention_events
                WHERE source_ref=? AND (segment_id=? OR substr(segment_id,1,?)=?)
                AND start_ms<? AND end_ms>? LIMIT 1""",
                    (
                        body["source_ref"],
                        segment,
                        len(segment + "-restart-"),
                        segment + "-restart-",
                        body["end_ms"],
                        body["start_ms"],
                    ),
                ).fetchone()
            )
        indexed = "observation_media_refs" in tables
        ranges = [
            tuple(r)
            for r in db.execute(
                """SELECT m.start_ms,m.end_ms FROM observation_media_refs r
                JOIN observation_media m ON m.task_id=r.task_id AND m.entry_id=r.entry_id
                WHERE r.media_ref=? AND m.seen IS NOT NULL ORDER BY m.start_ms"""
                if indexed else
                "SELECT start_ms,end_ms FROM observation_media WHERE (json_extract(body,'$.media_ref')=? OR EXISTS (SELECT 1 FROM json_each(body,'$.media_parts') WHERE value=?)) AND seen IS NOT NULL ORDER BY start_ms",
                (ident,) if indexed else (ident, ident),
            )
        ]
        from .observation_decisions import _covered

        return protected, _covered(ranges, body["start_ms"], body["end_ms"])

    def _finish_deletion(self, db, row):
        path = Path(row["owned_path"])
        body = json.loads(row["body"])
        if path.parent != self.media.root / "raw" or path.resolve() != path:
            raise Conflict("refusing deletion outside owned raw directory")
        if path.exists():
            if path.is_symlink() or tuple(
                body.get("file_fingerprint", body["source_fingerprint"])
            ) != _fingerprint(path):
                raise Conflict("owned recording changed before deletion")
            path.unlink()
        db.execute(
            "UPDATE raw_recordings SET state='expired',expired=? WHERE media_ref=?",
            (self.store.clock(), row["media_ref"]),
        )
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='observation_media'").fetchone():
            indexed = db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='observation_media_refs'"
            ).fetchone()
            selection = (
                "(task_id,entry_id) IN (SELECT task_id,entry_id FROM observation_media_refs WHERE media_ref=?)"
                if indexed else
                "(json_extract(body,'$.media_ref')=? OR EXISTS (SELECT 1 FROM json_each(body,'$.media_parts') WHERE value=?))"
            )
            params = (row["media_ref"],) if indexed else (row["media_ref"], row["media_ref"])
            db.execute(
                f"UPDATE observation_media SET gap_reason=? WHERE {selection} AND seen IS NULL",
                (row["reason"], *params),
            )
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='observation_backfill_retries'"
            ).fetchone():
                db.execute(
                    f"DELETE FROM observation_backfill_retries WHERE (task_id,batch_id) IN (SELECT task_id,entry_id FROM observation_media WHERE {selection})",
                    params,
                )
        self.store._audit(
            db,
            None,
            "raw.recording.expired",
            {"media_ref": row["media_ref"], "reason": row["reason"], "bytes": row["bytes"]},
        )

    def collect(self, principal, *, reserve_bytes=0):
        if type(reserve_bytes) is not int or reserve_bytes < 0:
            raise ValueError("invalid write reservation")
        shared = getattr(self.store, "shared_media_storage", None)
        if shared:
            return shared.collect_raw(self.media, principal, reserve_bytes)
        with self.lock:
            configuration = self.configuration(principal)
            policy = RawRetentionPolicy.model_validate(configuration["policy"])
            from .storage_review import record_pressure

            removed = []
            # Complete committed deletion intents before accepting another writer.
            with self.store.connect() as db:
                pending = db.execute(
                    "SELECT r.*,e.body,e.bytes FROM raw_recordings r JOIN managed_input_entries e ON e.id=r.media_ref WHERE r.principal=? AND state='deleting'",
                    (principal,),
                ).fetchall()
            for row in pending:
                with (
                    self.lease(principal, row["media_ref"], exclusive=True),
                    self.store.connect() as db,
                ):
                    db.execute("BEGIN IMMEDIATE")
                    self._finish_deletion(db, row)
                    removed.append(row["media_ref"])
            if not policy.enabled:
                record_pressure(self.health, principal, policy, 0, 0, configuration["revision"])
                self.last_usage = {
                    "enabled": False,
                    "expired": removed,
                    "writable": True,
                    "policy_revision": configuration["revision"],
                    "measured_at": self.store.clock(),
                }
                return self.last_usage
            with self.store.connect() as db:
                rows = db.execute(
                    "SELECT r.*,e.body,e.bytes FROM raw_recordings r JOIN managed_input_entries e ON e.id=r.media_ref WHERE r.principal=? AND state='available' ORDER BY COALESCE(recorded,ingested),ingested,media_ref",
                    (principal,),
                ).fetchall()
            managed_bytes = sum(r["bytes"] for r in rows)
            managed_inodes = set()
            for row in rows:
                try:
                    stat = Path(row["owned_path"]).stat()
                    managed_inodes.add((stat.st_dev, stat.st_ino))
                    if json.loads(row["body"]).get("recording_state") == "growing":
                        managed_bytes += max(0, stat.st_size - row["bytes"])
                except FileNotFoundError:
                    pass
            staging_bytes = 0
            staged_inodes = set()
            for path in (self.store.root / "input-capture").glob("*/continuous-*/part-*.mkv"):
                try:
                    stat = path.stat()
                    staged_inodes.add((stat.st_dev, stat.st_ino))
                    if (stat.st_dev, stat.st_ino) not in managed_inodes:
                        staging_bytes += stat.st_size
                except FileNotFoundError:
                    pass  # A committed segment may have just left staging.
            unindexed_bytes = unindexed_files = 0
            with self.store.connect() as db:
                indexed_paths = {
                    r[0]
                    for r in db.execute(
                        "SELECT path FROM managed_input_entries WHERE path IS NOT NULL"
                    )
                }
            counted = managed_inodes | staged_inodes
            for path in (self.media.root / "raw").glob("*"):
                if path.is_symlink() or not path.is_file() or str(path) in indexed_paths:
                    continue
                stat = path.stat()
                inode = (stat.st_dev, stat.st_ino)
                if inode not in counted:
                    unindexed_bytes += stat.st_size
                    unindexed_files += 1
                    counted.add(inode)
            used = managed_bytes + staging_bytes + unindexed_bytes
            simulated = bool(rows) and all(
                json.loads(r["body"]).get("simulated", False) for r in rows
            )
            record_pressure(
                self.health,
                principal,
                policy,
                used,
                reserve_bytes,
                configuration["revision"],
                simulated,
            )
            capacity = (
                policy.quota_bytes is not None
                and used + reserve_bytes >= policy.quota_bytes * policy.high_water
            )
            target = policy.quota_bytes * policy.low_water if capacity else None
            for row in rows:
                if json.loads(row["body"]).get("recording_state") == "growing":
                    continue
                now = self.store.clock()
                old = (
                    policy.max_age_seconds is not None
                    and row["recorded"] is not None
                    and 0 <= now - row["recorded"]
                    and now - row["recorded"] >= policy.max_age_seconds
                )
                pressure = target is not None and used + reserve_bytes > target
                pressure = (
                    pressure
                    or shutil.disk_usage(self.media.root).free - reserve_bytes
                    < policy.min_free_bytes
                )
                if not (old or pressure):
                    continue
                try:
                    with self.lease(principal, row["media_ref"], exclusive=True):
                        with self.store.connect() as db:
                            db.execute("BEGIN IMMEDIATE")
                            current = db.execute(
                                "SELECT protected,state FROM raw_recordings WHERE media_ref=?",
                                (row["media_ref"],),
                            ).fetchone()
                            aliased = db.execute(
                                "SELECT 1 FROM managed_input_entries WHERE path=? AND id!=? LIMIT 1",
                                (row["owned_path"], row["media_ref"]),
                            ).fetchone()
                            if aliased:
                                continue
                            protected, analysed = self._analysis(
                                db, row["media_ref"], json.loads(row["body"])
                            )
                            if (
                                current["state"] != "available"
                                or current["protected"]
                                or protected
                                or (not analysed and not policy.allow_unanalysed_expiry)
                            ):
                                continue
                            reason = "retention_expired" if analysed else "unanalysed_expired"
                            db.execute(
                                "UPDATE raw_recordings SET state='deleting',reason=? WHERE media_ref=?",
                                (reason, row["media_ref"]),
                            )
                        # Persist intent first; a crash must not leave a playable index for missing bytes.
                        with self.store.connect() as db:
                            db.execute("BEGIN IMMEDIATE")
                            self._finish_deletion(db, {**dict(row), "reason": reason})
                        used -= row["bytes"]
                        removed.append(row["media_ref"])
                except BlockingIOError:
                    continue  # An actual reader or inference still owns this file.
            writable = (
                policy.quota_bytes is None or used + reserve_bytes <= policy.quota_bytes
            ) and shutil.disk_usage(self.media.root).free - reserve_bytes >= policy.min_free_bytes
            self.last_usage = {
                "enabled": True,
                "expired": removed,
                "used_bytes": used,
                "reserved_bytes": reserve_bytes,
                "staging_bytes": staging_bytes,
                "unindexed_bytes": unindexed_bytes,
                "unindexed_files": unindexed_files,
                "writable": writable,
                "blocked_reason": None if writable else "protected_or_busy_storage_full",
                "policy_revision": configuration["revision"],
                "measured_at": self.store.clock(),
            }
            record_pressure(
                self.health,
                principal,
                policy,
                used,
                reserve_bytes,
                configuration["revision"],
                simulated,
            )
            return self.last_usage


def lease_observation(function):
    """Keep raw bytes until the consuming coroutine (including cancellation) ends."""

    @wraps(function)
    async def guarded(self, batch, *args, **kwargs):
        media = getattr(getattr(self.source, "preparer", None), "media", None)
        with ExitStack() as leases:
            if media:
                for ref in sorted(set(getattr(batch, "media_parts", ()) or (batch.media_ref,))):
                    leases.enter_context(media.retention.lease(batch.principal, ref))
            return await function(self, batch, *args, **kwargs)

    return guarded
