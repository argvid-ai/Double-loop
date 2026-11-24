"""File-library index and camera/source inventory, without invented live devices."""

import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, model_validator

from .contracts import Contract, canonical
from .store import Conflict


def media_title(value, fallback="视频片段"):
    """A display label, never a task instruction or a storage identity."""
    text = re.sub(r"(?:task-|record-|media-job-|received-)[\w-]+", "", str(value or ""))
    text = re.sub(r"[\x00-\x1f/\\<>]", " ", text).strip()
    text = re.split(r"[。；;\n]", text)[0].strip(" ，,：:")
    return text[:24].rstrip() if text else fallback


def explicit_review_title(notes):
    """Accept the explicit title label emitted by earlier model/tool calls."""
    match = re.match(
        r"\s*(?:title|内容标题|标题)\s*[:=：]\s*([^\n。]+)", str(notes or ""), re.IGNORECASE
    )
    return media_title(match.group(1)) if match else None


class CameraSpec(Contract):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,60}$")
    title: str = Field(min_length=1, max_length=100)
    kind: str = Field(pattern="^(file_replay|usb|rtsp)$")
    source_id: str | None = None
    device: str | None = None
    url: str | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def connection(self):
        if self.kind == "file_replay" and not self.source_id:
            raise ValueError("select a registered video")
        if self.kind == "usb" and (
            not self.device or not re.fullmatch(r"/dev/video[0-9]+", self.device)
        ):
            raise ValueError("select a discovered video device")
        if self.kind == "rtsp":
            u = urlparse(self.url or "")
            if (
                u.scheme not in {"rtsp", "rtsps"}
                or not u.hostname
                or u.username
                or u.password
                or u.query
                or u.fragment
            ):
                raise ValueError(
                    "use a credential-free RTSP endpoint; authenticated cameras require their adapter"
                )
        return self


class MediaCatalog:
    def __init__(self, host):
        self.host = host
        self.store = host.store
        self.probe_locks = {}
        with self.store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS camera_sources (id TEXT PRIMARY KEY,principal TEXT NOT NULL,spec TEXT NOT NULL,probe TEXT,updated REAL NOT NULL)"
            )
            if "revision" not in {
                r["name"] for r in db.execute("PRAGMA table_info(camera_sources)")
            }:
                db.execute(
                    "ALTER TABLE camera_sources ADD COLUMN revision TEXT NOT NULL DEFAULT ''"
                )

    def files(self, principal, category=None, task_id=None, query=""):
        items = []
        for s in self.host.sources.list(principal):
            items.append(
                {
                    "id": "source:" + s["source_id"],
                    "category": "original",
                    "title": media_title(s["title"], "原始录像"),
                    "source_id": s["source_id"],
                    "task_id": None,
                    "side": self.host.side,
                    "facts": s["facts"],
                    "provenance": s,
                    "created": s["registered_at"],
                    "url": "sources/" + s["source_id"] + "/video",
                    "simulated": s["simulated"],
                }
            )
        with self.store.connect() as db:
            headings = {}
            for row in db.execute(
                "SELECT json_extract(reply,'$.reviewed_result.asset_id') AS asset_id, "
                "json_extract(reply,'$.text') AS text FROM harness_events "
                "WHERE principal=? AND kind='media.review' AND status='done'",
                (principal,),
            ):
                match = re.search(r"^#{1,6}\s+([^\n]+)", row["text"] or "", re.MULTILINE)
                if not match:
                    match = re.search(
                        r"^\*\*\[([^\]\n]+)\]\(cloud-asset://[^)]+\)\*\*",
                        row["text"] or "",
                    )
                if row["asset_id"] and match:
                    headings[row["asset_id"]] = media_title(match.group(1))
        assets = self.host.effects.assets(None, principal)
        film_titles = {
            a.get("business_task_id") or a.get("task_id"): a["info"]["title"]
            for a in assets
            if a["kind"] in {"deliverable", "compose", "render"} and a["info"].get("title")
        }
        for a in assets:
            category_name = (
                "deliverable" if a["kind"] in {"deliverable", "compose", "render"} else "working"
            )
            owner = a.get("business_task_id") or a.get("task_id")
            facts = a["info"].get("asset") or {}
            items.append(
                {
                    "id": "asset:" + a["id"],
                    "category": category_name,
                    "title": media_title(
                        a["info"].get("title") or headings.get(a["id"]),
                        "剪辑短片"
                        if category_name == "deliverable"
                        else media_title(film_titles.get(owner, "观察候选") + "·素材"),
                    ),
                    "created": a.get("created"),
                    "task_id": owner,
                    "side": self.host.side,
                    "facts": facts,
                    "storage_side": facts.get("storage_side", self.host.side),
                    "local_copy_role": facts.get("local_copy_role"),
                    "provenance": a["info"],
                    "availability": a["availability"],
                    "version": a.get("job_version"),
                    "url": "assets/" + a["id"],
                    "simulated": a["info"].get("simulated", True),
                }
            )
        observation = getattr(self.host, "observations", None)
        if observation and observation.installation and observation.installation.archive:
            archive = observation.installation.archive
            with self.store.connect() as db:
                projects = [
                    r[0]
                    for r in db.execute(
                        "SELECT DISTINCT project_id FROM observation_storage WHERE principal=?",
                        (principal,),
                    )
                ]
            for project in projects:
                for asset in archive.list_media(principal, project):
                    items.append(
                        {
                            "id": "observation:" + asset["id"],
                            "category": "deliverable",
                            "title": media_title(asset["info"].get("title"), "观察片段"),
                            "task_id": asset["info"]["task_id"],
                            "side": self.host.side,
                            "facts": asset["info"]["asset"],
                            "created": asset["created"],
                            "url": "observation/media/" + asset["id"],
                            "simulated": asset["info"]["simulated"],
                        }
                    )
        return [
            x
            for x in items
            if (not category or x["category"] == category)
            and (not task_id or x["task_id"] == task_id)
            and (
                not query
                or query.casefold() in (x["title"] + " " + str(x.get("source_id", ""))).casefold()
            )
        ]

    def discover(self):
        from .input_devices import discover_inputs

        inventory = discover_inputs()
        return {
            "devices": [
                {
                    "device": item["address"],
                    "title": item["name"],
                    "configured": False,
                    "preview_verified": False,
                }
                for item in inventory["devices"]
                if item["kind"] == "video"
                and item.get("connection") == "usb"
                and item.get("capture_capable") is True
            ],
            "platform": inventory["platform"],
            "note": "Only enumerated USB capture-capable devices; actual frames remain unverified.",
        }

    def save(self, principal, args):
        if args.kind == "file_replay":
            self.host.sources.get(principal, args.source_id)
        if args.kind == "usb" and args.device not in {
            x["device"] for x in self.discover()["devices"]
        }:
            raise ValueError("video device not found on this node")
        with self.store.connect() as db:
            old = db.execute(
                "SELECT principal FROM camera_sources WHERE id=?", (args.id,)
            ).fetchone()
            if old and old["principal"] != principal:
                raise Conflict("source owner mismatch")
            db.execute(
                "INSERT OR REPLACE INTO camera_sources (id,principal,spec,probe,updated,revision) VALUES (?,?,?,NULL,?,?)",
                (args.id, principal, canonical(args), self.store.clock(), uuid.uuid4().hex),
            )
        return self.get(principal, args.id)

    def get(self, principal, ident):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM camera_sources WHERE id=? AND principal=?", (ident, principal)
            ).fetchone()
        if not row:
            raise KeyError(ident)
        return {
            **json.loads(row["spec"]),
            "last_probe": json.loads(row["probe"]) if row["probe"] else None,
            "simulated": json.loads(row["spec"])["kind"] == "file_replay",
            "vision_available": False,
            "recording_available": json.loads(row["spec"])["kind"] == "file_replay"
            and "capture" in self.host.binding["capabilities"]
            and "start_job" in self.host.binding["tools"],
        }

    def cameras(self, principal):
        with self.store.connect() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM camera_sources WHERE principal=? ORDER BY updated DESC",
                    (principal,),
                )
            ]
        return [self.get(principal, x) for x in ids]

    def seed_demo_replay(self, principal):
        if self.cameras(principal):
            return
        if any(s["source_id"] == "licensed-cat" for s in self.host.sources.list(principal)):
            self.save(
                principal,
                CameraSpec(
                    id="demo-replay",
                    title="猫咪视频回放",
                    kind="file_replay",
                    source_id="licensed-cat",
                ),
            )

    async def probe(self, principal, ident):
        async with self.probe_locks.setdefault(ident, asyncio.Lock()):
            return await self._probe(principal, ident)

    async def _probe(self, principal, ident):
        from .harness_media_contracts import SourceFrame

        value = self.get(principal, ident)
        with self.store.connect() as db:
            revision = db.execute(
                "SELECT revision FROM camera_sources WHERE id=? AND principal=?", (ident, principal)
            ).fetchone()[0]
        if not value["enabled"]:
            raise Conflict("source disabled")
        try:
            if value["kind"] == "file_replay":
                frame = await asyncio.to_thread(
                    self.host.sources.frame,
                    principal,
                    SourceFrame(source_id=value["source_id"], position_ms=0),
                )
                result = {
                    "connected": True,
                    "preview_available": True,
                    "frame_path": frame["path"],
                    "frame_time_ms": frame["frame_time_ms"],
                }
            else:
                folder = self.store.root / "camera-previews"
                folder.mkdir(exist_ok=True)
                ident_capture = uuid.uuid4().hex
                private = folder / (ident_capture + ".json")
                destination = folder / (ident_capture + ".jpg")
                private.write_text(
                    canonical(
                        {
                            "kind": value["kind"],
                            "device": value.get("device"),
                            "url": value.get("url"),
                            "output": str(destination),
                        }
                    )
                )
                os.chmod(private, 0o600)
                try:
                    proc = await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "cloud_loop.harness_camera_frame",
                        str(private),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        await asyncio.wait_for(proc.wait(), 12)
                    except BaseException:
                        if proc.returncode is None:
                            proc.kill()
                        await proc.wait()
                        raise
                    if proc.returncode:
                        raise ValueError("camera preview unavailable")
                    result = {
                        "connected": True,
                        "preview_available": True,
                        "frame_path": str(destination),
                    }
                finally:
                    private.unlink(missing_ok=True)
        except (ValueError, OSError, TimeoutError) as exc:
            result = {
                "connected": False,
                "preview_available": False,
                "error_type": type(exc).__name__,
            }
        result.update(
            checked_at=self.store.clock(),
            simulated=value["simulated"],
            vision_available=False,
            recording_available=value["recording_available"],
        )
        with self.store.connect() as db:
            changed = db.execute(
                "UPDATE camera_sources SET probe=? WHERE id=? AND principal=? AND revision=?",
                (canonical(result), ident, principal, revision),
            ).rowcount
        if not changed:
            if value["kind"] != "file_replay" and result.get("frame_path"):
                Path(result["frame_path"]).unlink(missing_ok=True)
            raise Conflict(
                "camera configuration changed during preview; test the current source again"
            )
        return {k: v for k, v in result.items() if k != "frame_path"}
