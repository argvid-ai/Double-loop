"""Read-only counts; filesystem space, shared quota and raw watermarks differ."""
import shutil
import sqlite3
from pathlib import Path


def existing_files(rows, *, content=False):
    identities = set()
    for row in rows:
        path = Path(row["path"])
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        if not path.is_file() or path.is_symlink():
            continue
        identities.add(row["sha"] if content and row["sha"] else (stat.st_dev, stat.st_ino))
    return identities


def query_summary(host, principal, snapshot):
    disk = shutil.disk_usage(host.store.root)
    shared = getattr(host.store, "shared_media_storage", None)
    result = {
        "observed_at": host.store.clock(), "unit": "GB = 1,000,000,000 bytes",
        "filesystem": {"total_gb": round(disk.total / 1e9, 2), "used_gb": round(disk.used / 1e9, 2),
                       "available_gb": round(disk.free / 1e9, 2)},
        "counts": {}, "count_errors": [], "managed": None, "raw_queue": None,
        "definitions": {
            "raw_files": "现存受管原片文件；不是两秒判断批次数，不是全部历史录像",
            "candidate_files": "候选库中实际存在的文件，不是初筛标记数",
            "approved_films": "有明确批准记录且文件仍存在的成片，按已登记内容哈希去重；不含未批准草稿或仅登记为assets的原片",
            "capacity": "全机可用空间与项目额度余量不同；原片整理水位只比较原片占用，不能拿共享总占用与原片水位比",
        },
    }
    if not shared:
        result["count_errors"].append("shared_storage_not_configured")
        return result
    usage = snapshot["shared_storage"]
    cfg = shared.config
    remaining = max(0, cfg.total_bytes - usage["used_bytes"] - usage["reserved_bytes"])
    result["managed"] = {
        "limit_gb": round(cfg.total_bytes / 1e9, 2),
        "used_gb": round(usage["used_bytes"] / 1e9, 2),
        "reserved_gb": round(usage["reserved_bytes"] / 1e9, 2),
        "remaining_gb": round(remaining / 1e9, 2),
    }
    result["raw_queue"] = {
        "used_gb": round(usage["raw"] / 1e9, 2), "limit_gb": round(cfg.raw_bytes / 1e9, 2),
        "review_at_gb": round(cfg.review_bytes / 1e9, 2),
        "collect_at_gb": round(cfg.collect_bytes / 1e9, 2),
        "at_or_above_review": usage["raw"] >= cfg.review_bytes,
        "at_or_above_collection": usage["raw"] >= cfg.collect_bytes,
    }
    raw, films = set(), set()
    for kind, roots in (("raw_files", cfg.raw_stores), ("approved_films", cfg.derived_stores)):
        failed = False
        for root in roots:
            database = next((Path(root) / (side + ".sqlite3") for side in ("edge", "cloud")
                             if (Path(root) / (side + ".sqlite3")).is_file()), None)
            if database is None:
                result["count_errors"].append(kind + ":store_unavailable")
                failed = True
                continue
            try:
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
                    db.row_factory = sqlite3.Row
                    if kind == "raw_files":
                        rows = db.execute("SELECT owned_path AS path FROM raw_recordings WHERE principal=? AND state='available'",
                                          (principal,)).fetchall()
                        raw.update(existing_files(rows))
                    else:
                        rows = db.execute("""SELECT DISTINCT a.path,json_extract(a.info,'$.asset.sha256') sha
                            FROM media_review_decisions r JOIN assets a ON a.id=r.asset_id
                            JOIN business_tasks t ON t.id=r.task_id
                            WHERE t.principal=? AND r.verdict='approve'""", (principal,)).fetchall()
                        films.update(existing_files(rows, content=True))
            except (OSError, sqlite3.Error):
                failed = True
                result["count_errors"].append(kind + ":query_unavailable")
        result["counts"][kind] = None if failed else len(raw if kind == "raw_files" else films)
    try:
        with sqlite3.connect(shared.database.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT path FROM candidates WHERE principal=?", (principal,)).fetchall()
        result["counts"]["candidate_files"] = len(existing_files(rows))
    except (OSError, sqlite3.Error):
        result["counts"]["candidate_files"] = None
        result["count_errors"].append("candidate_files:query_unavailable")
    return result


def storage_with_summary(host, principal):
    snapshot = host.media_storage.snapshot()
    return {**snapshot, "query_summary": query_summary(host, principal, snapshot)}
