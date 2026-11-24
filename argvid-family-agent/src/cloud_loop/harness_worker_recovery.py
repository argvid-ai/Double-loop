"""Retry failed host steps without restarting unrelated workers or model turns."""
import asyncio
import inspect
import logging

log = logging.getLogger(__name__)


class RecoveringSteps:
    """One instance per host loop; each named operation has its own backoff."""

    def __init__(self, host):
        self.host = host
        self.failures = {}

    async def run(self, name, operation):
        loop = asyncio.get_running_loop()
        attempts, due = self.failures.get(name, (0, 0))
        if loop.time() < due:
            return None
        try:
            value = operation()
            if inspect.isawaitable(value):
                value = await value
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempts += 1
            delay = min(30, 2 ** min(attempts - 1, 5))
            self.failures[name] = (attempts, loop.time() + delay)
            self._health(name, "retry_waiting", error_type=type(exc).__name__, retry_seconds=delay)
            # Do not log exception text: provider errors can contain credentials.
            if attempts == 1:
                log.warning("Host step %s failed (%s); automatic retry continues", name, type(exc).__name__)
            return None
        else:
            if name in self.failures:
                del self.failures[name]
                self._health(name, "healthy")
            return value

    def _health(self, name, code, **details):
        try:
            self.host.health.record("local-user", "worker_" + name, code, details=details)
        except Exception:
            # The database can itself be temporarily unavailable. The in-memory
            # loop remains alive, so failure reporting must not kill recovery.
            pass


async def finish_failed_step(host, event, error):
    """Release a claimed conversation after bookkeeping failed, not replay tools.

    If writing is temporarily unavailable, retain ownership and retry only this
    receipt. Other workers keep running. The next scheduled turn can then read
    the saved work and continue from actual receipts.
    """
    retry = RecoveringSteps(host)
    while not host.stop.is_set():
        def finish():
            current = host.store.event(event["id"], event["principal"])
            if current["status"] == "running":
                host.store.finish_event(event, {
                    "text": "本轮收尾出现错误，已有素材和工具结果保留，后续轮次继续处理。",
                    "error": type(error).__name__,
                    "source": "worker_bookkeeping_failure",
                }, error=type(error).__name__)
            return True
        if await retry.run("event_receipt", finish):
            return True
        await asyncio.sleep(0.2)
    return False


async def supervise_worker(host, name, operation):
    """Last-resort protection for exceptions in a loop's own error handling."""
    retry = RecoveringSteps(host)

    async def run():
        if "loop_" + name in retry.failures:
            retry._health("loop_" + name, "restarting")
        await operation()
        if not host.stop.is_set():
            raise RuntimeError("worker unexpectedly returned")

    while not host.stop.is_set():
        await retry.run("loop_" + name, run)
        if not host.stop.is_set():
            await asyncio.sleep(0.2)


async def finish_failed_job(host, job, error):
    """Do not leave a claimed render permanently running after bookkeeping fails."""
    retry = RecoveringSteps(host)
    while not host.stop.is_set():
        def finish():
            with host.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT status FROM harness_jobs WHERE id=?", (job["id"],)).fetchone()
                if row and row["status"] == "running":
                    host.store.finish_job(job, {
                        "error": type(error).__name__, "source": "worker_bookkeeping_failure",
                        "review_required": job["kind"] == "compose",
                        "review_status": "render_failed",
                    }, error=type(error).__name__, _db=db)
            return True
        if await retry.run("job_receipt", finish):
            return True
        await asyncio.sleep(0.2)
    return False
