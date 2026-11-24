"""Requested edge observation files bound to an existing cloud editing task.

Listing or saving a clip never invokes this transport. Raw file bytes, not a
model-supplied URL/path/manifest, are verified before they become job inputs.
"""

import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import threading
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Literal

import av
import httpx
from pydantic import Field, model_validator

from .contracts import Contract, canonical, digest
from .media import file_hash, inspect_video
from .observation_contracts import Counter, Reference, Version
from .observation_media import _fingerprint
from .store import Conflict


class ObservationMediaSelection(Contract):
    project_id: Reference
    asset_id: Reference = Field(description=(
        "已存在的来源素材编号，不是给片段起名。raw_input时逐字复制标记/证据返回的media_ref；"
        "retained时复制已导出视频的asset_id。不要编造night-floor-a这类别名。"))
    source_kind: Literal["retained", "raw_input"] = Field(
        default="retained", description="raw_input读取真实原片；retained读取已导出视频。")
    start_ms: Counter | None = None
    end_ms: Counter | None = None

    @model_validator(mode="after")
    def interval(self):
        if (self.start_ms is None) != (self.end_ms is None):
            raise ValueError("provide both input boundaries")
        if self.start_ms is not None and (
            self.source_kind != "raw_input"
            or self.end_ms <= self.start_ms
            or self.end_ms - self.start_ms > 60000
        ):
            raise ValueError("requested raw input must be a nonempty window at most sixty seconds")
        return self


class AttachObservationMedia(ObservationMediaSelection):
    task_id: Reference = Field(description="接收素材的当前云端剪辑业务任务编号，不是录像任务编号。")
    expected_version: Version = Field(description=(
        "当前云端剪辑业务任务版本，从save_task/begin_media_output/get_task_context返回值读取，不猜版本。"))


class ObservationAssetQuery(ObservationMediaSelection):
    pass


def requested_input_id(principal, selection):
    return (
        "requested-input-"
        + digest(
            {
                "principal": principal,
                **ObservationAssetQuery.model_validate(
                    selection.model_dump(exclude={"task_id", "expected_version"})
                ).model_dump(mode="json"),
            }
        )[:32]
    )


class ObservationAttachments:
    def __init__(self, host):
        self.host, self.store = host, host.store
        self.lock = asyncio.Lock()
        self.root = self.store.root.resolve() / "observation-inputs"
        with self.store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS observation_input_downloads (path TEXT PRIMARY KEY)"
            )

    @contextmanager
    def _download_owner(self):
        # Separate from the server worker lease, also used by one-shot tools.
        # The OS releases this lock on process death; only then may a successor
        # remove incomplete files explicitly journaled by this component.
        with (self.store.root / "observation-input.lock").open("a") as lease:
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Conflict("another observation download is active") from None
            try:
                with self.store.connect() as db:
                    for row in db.execute(
                        "SELECT path FROM observation_input_downloads"
                    ).fetchall():
                        path = self._path(row["path"])
                        if path.parent != self.root or not path.name.startswith(".incoming-"):
                            raise Conflict("invalid pending download record")
                        path.unlink(missing_ok=True)
                        db.execute(
                            "DELETE FROM observation_input_downloads WHERE path=?", (str(path),)
                        )
                yield
            finally:
                fcntl.flock(lease, fcntl.LOCK_UN)

    def _guard(self, event, args, db=None):
        if self.host.side != "cloud" or event["read_only"]:
            raise Conflict("editing attachment requires a writable cloud task")
        if db is None:
            with self.store.connect() as connection:
                return self._guard(event, args, connection)
        current = self.store.event(event["id"], event["principal"], _db=db)
        if current["status"] != "running":
            raise Conflict("input no longer owns this attachment")
        from .harness_authority import local_work, require_work_target

        if not local_work(event):
            raise Conflict("editing requires an authorized work turn")
        require_work_target(self.store, event, args.task_id)
        task = self.store._task(db, args.task_id, event["principal"])
        if task["version"] != args.expected_version or task["status"] in {
            "paused",
            "cancelled",
            "completed",
            "expired",
        }:
            raise Conflict("editing task changed or ended")
        if db.execute(
            "SELECT 1 FROM peer_task_bindings WHERE task_id=? AND expires<=?",
            (args.task_id, self.store.clock()),
        ).fetchone():
            raise Conflict("editing task authorization expired")
        if task["kind"] not in {"goal", "edit"} or task["constraints"].get("media_recipe"):
            raise Conflict(
                "attach to a goal without a frozen recipe, then select its inputs in start_job; existing recipes cannot be silently replaced"
            )
        return task

    def _path(self, path):
        path = Path(path)
        if (
            self.root.resolve() != self.root
            or path.resolve() != path
            or not path.is_relative_to(self.root)
        ):
            raise Conflict("observation input cache path changed")
        return path

    async def attach(self, event, raw):
        args = AttachObservationMedia.model_validate(raw)
        async with self.lock:
            self._guard(event, args)
            with self._download_owner():
                with self.host.media_storage.reserve(
                    "observation-inputs", self.host.settings.observation_input_file_bytes
                ):
                    result = await self._attach(event, args)
                    path = self.root / (result["asset_id"] + ".mp4")
                    self.host.media_storage.register(path, "observation-inputs", owner=args.task_id)
                    return result

    async def _attach(self, event, args):
        self._guard(event, args)
        selection = args.model_dump(exclude={"task_id", "expected_version"}, exclude_defaults=True)
        binding = {
            **args.model_dump(exclude={"source_kind", "start_ms", "end_ms"}),
            **(
                {k: v for k, v in selection.items() if k in {"source_kind", "start_ms", "end_ms"}}
                if args.source_kind == "raw_input"
                else {}
            ),
            "principal": event["principal"],
            "origin_side": "edge",
        }
        ident = "observation-input-" + digest(binding)[:32]
        with self.store.connect() as db:
            old = db.execute("SELECT * FROM assets WHERE id=?", (ident,)).fetchone()
        if old and not self.host.media_storage.expired(self.root / (ident + ".mp4")):
            info = json.loads(old["info"])
            path = self._path(old["path"])
            if (
                info.get("binding") != binding
                or old["task_id"] != args.task_id
                or old["kind"] != "observation_input"
            ):
                raise Conflict("editing input binding collision")
            sha = await asyncio.to_thread(file_hash, path)
            if sha != info["asset"]["sha256"]:
                raise Conflict("cached observation media changed")
            self._guard(event, args)
            return {
                "asset_id": ident,
                "task_id": args.task_id,
                "task_version": args.expected_version,
                "verified": True,
                "from_cache": True,
                "asset": info["asset"],
                "binding": binding,
            }
        response = await self.host.client.post(
            "/harness/observation/asset",
            json=ObservationAssetQuery.model_validate(
                args.model_dump(exclude={"task_id", "expected_version"})
            ).model_dump(mode="json", exclude_defaults=args.source_kind == "retained"),
            timeout=180 if args.source_kind == "raw_input" else 10,
        )
        if response.status_code == 409:
            # Recognized peer errors are safe, actionable contract facts, not
            # arbitrary upstream text. Never turn rejection into successful input.
            try:
                error = response.json().get("error")
            except (ValueError, AttributeError):
                error = None
            explanations = {
                "requested input is not captured in this user/project":
                    "所填asset_id不是本项目已登记的原片。请重新查标记，逐字复制其media_ref作为asset_id；"
                    "不要用自起的片段名称，也不要为这个错误乱改剪辑任务版本。",
                "requested span exceeds actual registered media coverage":
                    "选段范围超出这份原片的实际覆盖。请核对同一media_ref的起止范围，跨文件分别绑定；不要改素材编号猜测。",
                "source changed during requested clipping":
                    "这份原片在取段期间仍发生变化。请重新读取实际范围，必要时选已封存文件，不要改任务版本绕过。",
                "requested input duration or original audio failed verification":
                    "取段后的时长或原声音轨没有通过核验。本次没有交付剪辑输入，不能当作成功；需要检查该原片及实际取段结果。",
            }
            if isinstance(error, str) and error in explanations:
                raise httpx.HTTPStatusError(
                    "原片绑定未应用：" + explanations[error],
                    request=response.request, response=response,
                )
        response.raise_for_status()
        remote = response.json()
        expected_remote = (
            requested_input_id(event["principal"], args)
            if args.source_kind == "raw_input"
            else args.asset_id
        )
        if (
            not isinstance(remote, dict)
            or remote.get("asset_id") != expected_remote
            or remote.get("project_id") != args.project_id
        ):
            raise Conflict("remote observation identity mismatch")
        expected = remote.get("sha256")
        size = remote.get("size_bytes")
        if (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(c not in "0123456789abcdef" for c in expected)
            or type(size) is not int
            or size <= 0
            or type(remote.get("simulated")) is not bool
            or not isinstance(remote.get("manifest"), dict)
        ):
            raise Conflict("remote observation lacks verified file facts")
        settings = self.host.settings
        if size > settings.observation_input_file_bytes:
            raise Conflict("selected observation exceeds input file limit")
        self.root.mkdir(exist_ok=True)
        destination = self._path(self.root / (ident + ".mp4"))
        if any(p.is_symlink() for p in self.root.iterdir()):
            raise Conflict("unmanaged symlink in editing input cache")
        used = sum(p.stat().st_size for p in self.root.iterdir() if p.is_file())
        reserve = 0 if destination.exists() else size
        if not getattr(self.store, "shared_media_storage", None) and used + reserve > settings.observation_input_cache_bytes:
            raise Conflict("observation input cache quota exhausted")
        if shutil.disk_usage(self.root).free < settings.observation_input_min_free_bytes + reserve:
            raise Conflict("editing input free-space floor reached")
        self._guard(event, args)
        temporary = None
        try:
            if not destination.exists():
                temporary = self.root / (".incoming-" + uuid.uuid4().hex)
                with self.store.connect() as db:
                    db.execute(
                        "INSERT INTO observation_input_downloads VALUES (?)", (str(temporary),)
                    )
                with temporary.open("xb") as stream:
                    count = 0
                    hasher = hashlib.sha256()
                    async with asyncio.timeout(120):
                        route = (
                            "/harness/observation/requested-input/"
                            if args.source_kind == "raw_input"
                            else "/harness/observation/media/"
                        ) + expected_remote
                        async with self.host.client.stream("GET", route, timeout=10) as incoming:
                            incoming.raise_for_status()
                            async for chunk in incoming.aiter_bytes(1024 * 1024):
                                self._guard(event, args)
                                count += len(chunk)
                                if count > size:
                                    raise Conflict("remote observation exceeds declared size")
                                if shutil.disk_usage(
                                    self.root
                                ).free < settings.observation_input_min_free_bytes + len(chunk):
                                    raise Conflict(
                                        "editing input storage floor reached during transfer"
                                    )
                                stream.write(chunk)
                                hasher.update(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if count != size or hasher.hexdigest() != expected:
                    raise Conflict("remote observation bytes differ from index")
            selected = temporary or destination
            stopped = threading.Event()

            def check():
                if stopped.is_set():
                    raise Conflict("attachment cancelled")
                self._guard(event, args)

            def inspect():
                before = _fingerprint(selected)
                facts = inspect_video(selected, check_active=check)
                with av.open(str(selected)) as media:
                    frame = next(media.decode(video=0))
                    rotation = frame.rotation
                facts.update(
                    rotation=rotation,
                    display_width=facts["height"] if abs(rotation) == 90 else facts["width"],
                    display_height=facts["width"] if abs(rotation) == 90 else facts["height"],
                )
                check()
                if _fingerprint(selected) != before:
                    raise Conflict("editing input changed during verification")
                return facts, before

            worker = asyncio.create_task(asyncio.to_thread(inspect))
            try:
                facts, fingerprint = await asyncio.shield(worker)
            except asyncio.CancelledError:
                stopped.set()
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                with suppress(Exception):
                    worker.result()
                raise
            if facts["sha256"] != expected or facts["size_bytes"] != size:
                raise Conflict("editing input failed file verification")
            info = {
                "asset": {**facts, "asset_id": ident, "simulated": remote["simulated"]},
                "binding": binding,
                "provenance": {
                    "origin": "requested_observation_attachment",
                    "source_manifest": remote["manifest"],
                },
                "simulated": remote["simulated"],
            }
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self._guard(event, args, db)
                self._path(selected)
                self._path(destination)
                if _fingerprint(selected) != fingerprint:
                    raise Conflict("editing input changed before publication")
                if temporary:
                    os.link(temporary, destination)
                    folder = os.open(self.root, os.O_RDONLY)
                    try:
                        os.fsync(folder)
                    finally:
                        os.close(folder)
                db.execute(
                    "INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?)",
                    (ident, args.task_id, str(destination), canonical(info), "observation_input"),
                )
                self.store._audit(
                    db,
                    args.task_id,
                    "observation.media.attached",
                    {
                        "event_id": event["id"],
                        "asset_id": ident,
                        "binding": binding,
                        "sha256": expected,
                        "bytes": size,
                        "requested_transfer": True,
                        "model_invoked": False,
                    },
                )
            return {
                "asset_id": ident,
                "task_id": args.task_id,
                "task_version": args.expected_version,
                "verified": True,
                "from_cache": False,
                "asset": info["asset"],
                "binding": binding,
            }
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
                with self.store.connect() as db:
                    db.execute(
                        "DELETE FROM observation_input_downloads WHERE path=?", (str(temporary),)
                    )
