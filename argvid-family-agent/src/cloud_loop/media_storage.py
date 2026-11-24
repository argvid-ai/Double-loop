"""Quota and lifecycle for registered derived media in this node's state directory."""

import fcntl
import asyncio
import hashlib
import json
import shutil
import tempfile
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Literal

from pydantic import Field

from .contracts import Contract, canonical
from .store import Conflict


Category = Literal[
    "observation-inputs",
    "requested-edit-inputs",
    "assets",
    "edge-renders",
    "render-projects",
    "voice-audio",
]


@contextmanager
def render_workspace(parent):
    """An owned scratch directory whose lock also survives normal thread cancellation."""
    with tempfile.TemporaryDirectory(prefix="render-", dir=parent) as directory:
        path = Path(directory)
        with (path / ".muse-render-lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            (path / ".muse-render-owned").write_text("muse-render-work/1\n")
            yield path


def reap_render_work(folder):
    for path in folder.glob("render-*"):
        if not path.is_dir() or path.resolve() != path or path.parent != folder:
            continue
        marker, lease = path / ".muse-render-owned", path / ".muse-render-lock"
        if not marker.is_file() or marker.is_symlink() or lease.is_symlink():
            continue
        if marker.read_text() != "muse-render-work/1\n" or not lease.is_file():
            continue
        try:
            with lease.open("r+") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                shutil.rmtree(path)
        except BlockingIOError:
            continue


class MediaPoolPolicy(Contract):
    quota_bytes: int | None = Field(default=2_000_000_000, gt=0)
    high_water: float = Field(default=0.95, gt=0, le=1)
    low_water: float | None = Field(default=None, gt=0, lt=1)
    delete_completed: bool = False
    max_age_seconds: int | None = Field(default=None, gt=0)
    automatic_delete: bool = True


class MediaStoragePolicy(Contract):
    min_free_bytes: int = Field(default=5_000_000_000, ge=0)
    pools: dict[Category, MediaPoolPolicy] = Field(
        default_factory=lambda: {
            "observation-inputs": MediaPoolPolicy(quota_bytes=5_000_000_000),
            "requested-edit-inputs": MediaPoolPolicy(),
            "assets": MediaPoolPolicy(quota_bytes=20_000_000_000, automatic_delete=False),
            "edge-renders": MediaPoolPolicy(quota_bytes=20_000_000_000),
            "render-projects": MediaPoolPolicy(quota_bytes=1_000_000_000),
            "voice-audio": MediaPoolPolicy(quota_bytes=1_000_000_000),
        }
    )


class ConfigureMediaStorage(Contract):
    expected_revision: int = Field(ge=0)
    policy: MediaStoragePolicy


class MediaStorageQuery(Contract):
    target: Literal["local", "peer"] = "peer"


class MediaStorageUpdate(ConfigureMediaStorage):
    target: Literal["local", "peer"] = "peer"


class CollectMediaStorage(MediaStorageQuery):
    category: Category


class ProtectMediaFile(CollectMediaStorage):
    file_name: str = Field(pattern=r"^[\w.-]{1,200}$")
    protected: bool


class MediaStorage:
    def __init__(self, host):
        self.host, self.store = host, host.store
        self.root = self.store.root.resolve()
        self.download_lock = asyncio.Lock()
        self.locks = self.root / "media-leases"
        self.locks.mkdir(exist_ok=True)
        with self.store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS managed_media_files (
                    path TEXT PRIMARY KEY, category TEXT NOT NULL, owner TEXT,
                    size INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                    touched REAL NOT NULL, protected INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'available', expired REAL);
                CREATE TABLE IF NOT EXISTS media_space_reservations (
                    id TEXT PRIMARY KEY,category TEXT NOT NULL,bytes INTEGER NOT NULL);
            """)

    def configuration(self):
        configuration = self.store.meta(
            "media_storage_policy",
            {"revision": 0, "policy": MediaStoragePolicy().model_dump(mode="json")},
        )
        # Older installations predate the separate project archive pool.
        configuration["policy"]["pools"].setdefault(
            "render-projects", MediaPoolPolicy(quota_bytes=1_000_000_000).model_dump()
        )
        return configuration

    def configure(self, request):
        if any(p.quota_bytes is None for p in request.policy.pools.values()) and not getattr(
            self.store, "shared_media_storage", None
        ):
            raise Conflict("unbounded local pools require the shared storage budget")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT value FROM metadata WHERE key='media_storage_policy'"
            ).fetchone()
            revision = json.loads(row[0])["revision"] if row else 0
            if revision != request.expected_revision:
                raise Conflict("media storage policy changed")
            result = {"revision": revision + 1, "policy": request.policy.model_dump(mode="json")}
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES (?,?)",
                ("media_storage_policy", canonical(result)),
            )
        return result

    @staticmethod
    def fingerprint(path):
        stat = path.stat()
        return canonical([stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns])

    def owned_path(self, path, category):
        path = Path(path)
        parent = self.root / category
        if path.resolve() != path or not path.is_relative_to(parent) or parent.resolve() != parent:
            raise Conflict("media path is outside its owned pool")
        return path

    @contextmanager
    def pin(self, path, *, exclusive=False):
        key = hashlib.sha256(str(Path(path).resolve()).encode()).hexdigest()
        with (self.locks / (key + ".lock")).open("a") as handle:
            mode = fcntl.LOCK_EX | fcntl.LOCK_NB if exclusive else fcntl.LOCK_SH
            fcntl.flock(handle, mode)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def register(self, path, category, *, owner=None, protected=False):
        path = self.owned_path(path, category)
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO managed_media_files
                (path,category,owner,size,fingerprint,touched,protected,status)
                VALUES (?,?,?,?,?,?,?,'available') ON CONFLICT(path) DO UPDATE SET
                size=excluded.size,fingerprint=excluded.fingerprint,touched=excluded.touched,
                status='available',expired=NULL,owner=excluded.owner""",
                (
                    str(path),
                    category,
                    owner,
                    path.stat().st_size,
                    self.fingerprint(path),
                    self.store.clock(),
                    int(protected),
                ),
            )

    def expired(self, path):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT status FROM managed_media_files WHERE path=?", (str(path),)
            ).fetchone()
        return bool(row and row[0] == "expired")

    def availability(self, path, info, registered_status):
        """Read-only display state; file presence is not a new media verification."""
        path = Path(path)
        if registered_status == "expired":
            # Preserve the existing legacy delivery-copy reload contract. Shared
            # library expiry deliberately cannot resurrect a retained-file copy.
            reloadable = (
                not getattr(self.store, "shared_media_storage", None)
                and bool(info.get("asset", {}).get("remote_job_id"))
            )
            return {"state": "reloadable" if reloadable else "expired"}
        try:
            present = path.resolve().is_relative_to(self.root) and path.is_file()
        except OSError:
            present = False
        return {"state": "available" if present else "missing"}

    def release_temporary(self, path, category):
        """Drop one regenerable input only after all actual readers have released it."""
        if category != "requested-edit-inputs":
            raise Conflict("only requested editing inputs use immediate release")
        path = self.owned_path(path, category)
        try:
            with self.pin(path, exclusive=True), self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM managed_media_files WHERE path=? AND category=?",
                                 (str(path), category)).fetchone()
                if (not row or self._busy(db, row)
                        or (path.exists() and self.fingerprint(path) != row["fingerprint"])):
                    return False
                path.unlink(missing_ok=True)
                db.execute("UPDATE managed_media_files SET status='expired',expired=? WHERE path=?",
                           (self.store.clock(), str(path)))
                return True
        except BlockingIOError:
            return False

    def protect(self, request):
        path = self.owned_path(self.root / request.category / request.file_name, request.category)
        with self.store.connect() as db:
            result = db.execute(
                "UPDATE managed_media_files SET protected=? WHERE path=? AND status='available'",
                (int(request.protected), str(path)),
            )
            if not result.rowcount:
                raise Conflict("only registered available files can be protected")
        return {
            "applied": True,
            "category": request.category,
            "file_name": request.file_name,
            "protected": request.protected,
        }

    def _consumer_finished(self, db, row):
        if not row["owner"]:
            return False
        task = db.execute("SELECT status,version FROM business_tasks WHERE id=?",
                          (row["owner"],)).fetchone()
        if not task:
            return False
        if task["status"] in {"completed", "cancelled", "failed", "expired"}:
            return True
        if row["category"] != "observation-inputs":
            return False
        asset = db.execute(
            "SELECT info FROM assets WHERE path=? AND task_id=? AND kind='observation_input'",
            (row["path"], row["owner"]),
        ).fetchone()
        binding = json.loads(asset["info"]).get("binding", {}) if asset else {}
        version = binding.get("expected_version")
        if not isinstance(version, int):
            return False
        if db.execute(
            "SELECT 1 FROM harness_jobs WHERE task_id=? AND version=? AND status IN ('queued','running','waiting')",
            (row["owner"], version),
        ).fetchone():
            return False
        if version < task["version"]:
            return True  # Explicit revision/update, not a mere timer wake.
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='media_review_decisions'").fetchone():
            return bool(db.execute(
                "SELECT 1 FROM media_review_decisions WHERE task_id=? AND version=? AND verdict='approve'",
                (row["owner"], version),
            ).fetchone())
        return False

    def _busy(self, db, row):
        if row["protected"]:
            return True
        if row["owner"]:
            task = db.execute(
                "SELECT status FROM business_tasks WHERE id=?", (row["owner"],)
            ).fetchone()
            if task and not self._consumer_finished(db, row):
                return True
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='feishu_deliveries'").fetchone():
            if db.execute(
                "SELECT 1 FROM feishu_deliveries d,json_each(d.body,'$.media') m WHERE d.status!='delivered' AND m.value=? LIMIT 1",
                (row["path"],),
            ).fetchone():
                return True
        return False

    def snapshot(self):
        policy = MediaStoragePolicy.model_validate(self.configuration()["policy"])
        pools = {}
        with self.store.connect() as db:
            for category, rule in policy.pools.items():
                folder = self.root / category
                files = (
                    [p for p in folder.rglob("*") if p.is_file() and not p.is_symlink()]
                    if folder.exists()
                    else []
                )
                owned = db.execute(
                    "SELECT COUNT(*),COALESCE(SUM(size),0) FROM managed_media_files WHERE category=? AND status='available'",
                    (category,),
                ).fetchone()
                pools[category] = {
                    "path": str(folder),
                    "used_bytes": sum(p.stat().st_size for p in files),
                    "registered_files": owned[0],
                    "registered_bytes": owned[1],
                    **rule.model_dump(),
                }
                pools[category]["recent_files"] = [
                    {
                        "file_name": Path(row["path"]).name,
                        "owner": row["owner"],
                        "bytes": row["size"],
                        "protected": bool(row["protected"]),
                        "status": row["status"],
                    }
                    for row in db.execute(
                        "SELECT * FROM managed_media_files WHERE category=? ORDER BY touched DESC LIMIT 100",
                        (category,),
                    )
                ]
        return {
            **self.configuration(),
            "pools": pools,
            "disk_free_bytes": shutil.disk_usage(self.root).free,
            "shared_storage": (
                self.store.shared_media_storage.snapshot()
                if getattr(self.store, "shared_media_storage", None) else None
            ),
            "historical_unregistered_files": "counted_in_usage_but_not_automatically_deleted",
        }

    def collect(self, category, *, reserve_bytes=0, reservation_id=None):
        policy = MediaStoragePolicy.model_validate(self.configuration()["policy"])
        rule = policy.pools.get(category)
        if rule is None:
            raise Conflict("media pool has no configured budget")
        folder = self.root / category
        folder.mkdir(exist_ok=True)
        removed = []
        # Serialize quota decisions across API workers and processes.
        with self.pin(self.locks / "collector", exclusive=True), self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if category in {"assets", "edge-renders"}:
                reap_render_work(folder)
            for reservation in db.execute("SELECT id FROM media_space_reservations").fetchall():
                try:
                    with self.pin(self.locks / reservation["id"], exclusive=True):
                        db.execute(
                            "DELETE FROM media_space_reservations WHERE id=?", (reservation["id"],)
                        )
                except BlockingIOError:
                    pass
            used = sum(
                p.stat().st_size for p in folder.rglob("*") if p.is_file() and not p.is_symlink()
            )
            pending = db.execute(
                "SELECT COALESCE(SUM(bytes),0) FROM media_space_reservations WHERE category=?",
                (category,),
            ).fetchone()[0]
            total_pending = db.execute(
                "SELECT COALESCE(SUM(bytes),0) FROM media_space_reservations"
            ).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM managed_media_files WHERE category=? AND status='available' ORDER BY touched",
                (category,),
            ).fetchall()
            reclaim = bool(rule.quota_bytes and used + pending + reserve_bytes > rule.quota_bytes * rule.high_water)
            for row in rows:
                pressure = reclaim and used + pending + reserve_bytes > rule.quota_bytes * (rule.low_water or rule.high_water)
                aged = (
                    rule.max_age_seconds is not None
                    and self.store.clock() - row["touched"] >= rule.max_age_seconds
                )
                completed = rule.delete_completed and self._consumer_finished(db, row)
                if not rule.automatic_delete or not (pressure or aged or completed) or self._busy(db, row):
                    continue
                path = self.owned_path(row["path"], category)
                try:
                    with self.pin(path, exclusive=True):
                        if path.exists():
                            if self.fingerprint(path) != row["fingerprint"]:
                                continue
                            path.unlink()
                            used -= row["size"]
                        db.execute(
                            "UPDATE managed_media_files SET status='expired',expired=? WHERE path=?",
                            (self.store.clock(), str(path)),
                        )
                        removed.append(str(path))
                except BlockingIOError:
                    continue
            writable = (
                (rule.quota_bytes is None or used + pending + reserve_bytes <= rule.quota_bytes)
                and shutil.disk_usage(self.root).free
                >= policy.min_free_bytes + total_pending + reserve_bytes
            )
            if writable and reservation_id:
                db.execute(
                    "INSERT INTO media_space_reservations VALUES (?,?,?)",
                    (reservation_id, category, reserve_bytes),
                )
            return {
                "category": category,
                "removed": removed,
                "used_bytes": max(0, used),
                "writable": writable,
            }

    @contextmanager
    def reserve(self, category, size):
        import uuid

        ident = uuid.uuid4().hex
        shared = getattr(self.store, "shared_media_storage", None)
        with (shared.reserve(size) if shared else nullcontext()), self.pin(self.locks / ident):
            receipt = self.collect(category, reserve_bytes=size, reservation_id=ident)
            if not receipt["writable"]:
                raise Conflict("media pool or disk free-space budget exhausted: " + category)
            try:
                yield
            finally:
                with self.store.connect() as db:
                    db.execute("DELETE FROM media_space_reservations WHERE id=?", (ident,))
