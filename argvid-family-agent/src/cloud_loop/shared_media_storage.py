"""One on-disk budget for multiple logical node stores on the same filesystem.

Only explicitly enrolled roots are counted. Deletion still uses each recording's
original owner, inode fingerprint, reader lock and durable expiry receipt.
"""
from contextlib import contextmanager, nullcontext
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import time
import uuid

from pydantic import Field, model_validator
from .contracts import Contract
from .store import Conflict


class SharedStorageConfig(Contract):
    root: str
    roots: list[str] = Field(min_length=1)
    raw_stores: list[str] = Field(default_factory=list)
    derived_stores: list[str] = Field(default_factory=list)
    total_bytes: int = Field(default=120_000_000_000, gt=0)
    min_free_bytes: int = Field(default=20_000_000_000, ge=0)
    raw_bytes: int = Field(default=70_000_000_000, gt=0)
    review_bytes: int = Field(default=67_000_000_000, gt=0)
    collect_bytes: int = Field(default=68_500_000_000, gt=0)
    target_bytes: int = Field(default=59_500_000_000, gt=0)
    candidate_bytes: int = Field(default=20_000_000_000, gt=0)
    final_bytes: int = Field(default=20_000_000_000, gt=0)
    review_grace_seconds: int = Field(default=300, ge=0)
    review_task_id: str | None = None

    @model_validator(mode="after")
    def bounds(self):
        if not self.target_bytes < self.review_bytes < self.collect_bytes <= self.raw_bytes:
            raise ValueError("raw target < review < collection <= maximum required")
        if self.raw_bytes + self.candidate_bytes + self.final_bytes >= self.total_bytes:
            raise ValueError("shared work needs space outside persistent libraries")
        paths = [Path(p) for p in self.roots]
        if any(not p.is_absolute() or p.resolve() != p for p in paths):
            raise ValueError("shared roots must be canonical absolute paths")
        if any(a == b or a.is_relative_to(b) or b.is_relative_to(a)
               for i, a in enumerate(paths) for b in paths[i + 1:]):
            raise ValueError("shared roots must not overlap")
        if any(not any(Path(p).is_relative_to(r) for r in paths)
               for p in [*self.raw_stores, *self.derived_stores]):
            raise ValueError("raw owners must be inside the accounted roots")
        return self


class SharedMediaStorage:
    def __init__(self, config):
        self.config = SharedStorageConfig.model_validate(config)
        self.root = Path(self.config.root)
        if not self.root.is_absolute() or self.root.resolve() != self.root:
            raise ValueError("shared state directory must be canonical")
        self.root.mkdir(parents=True, exist_ok=True)
        self.database = self.root / "storage.sqlite3"
        self.owners = {}
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reservations(id TEXT PRIMARY KEY, bytes INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS candidates(
                    path TEXT PRIMARY KEY, principal TEXT NOT NULL, media_ref TEXT NOT NULL,
                    owner_root TEXT NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS review_state(
                    principal TEXT PRIMARY KEY, started REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS final_deletions(
                    path TEXT PRIMARY KEY, owner_root TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, created REAL NOT NULL);
            """)

    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def lock(self, name, *, nonblocking=False):
        with (self.root / (name + ".lock")).open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def snapshot(self):
        with self.connect() as db:
            candidates = {r[0] for r in db.execute("SELECT path FROM candidates")}
            pending = db.execute("SELECT COALESCE(SUM(bytes),0) FROM reservations").fetchone()[0]
        candidate_inodes = set()
        for p in candidates:
            try:
                info = Path(p).stat()
                candidate_inodes.add((info.st_dev, info.st_ino))
            except FileNotFoundError:
                pass
        seen, counts = set(), dict(raw=0, candidates=0, finals=0, work_and_records=0)
        for root in self.config.roots:
            for folder, dirs, files in os.walk(root, followlinks=False):
                dirs[:] = [d for d in dirs if not Path(folder, d).is_symlink()]
                for name in files:
                    path = Path(folder, name)
                    try:
                        info = path.lstat()
                    except FileNotFoundError:
                        continue
                    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) in seen:
                        continue
                    seen.add((info.st_dev, info.st_ino))
                    # Small-file allocation and a not-yet-allocated growing file both count.
                    size = max(info.st_size, info.st_blocks * 512)
                    kind = ("candidates" if (info.st_dev, info.st_ino) in candidate_inodes else
                            "raw" if "input-media" in path.parts and "raw" in path.parts else
                            "finals" if {"assets", "edge-renders"} & set(path.parts) else "work_and_records")
                    counts[kind] += size
        return {**counts, "used_bytes": sum(counts.values()), "reserved_bytes": pending,
                "free_bytes": shutil.disk_usage(self.root).free}

    def _reap_reservations(self):
        with self.connect() as db:
            for row in db.execute("SELECT id FROM reservations").fetchall():
                try:
                    with self.lock("writer-" + row["id"], nonblocking=True):
                        db.execute("DELETE FROM reservations WHERE id=?", (row["id"],))
                except BlockingIOError:
                    pass

    def writable(self, amount=0):
        value = self.snapshot()
        return (value["used_bytes"] + value["reserved_bytes"] + amount <= self.config.total_bytes
                and value["free_bytes"] - value["reserved_bytes"] - amount >= self.config.min_free_bytes)

    @contextmanager
    def reserve(self, amount):
        if type(amount) is not int or amount < 0:
            raise ValueError("invalid shared write reservation")
        ident = uuid.uuid4().hex
        with self.lock("writer-" + ident):
            with self.lock("admission"):
                self._reap_reservations()
                if not self.writable(amount):
                    raise Conflict("shared storage total or filesystem safety floor reached")
                with self.connect() as db:
                    db.execute("INSERT INTO reservations VALUES (?,?)", (ident, amount))
            try:
                yield
            finally:
                with self.connect() as db:
                    db.execute("DELETE FROM reservations WHERE id=?", (ident,))

    def promote(self, media, principal, refs, reason):
        """A candidate library is a durable ownership tag, not a second byte copy."""
        from contextlib import ExitStack
        with ExitStack() as leases, self.lock("admission"):
            items = []
            for ref in sorted(set(refs)):
                leases.enter_context(media.retention.lease(principal, ref))
                raw = media.resolve(principal, ref)
                path = Path(raw["path"])
                if not any(path.is_relative_to(Path(r)) for r in self.config.roots):
                    raise Conflict("candidate is outside shared storage")
                items.append((ref, path))
            value = self.snapshot()
            with self.connect() as db:
                existing = {r[0] for r in db.execute("SELECT path FROM candidates")}
                added = sum(max(p.stat().st_size, p.stat().st_blocks * 512)
                            for _, p in items if str(p) not in existing)
                if value["candidates"] + added > self.config.candidate_bytes:
                    raise Conflict("candidate library full; review existing candidates first")
                for ref, path in items:
                    db.execute("INSERT OR IGNORE INTO candidates VALUES (?,?,?,?,?,?)",
                               (str(path), principal, ref, str(media.store.root), reason, time.time()))
            return {"retained": [r for r, _ in items], "copied_bytes": 0,
                    "library": "candidates", "added_bytes": added}

    def release_candidates(self, media, principal, refs):
        with self.lock("admission"), self.connect() as db:
            for ref in refs:
                db.execute("DELETE FROM candidates WHERE owner_root=? AND principal=? AND media_ref=?",
                           (str(media.store.root), principal, ref))

    def _raw_owners(self, current):
        from .harness_state import HarnessStore
        from .harness_input_store import InputMediaStore
        for root in self.config.raw_stores:
            if Path(root) == current.store.root:
                yield current
            else:
                if root not in self.owners:
                    path = Path(root)
                    if not (path / "edge.sqlite3").is_file():
                        raise Conflict("enrolled raw owner database missing")
                    self.owners[root] = InputMediaStore(HarnessStore(path, "edge"))
                yield self.owners[root]

    def collect_raw(self, current, principal, reserve_bytes=0):
        from .raw_retention import RawRetentionPolicy
        from .storage_review import record_pressure
        c = self.config
        removed = []
        with self.lock("raw-collector"):
            # Replay committed intent even if a restart happened below the waterline.
            for owner in self._raw_owners(current):
                with owner.store.connect() as db:
                    unfinished = db.execute(
                        "SELECT r.*,e.body,e.bytes FROM raw_recordings r JOIN managed_input_entries e "
                        "ON e.id=r.media_ref WHERE r.principal=? AND r.state='deleting'", (principal,)
                    ).fetchall()
                for row in unfinished:
                    try:
                        with owner.retention.lease(principal, row["media_ref"], exclusive=True):
                            with owner.store.connect() as db:
                                owner.retention._finish_deletion(db, dict(row))
                            with self.connect() as db:
                                db.execute("DELETE FROM candidates WHERE path=?", (row["owned_path"],))
                            removed.append(row["media_ref"])
                    except BlockingIOError:
                        continue
            value = self.snapshot()
            projected = value["raw"] + reserve_bytes
            policy = RawRetentionPolicy(
                enabled=True, quota_bytes=c.raw_bytes, low_water=c.target_bytes/c.raw_bytes,
                review_water=c.review_bytes/c.raw_bytes, high_water=c.collect_bytes/c.raw_bytes,
                review_task_id=c.review_task_id, min_free_bytes=c.min_free_bytes,
            )
            record_pressure(current.retention.health, principal, policy, value["raw"],
                            reserve_bytes, 1, False)
            with self.connect() as db:
                if projected >= c.review_bytes:
                    db.execute("INSERT OR IGNORE INTO review_state VALUES (?,?)",
                               (principal, time.time()))
                elif projected <= c.target_bytes:
                    db.execute("DELETE FROM review_state WHERE principal=?", (principal,))
                review = db.execute("SELECT started FROM review_state WHERE principal=?", (principal,)).fetchone()
                candidates = {r[0] for r in db.execute("SELECT path FROM candidates")}
            urgent = not self.writable(reserve_bytes) or projected >= c.raw_bytes
            grace = bool(review and time.time() - review[0] < c.review_grace_seconds)
            pressure = projected >= c.collect_bytes or urgent
            if pressure and (not grace or urgent):
                rows = []
                for owner in self._raw_owners(current):
                    with owner.store.connect() as db:
                        for row in db.execute(
                            "SELECT r.*,e.body,e.bytes FROM raw_recordings r JOIN managed_input_entries e "
                            "ON e.id=r.media_ref WHERE r.principal=? AND r.state IN ('available','deleting')",
                            (principal,),
                        ):
                            rows.append((owner, dict(row)))
                rows.sort(key=lambda pair: (pair[1]["recorded"] or pair[1]["ingested"],
                                             pair[1]["ingested"], pair[1]["media_ref"]))
                for owner, row in rows:
                    if projected <= c.target_bytes and self.writable(reserve_bytes):
                        break
                    if (row["protected"] or row["owned_path"] in candidates
                            or json.loads(row["body"]).get("recording_state") == "growing"):
                        continue
                    try:
                        with owner.retention.lease(principal, row["media_ref"], exclusive=True):
                            # A promotion may have committed after the initial snapshot.
                            with self.connect() as library:
                                if library.execute("SELECT 1 FROM candidates WHERE path=?",
                                                   (row["owned_path"],)).fetchone():
                                    continue
                            with owner.store.connect() as db:
                                # The lease fences actual readers; expensive history reads
                                # must not hold the node's writer slot.
                                _, analysed = owner.retention._analysis(
                                    db, row["media_ref"], json.loads(row["body"]),
                                    include_protection=False,
                                )
                            with owner.store.connect() as db:
                                db.execute("BEGIN IMMEDIATE")
                                fresh = db.execute("SELECT protected,state FROM raw_recordings WHERE media_ref=?",
                                                   (row["media_ref"],)).fetchone()
                                if not fresh or fresh[0] or fresh[1] not in {"available", "deleting"}:
                                    continue
                                if db.execute("SELECT 1 FROM managed_input_entries WHERE path=? AND id!=?",
                                              (row["owned_path"], row["media_ref"])).fetchone():
                                    continue
                                row["reason"] = "shared_fifo_expiry" if analysed else "unanalysed_expired"
                                db.execute("UPDATE raw_recordings SET state='deleting',reason=? WHERE media_ref=?",
                                           (row["reason"], row["media_ref"]))
                            with owner.store.connect() as db:
                                owner.retention._finish_deletion(db, row)
                            removed.append(row["media_ref"])
                            projected -= row["bytes"]
                    except BlockingIOError:
                        continue
            value = self.snapshot()
            writable = value["raw"] + reserve_bytes <= c.raw_bytes and self.writable(reserve_bytes)
            current.retention.last_usage = {
                "enabled": True, "shared": True, "expired": removed, "used_bytes": value["raw"],
                "reserved_bytes": reserve_bytes, "writable": writable,
                "blocked_reason": None if writable else "shared_capacity_or_protected_files",
                "review_waiting": grace, "measured_at": time.time(), "shared_usage": value,
            }
            return current.retention.last_usage

    def _finish_final_deletion(self, storage, intent):
        category = Path(intent["path"]).relative_to(storage.store.root).parts[0]
        if category not in {"assets", "edge-renders"}:
            raise Conflict("final deletion is outside either final-library location")
        path = storage.owned_path(intent["path"], category)
        try:
            with storage.pin(path, exclusive=True), storage.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                fresh = db.execute("SELECT * FROM managed_media_files WHERE path=?", (str(path),)).fetchone()
                if (not fresh or storage._busy(db, fresh)
                        or fresh["fingerprint"] != intent["fingerprint"]
                        or (path.exists() and storage.fingerprint(path) != intent["fingerprint"])):
                    return False
                db.execute("UPDATE managed_media_files SET status='expired',expired=? WHERE path=?",
                           (time.time(), str(path)))
                db.commit()
                path.unlink(missing_ok=True)
                with self.connect() as journal:
                    journal.execute("DELETE FROM final_deletions WHERE path=?", (str(path),))
                return True
        except BlockingIOError:
            return False

    def collect_libraries(self, current, principal):
        """Library eviction is local policy, not a model call or a retention promise."""
        from types import SimpleNamespace
        from .harness_state import HarnessStore
        from .media_storage import MediaStorage

        removed = []
        with self.lock("library-collector"):
            with self.connect() as db:
                unfinished = [dict(r) for r in db.execute("SELECT * FROM final_deletions")]
            for intent in unfinished:
                if intent["owner_root"] not in self.config.derived_stores:
                    raise Conflict("unfinished deletion has no enrolled owner")
                path = Path(intent["owner_root"])
                storage = MediaStorage(SimpleNamespace(store=HarnessStore(path, path.name)))
                if self._finish_final_deletion(storage, intent):
                    removed.append(intent["path"])
            value = self.snapshot()
            if value["candidates"] >= self.config.candidate_bytes * .9:
                owners = {str(m.store.root): m for m in self._raw_owners(current)}
                with self.connect() as db:
                    candidates = [dict(r) for r in db.execute(
                        "SELECT * FROM candidates WHERE principal=? ORDER BY created,path", (principal,))]
                used = value["candidates"]
                for candidate in candidates:
                    if used <= self.config.candidate_bytes * .8:
                        break
                    media = owners.get(candidate["owner_root"])
                    if media is None:
                        continue
                    try:
                        with media.retention.lease(principal, candidate["media_ref"], exclusive=True):
                            with media.store.connect() as db:
                                db.execute("BEGIN IMMEDIATE")
                                row = db.execute(
                                    "SELECT r.*,e.body,e.bytes FROM raw_recordings r JOIN managed_input_entries e "
                                    "ON e.id=r.media_ref WHERE r.media_ref=? AND r.principal=?",
                                    (candidate["media_ref"], principal),
                                ).fetchone()
                                if (not row or row["protected"] or row["state"] != "available"
                                        or json.loads(row["body"]).get("recording_state") == "growing"):
                                    continue
                                db.execute("UPDATE raw_recordings SET state='deleting',reason=? WHERE media_ref=?",
                                           ("candidate_library_expiry", row["media_ref"]))
                            with media.store.connect() as db:
                                media.retention._finish_deletion(db, {**dict(row), "reason":"candidate_library_expiry"})
                            with self.connect() as db:
                                db.execute("DELETE FROM candidates WHERE path=?", (candidate["path"],))
                            used -= row["bytes"]
                            removed.append(candidate["path"])
                    except BlockingIOError:
                        continue
            if value["finals"] >= self.config.final_bytes * .9:
                files = []
                for root in self.config.derived_stores:
                    path = Path(root)
                    side = path.name
                    if side not in {"cloud", "edge"} or not (path / (side + ".sqlite3")).is_file():
                        raise Conflict("derived media owner missing")
                    store = HarnessStore(path, side)
                    storage = MediaStorage(SimpleNamespace(store=store))
                    with store.connect() as db:
                        for row in db.execute("SELECT * FROM managed_media_files "
                                              "WHERE category IN ('assets','edge-renders') AND status='available'"):
                            files.append((storage, dict(row)))
                files.sort(key=lambda pair: (pair[1]["touched"], pair[1]["path"]))
                used = value["finals"]
                for storage, row in files:
                    if used <= self.config.final_bytes * .8:
                        break
                    path = storage.owned_path(row["path"], row["category"])
                    try:
                        with storage.pin(path, exclusive=True), storage.store.connect() as db:
                            db.execute("BEGIN IMMEDIATE")
                            fresh = db.execute("SELECT * FROM managed_media_files WHERE path=?",
                                               (str(path),)).fetchone()
                            if (fresh["status"] != "available" or storage._busy(db, fresh)
                                    or not path.is_file() or storage.fingerprint(path) != fresh["fingerprint"]):
                                continue
                            # Persist cross-store intent before changing the owner index.
                            with self.connect() as journal:
                                journal.execute("INSERT OR REPLACE INTO final_deletions VALUES (?,?,?,?)",
                                                (str(path), str(storage.store.root),
                                                 fresh["fingerprint"], time.time()))
                        if self._finish_final_deletion(storage, {
                            "path": str(path), "fingerprint": row["fingerprint"]
                        }):
                            used -= row["size"]
                            removed.append(str(path))
                    except BlockingIOError:
                        continue
            return {"removed": removed, "snapshot": self.snapshot()}


def install_shared_storage(store):
    path = store.meta("shared_media_storage_file", None)
    store.shared_media_storage = SharedMediaStorage(json.loads(Path(path).read_text())) if path else None
    return store.shared_media_storage
