"""Disposable browser derivatives; never replace source or formal saved media."""

import asyncio
import shutil
import tempfile
import threading
from contextlib import ExitStack, asynccontextmanager
from hashlib import sha256
from pathlib import Path

import av
from fastapi import HTTPException
from fastapi.responses import FileResponse

from .media import ffmpeg
from .media_storage import render_workspace, reap_render_work
from .store import Conflict


class BrowserVideoCache:
    max_input_bytes = 256 * 1024 * 1024
    budget_bytes = 512 * 1024 * 1024

    def __init__(self, root, *, budget_bytes=None, shared_storage=None, output_limit_bytes=None,
                 shutdown=None):
        if budget_bytes is not None:
            self.budget_bytes = budget_bytes
        self.root = Path(root) / "browser-video-cache"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self.shared = shared_storage
        self.output_limit_bytes = output_limit_bytes
        self.pending = {}
        self.shutdown = shutdown
        if self.shared:
            reap_render_work(self.root)

    async def _ephemeral(self, method, *args):
        """Shared installations retain a preview only while its HTTP response is in use."""
        async with self.lock:
            return await self._render_ephemeral(method, *args)

    async def _render_ephemeral(self, method, *args):
        context = ExitStack()
        try:
            folder = context.enter_context(render_workspace(self.root))
            # This is one operation's admission bound, not a separate cache quota.
            async with self._shared_admission(self.max_input_bytes * 4):
                cache = BrowserVideoCache(
                    folder, budget_bytes=self.max_input_bytes * 4,
                    output_limit_bytes=self.max_input_bytes * 2,
                    shutdown=self.shutdown,
                )
                result = await getattr(cache, method)(*args)
            self.pending[str(result)] = context
            return result
        except BaseException as exc:
            context.close()
            if isinstance(exc, Conflict):
                raise HTTPException(507, "shared storage has no room for a preview") from None
            raise

    @asynccontextmanager
    async def _shared_admission(self, amount):
        # The complete disk scan must not block the API event loop.
        reservation = self.shared.reserve(amount)
        work = asyncio.create_task(asyncio.to_thread(reservation.__enter__))
        try:
            await asyncio.shield(work)
        except asyncio.CancelledError:
            outcome = await asyncio.gather(work, return_exceptions=True)
            if not isinstance(outcome[0], BaseException):
                reservation.__exit__(None, None, None)
            raise
        try:
            yield
        finally:
            reservation.__exit__(None, None, None)

    def response(self, path, kind):
        context = self.pending.pop(str(path), None)

        class PreviewResponse(FileResponse):
            async def __call__(self, scope, receive, send):
                try:
                    return await super().__call__(scope, receive, send)
                finally:
                    if context:
                        context.close()

        return PreviewResponse(path, media_type="image/jpeg" if kind == "poster" else "video/mp4")

    def _limit(self, args):
        if self.output_limit_bytes is not None:
            args[-1:-1] = ["-fs", str(self.output_limit_bytes)]
        return args

    def _check_output(self, path):
        if self.output_limit_bytes is not None and path.stat().st_size >= self.output_limit_bytes:
            raise HTTPException(413, "preview output exceeded this operation's reserved bound")

    def _prune(self, needed, keep):
        entries = [
            (p, sum(f.stat().st_size for f in p.iterdir() if f.is_file()))
            for p in self.root.iterdir()
            if p.is_dir() and len(p.name) == 64
        ]
        used = sum(size for _, size in entries)
        for path, size in sorted(entries, key=lambda item: item[0].stat().st_mtime):
            if used + needed <= self.budget_bytes:
                break
            if path != keep:
                shutil.rmtree(path)
                used -= size
        if used + needed > self.budget_bytes:
            raise HTTPException(507, "browser preview cache is full")

    async def registered_range(self, source, start_ms, end_ms, kind):
        """Small display copy of an installed interval, never a retained clip.

        The caller authorizes the registered source on every request. Seek on
        the source node so a multi-GB MOV is never copied into the UI cache.
        """
        if self.shared:
            return await self._ephemeral("registered_range", source, start_ms, end_ms, kind)
        source = Path(source)
        before = source.stat()
        fingerprint = (str(source), before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if end_ms is None:
            with av.open(str(source)) as media:
                stream = media.streams.video[0]
                if stream.duration is None:
                    raise HTTPException(422, "preview requires a declared video duration")
                end_ms = int(((stream.start_time or 0) + stream.duration) * stream.time_base * 1000)
        if not 0 <= start_ms < end_ms or end_ms - start_ms > 600000:
            raise HTTPException(
                422, "browser preview requires an installed interval up to 10 minutes"
            )
        key = sha256(
            repr(("registered-range-v1", fingerprint, start_ms, end_ms)).encode()
        ).hexdigest()
        folder = self.root / key
        name = "poster.jpg" if kind == "poster" else "video.mp4"
        async with self.lock:
            if (folder / name).is_file():
                return folder / name
            if shutil.disk_usage(self.root).free < self.max_input_bytes * 2:
                raise HTTPException(507, "not enough space for browser preview")
            cancelled = threading.Event()

            def check():
                if cancelled.is_set() or (self.shutdown is not None and self.shutdown.is_set()):
                    raise RuntimeError("browser preview cancelled")

            with tempfile.TemporaryDirectory(prefix="render-", dir=self.root) as temp:
                output = Path(temp) / name
                args = [
                    "-y",
                    "-threads",
                    "2",
                    "-seek_timestamp",
                    "1",
                    "-ss",
                    str(start_ms / 1000),
                    "-i",
                    str(source),
                ]
                scale = "scale=720:720:force_original_aspect_ratio=decrease:force_divisible_by=2"
                if kind == "poster":
                    args += ["-frames:v", "1", "-vf", scale, "-q:v", "3", str(output)]
                else:
                    args += [
                        "-t",
                        str((end_ms - start_ms) / 1000),
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a:0?",
                        "-vf",
                        scale,
                        "-r",
                        "15",
                        "-c:v",
                        "libx264",
                        "-threads",
                        "2",
                        "-preset",
                        "ultrafast",
                        "-crf",
                        "24",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                        "-movflags",
                        "+faststart",
                        str(output),
                    ]
                worker = asyncio.create_task(asyncio.to_thread(ffmpeg, self._limit(args), check_active=check))
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    cancelled.set()
                    await asyncio.gather(worker, return_exceptions=True)
                    raise
                except Exception:
                    raise HTTPException(422, "cannot prepare registered video preview") from None
                after = source.stat()
                if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != fingerprint[1:]:
                    raise HTTPException(409, "source changed during preview preparation")
                self._check_output(output)
                self._prune(output.stat().st_size, folder)
                folder.mkdir(exist_ok=True)
                output.replace(folder / name)
                return folder / name

    async def get(self, identity, loader, kind):
        if self.shared:
            return await self._ephemeral("get", identity, loader, kind)
        # The caller revalidates the registered asset even for a cache hit.
        key = sha256(("browser-h264-aac-v1:" + identity).encode()).hexdigest()
        folder = self.root / key
        async with self.lock:
            if (folder / "video.mp4").is_file() and (folder / "poster.jpg").is_file():
                return folder / ("poster.jpg" if kind == "poster" else "video.mp4")
            if shutil.disk_usage(self.root).free < self.max_input_bytes * 2:
                raise HTTPException(507, "not enough space for browser preview")
            with tempfile.TemporaryDirectory(prefix="render-", dir=self.root) as temp:
                temp = Path(temp)
                source = temp / "source"
                await loader(source)
                if not 0 < source.stat().st_size <= self.max_input_bytes:
                    raise HTTPException(413, "media exceeds browser preview input limit")
                cancelled = threading.Event()

                def check():
                    if cancelled.is_set() or (self.shutdown is not None and self.shutdown.is_set()):
                        raise RuntimeError("browser preview cancelled")

                def render():
                    ffmpeg(
                        self._limit([
                            "-y",
                            "-i",
                            str(source),
                            "-map",
                            "0:v:0",
                            "-map",
                            "0:a:0?",
                            "-c:v",
                            "libx264",
                            "-threads",
                            "2",
                            "-preset",
                            "veryfast",
                            "-crf",
                            "20",
                            "-pix_fmt",
                            "yuv420p",
                            "-c:a",
                            "aac",
                            "-b:a",
                            "192k",
                            "-movflags",
                            "+faststart",
                            str(temp / "video.mp4"),
                        ]),
                        check_active=check,
                    )
                    self._check_output(temp / "video.mp4")
                    ffmpeg(
                        [
                            "-y",
                            "-i",
                            str(temp / "video.mp4"),
                            "-frames:v",
                            "1",
                            "-q:v",
                            "3",
                            str(temp / "poster.jpg"),
                        ],
                        check_active=check,
                    )

                worker = asyncio.create_task(asyncio.to_thread(render))
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    cancelled.set()
                    await asyncio.gather(worker, return_exceptions=True)
                    raise
                except Exception:
                    raise HTTPException(422, "cannot prepare browser video preview") from None
                source.unlink()
                size = sum(p.stat().st_size for p in temp.iterdir())
                self._prune(size, folder)
                folder.mkdir(exist_ok=True)
                for name in ("video.mp4", "poster.jpg"):
                    (temp / name).replace(folder / name)
            return folder / ("poster.jpg" if kind == "poster" else "video.mp4")
