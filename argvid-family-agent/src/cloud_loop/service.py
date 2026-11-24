"""One local worker routes durable events into an existing agent runtime."""

import asyncio
import fcntl
import logging
from pathlib import Path

from .store import Store

log = logging.getLogger(__name__)


class Service:
    def __init__(self, root: Path, agent, *, store=None):
        self.store = store if store is not None else Store(root)
        self.agent = agent
        self._lock = None
        self._worker = None
        self._stop = asyncio.Event()

    async def start(self):
        self._lock = (self.store.root / "worker.lock").open("a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._lock.close()
            self._lock = None
            raise RuntimeError("This data directory already has a running worker") from None
        self.store.recover()
        self._worker = asyncio.create_task(self._loop())

    async def close(self):
        self._stop.set()
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        await self.agent.close()
        if self._lock:
            self._lock.close()
            self._lock = None

    async def _loop(self):
        while not self._stop.is_set():
            if not await self.process_one():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.25)
                except TimeoutError:
                    pass

    async def process_one(self):
        event = self.store.claim()
        if event is None:
            return False
        try:
            task = self.store.task(event["task_id"])
            result = await self.agent.run(task, event)
            self.store.finish(
                event, answer=result["answer"], trace=result, error=result.get("error")
            )
        except asyncio.CancelledError:
            # Leave the claimed event in 'running' for explicit startup recovery.
            raise
        except Exception as exc:
            # Never expose provider exception strings, headers or credential-bearing URLs.
            kind = type(exc).__name__
            log.error("Agent run failed: task=%s error_type=%s", event["task_id"], kind)
            self.store.finish(
                event,
                answer="模型运行失败，请检查模型服务配置后重试。",
                trace={"error_type": kind},
                error=kind,
            )
        return True
