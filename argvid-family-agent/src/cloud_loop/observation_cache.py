"""Bound analysis writes before preprocessing; never delete registered originals.

Reservations include the native adapter's later split-WAV files. Deletion and
releasing reservations require the separate reference-aware cleanup worker.
"""

import asyncio
import os
import re
import shutil
from pathlib import Path

from .contracts import ID
from .observation_replay import FileReplaySource
from .store import Conflict


def pending_disk_bytes(db, exclude_export=None):
    total = 0
    if db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='observation_cache_reservations'"
    ).fetchone():
        total += db.execute(
            "SELECT COALESCE(SUM(reserved_bytes-materialized_bytes),0) FROM observation_cache_reservations"
        ).fetchone()[0]
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='observation_exports'").fetchone():
        total += db.execute(
            "SELECT COALESCE(SUM(reserved_bytes),0) FROM observation_exports WHERE status!='saved' AND id!=?",
            (exclude_export or "",),
        ).fetchone()[0]
    return total


def free_space_floor(db, minimum=0):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='observation_storage'").fetchone():
        return minimum
    return max(
        minimum,
        db.execute("SELECT COALESCE(MAX(min_free_bytes),0) FROM observation_storage").fetchone()[0],
    )


class AnalysisBudget:
    def __init__(self, store, root, quota_bytes, min_free_bytes):
        self.store, self.root = store, Path(root).resolve()
        self.quota, self.min_free = quota_bytes, min_free_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS observation_cache_reservations (
                path TEXT PRIMARY KEY,reserved_bytes INTEGER NOT NULL,materialized_bytes INTEGER NOT NULL DEFAULT 0)""")
            if "materialized_bytes" not in {
                row[1] for row in db.execute("PRAGMA table_info(observation_cache_reservations)")
            }:
                db.execute(
                    "ALTER TABLE observation_cache_reservations ADD COLUMN materialized_bytes INTEGER NOT NULL DEFAULT 0"
                )
            for row in db.execute(
                "SELECT path,reserved_bytes FROM observation_cache_reservations"
            ).fetchall():
                path = Path(row["path"])
                if not path.resolve().is_relative_to(self.root):
                    raise Conflict("cache reservation escaped root")
                actual = self._bytes(path)
                if actual > row["reserved_bytes"]:
                    raise Conflict("existing cache exceeds its reservation")
                db.execute(
                    "UPDATE observation_cache_reservations SET materialized_bytes=? WHERE path=?",
                    (actual, row["path"]),
                )
            reserved = db.execute(
                "SELECT COALESCE(SUM(reserved_bytes),0) FROM observation_cache_reservations"
            ).fetchone()[0]
        actual = sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())
        if actual > reserved:
            raise Conflict(
                "analysis cache contains unaccounted files; use an isolated state directory"
            )
        if reserved > quota_bytes:
            raise Conflict("existing analysis reservations exceed configured quota")

    @staticmethod
    def _bytes(path):
        total = 0
        for file in path.rglob("*") if path.exists() else []:
            if file.is_symlink():
                raise Conflict("analysis cache must not contain symlinks")
            if file.is_file():
                total += file.stat().st_size
        return total

    def _pending(self, db):
        return pending_disk_bytes(db)

    def reserve(self, path, amount):
        if type(amount) is not int or amount <= 0:
            raise ValueError("invalid analysis reservation")
        path = Path(path).resolve()
        if not path.is_relative_to(self.root):
            raise Conflict("analysis reservation escaped cache")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT reserved_bytes FROM observation_cache_reservations WHERE path=?",
                (str(path),),
            ).fetchone()
            if old:
                raise Conflict("analysis batch already has a reservation")
            used = db.execute(
                "SELECT COALESCE(SUM(reserved_bytes),0) FROM observation_cache_reservations"
            ).fetchone()[0]
            if used + amount > self.quota:
                raise Conflict("analysis cache quota exhausted")
            if (
                shutil.disk_usage(self.root).free
                < free_space_floor(db, self.min_free) + self._pending(db) + amount
            ):
                raise Conflict("analysis free-space floor reached")
            db.execute(
                "INSERT INTO observation_cache_reservations(path,reserved_bytes) VALUES (?,?)",
                (str(path), amount),
            )
        return amount

    def settle(self, path, future_audio_bytes):
        path = Path(path).resolve()
        if not path.is_relative_to(self.root):
            raise Conflict("analysis settlement escaped cache")
        actual = self._bytes(path)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT reserved_bytes FROM observation_cache_reservations WHERE path=?",
                (str(path),),
            ).fetchone()
            if row is None or actual + future_audio_bytes > row[0]:
                raise Conflict("analysis exceeded its reservation")
            if not path.exists():
                db.execute("DELETE FROM observation_cache_reservations WHERE path=?", (str(path),))
            else:
                db.execute(
                    "UPDATE observation_cache_reservations SET reserved_bytes=?,materialized_bytes=? WHERE path=?",
                    (actual + future_audio_bytes, actual, str(path)),
                )

    def write_native(self, directory, path, data):
        directory = Path(directory).resolve()
        path = Path(path)
        if (
            not directory.is_relative_to(self.root)
            or path.parent.resolve() != directory / "native-audio"
            or path.is_symlink()
        ):
            raise Conflict("native audio write escaped analysis reservation")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT reserved_bytes FROM observation_cache_reservations WHERE path=?",
                (str(directory),),
            ).fetchone()
            actual = self._bytes(directory)
            if row is None or actual > row[0]:
                raise Conflict("native audio has no valid reservation")
            db.execute(
                "UPDATE observation_cache_reservations SET materialized_bytes=? WHERE path=?",
                (actual, str(directory)),
            )
            if path.exists():
                if path.read_bytes() != data:
                    raise Conflict("native audio bytes changed")
                return
            if actual + len(data) > row[0]:
                raise Conflict("native audio exceeds analysis reservation")
            if shutil.disk_usage(self.root).free < free_space_floor(
                db, self.min_free
            ) + self._pending(db):
                raise Conflict("analysis free-space floor reached")
            path.parent.mkdir(exist_ok=True)
            with path.open("xb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            db.execute(
                "UPDATE observation_cache_reservations SET materialized_bytes=? WHERE path=?",
                (actual + len(data), str(directory)),
            )


class BudgetedReplaySource(FileReplaySource):
    def __init__(self, *args, budget, **kwargs):
        super().__init__(*args, **kwargs)
        self.budget = budget

    def discard_prepared(self, attempt, batch):
        super().discard_prepared(attempt, batch)
        path = self.cache_root / attempt / batch.batch_id
        with self.budget.store.connect() as db:
            reserved = db.execute(
                "SELECT 1 FROM observation_cache_reservations WHERE path=?", (str(path.resolve()),)
            ).fetchone()
        if reserved:
            self.budget.settle(path, 0)

    async def _prepare_batch(self, batch, *, attempt_id, check_active=lambda: None):
        if self.memory_images:
            return await super()._prepare_batch(
                batch, attempt_id=attempt_id, check_active=check_active
            )
        if not re.fullmatch(ID, attempt_id):
            raise ValueError("invalid analysis attempt identity")
        duration = batch.end_ms - batch.start_ms
        frames = (duration + self.sampling.frame_interval_ms - 1) // self.sampling.frame_interval_ms
        # Pillow creates new JPEGs without original metadata; this reserves well
        # above RGB storage plus headers, two PCM copies and bounded manifests.
        native_audio = duration * 32 + frames * 64 if self.sampling.include_audio else 0
        bound = (
            frames * (self.sampling.image_long_edge**2 * 8 + 65536)
            + (duration * 64 if self.sampling.include_audio else 0)
            + 256 * 1024
        )
        path = self.cache_root / attempt_id / batch.batch_id
        async with self.budget.lock:
            self.budget.reserve(path, bound)
            try:
                result = await super()._prepare_batch(
                    batch, attempt_id=attempt_id, check_active=check_active
                )
            except BaseException:
                # Base source awaits its encoding thread on cancellation. Any
                # remaining files keep a reservation until explicit cleanup.
                self.budget.settle(path, native_audio if path.exists() else 0)
                raise
            self.budget.settle(path, native_audio)
            return result
