"""Bounded, passive probe evidence. This module never starts or stops a model."""

import asyncio
import math
import os
import resource
import shutil
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

from .contracts import canonical
from .observation_process import birth


def linux_values(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        fields = line.replace(":", " ").split()
        if len(fields) >= 2 and fields[1].isdigit():
            values[fields[0]] = int(fields[1]) * (1024 if fields[2:] == ["kB"] else 1)
    return values


def linux_process(pid, expected_birth, proc=Path("/proc")):
    def identity():
        fields = (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
        return fields, (proc / "sys/kernel/random/boot_id").read_text().strip() + ":" + fields[19]

    try:
        fields, before = identity()
        if not expected_birth or before != expected_birth:
            return {"status": "identity_changed"}
        status = linux_values(proc / str(pid) / "status")
        after_fields, after = identity()
        if before != after:
            return {"status": "identity_changed"}
        return {
            "status": "measured",
            "pid": pid,
            "rss_bytes": status.get("VmRSS"),
            "peak_rss_bytes": status.get("VmHWM"),
            "swap_bytes": status.get("VmSwap"),
            "cpu_seconds": (int(after_fields[11]) + int(after_fields[12]))
            / os.sysconf("SC_CLK_TCK"),
        }
    except (OSError, ValueError, IndexError):
        return {"status": "unavailable", "pid": pid}


def collect_resources(root, native, proc=Path("/proc")):
    """Only the probe process and its registered native PID; no process search.

    System memory/swap includes other applications. Child encoders are reflected
    in system totals, not in a falsely named whole-process-tree RSS sum.
    """
    started = time.monotonic()
    result = {
        "platform": sys.platform,
        "logical_cpus": os.cpu_count(),
        "load_average": list(os.getloadavg()),
        "system": {"scope": "whole_device"},
        "native": {"status": "not_started" if native is None else "unavailable"},
    }
    try:
        if sys.platform.startswith("linux"):
            mem = linux_values(proc / "meminfo")
            vm = linux_values(proc / "vmstat")
            result["system"].update(
                memory_available_bytes=mem.get("MemAvailable"),
                swap_used_bytes=mem["SwapTotal"] - mem["SwapFree"],
                swap_in_pages=vm.get("pswpin"),
                swap_out_pages=vm.get("pswpout"),
            )
            result["host"] = linux_process(os.getpid(), birth(os.getpid()), proc)
            if native is not None:
                result["native"] = linux_process(native["pid"], native.get("birth"), proc)
        else:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            ps = subprocess.run(
                ["ps", "-p", str(os.getpid()), "-o", "rss="],
                capture_output=True,
                text=True,
                timeout=2,
                check=True,
            )
            result["host"] = {
                "status": "measured",
                "pid": os.getpid(),
                "rss_bytes": int(ps.stdout.strip()) * 1024,
                "peak_rss_bytes": int(usage.ru_maxrss) * (1 if sys.platform == "darwin" else 1024),
                "cpu_seconds": usage.ru_utime + usage.ru_stime,
                "swap_bytes": None,
            }
            result["system"]["unavailable"] = (
                "system swap/memory counters require Linux /proc; not inferred on this host"
            )
            if native is not None:
                result["native"] = {"status": "unsupported_platform"}
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        result["error_type"] = type(exc).__name__
    try:
        disk = shutil.disk_usage(root)
        result["disk"] = {
            "scope": "filesystem_containing_probe_state",
            "total_bytes": disk.total,
            "free_bytes": disk.free,
        }
    except OSError:
        result["disk"] = {"status": "unavailable"}
    result["collection_seconds"] = time.monotonic() - started
    return result


def reserved_unsaved_ms(store, task_id):
    with store.connect() as db:
        row = db.execute(
            """SELECT COALESCE(SUM(json_extract(body,'$.end_ms')-json_extract(body,'$.start_ms')),0)
            FROM observation_exports WHERE task_id=? AND status!='saved' """,
            (task_id,),
        ).fetchone()
    return row[0]


class TrendBins:
    """One bounded accumulator per five-minute segment and phase, no raw cache."""

    def __init__(self):
        self.bins = {}

    def add(self, sample):
        elapsed = sample["phase_elapsed_seconds"]
        number = int(elapsed // 300)
        key = (sample["phase"], number)
        row = self.bins.setdefault(
            key,
            {
                "phase": sample["phase"],
                "start_seconds": number * 300,
                "end_seconds": (number + 1) * 300,
                "samples": 0,
                "first_sample_seconds": elapsed,
                "last_sample_seconds": elapsed,
                "metrics": {},
            },
        )
        row["samples"] += 1
        row["last_sample_seconds"] = elapsed
        resources = sample["resources"]
        ledger = sample.get("ledger") or {}
        values = {
            "unseen_media_ms": ledger.get("unseen_media_ms"),
            "reserved_unsaved_media_ms": sample.get("reserved_unsaved_media_ms"),
            "host_rss_bytes": resources.get("host", {}).get("rss_bytes"),
            "model_rss_bytes": resources.get("native", {}).get("rss_bytes"),
            "memory_available_bytes": resources.get("system", {}).get("memory_available_bytes"),
            "system_swap_used_bytes": resources.get("system", {}).get("swap_used_bytes"),
            "system_swap_in_pages": resources.get("system", {}).get("swap_in_pages"),
            "system_swap_out_pages": resources.get("system", {}).get("swap_out_pages"),
            "disk_free_bytes": resources.get("disk", {}).get("free_bytes"),
        }
        for name, value in values.items():
            if type(value) not in {int, float} or not math.isfinite(value):
                continue
            item = row["metrics"].setdefault(
                name, {"samples": 0, "first": value, "last": value, "min": value, "max": value}
            )
            item.update(
                samples=item["samples"] + 1,
                last=value,
                min=min(item["min"], value),
                max=max(item["max"], value),
            )

    def report(self):
        return [
            {
                **row,
                "observed_span_seconds": row["last_sample_seconds"] - row["first_sample_seconds"],
                "metrics": {
                    name: {**value, "first_to_last_delta": value["last"] - value["first"]}
                    for name, value in row["metrics"].items()
                },
            }
            for _, row in sorted(self.bins.items())
        ]


class ProbeMeasurements:
    def __init__(
        self,
        bridge,
        task_id,
        output,
        *,
        interval=5,
        max_bytes=64 * 1024**2,
        collector=collect_resources,
    ):
        if (
            type(interval) not in {float, int}
            or not math.isfinite(interval)
            or not 0.05 <= interval <= 60
        ):
            raise ValueError("invalid measurement interval")
        if type(max_bytes) is not int or max_bytes < 1024:
            raise ValueError("invalid measurement evidence budget")
        self.bridge, self.task_id, self.output = bridge, task_id, Path(output)
        self.interval, self.max_bytes, self.collector = interval, max_bytes, collector
        self.phase = "startup"
        self.phase_started = self.started = time.monotonic()
        self.stop = asyncio.Event()
        self.wake = asyncio.Event()
        self.task = None
        self.bytes = 0
        self.samples = 0
        self.trends = TrendBins()
        self.max_gap = 0.0
        self.last_sample = None
        self.errors = 0
        self.resource_unknown_samples = 0

    def set_phase(self, value):
        if value != self.phase:
            self.phase = value
            self.phase_started = time.monotonic()
            self.wake.set()

    def native_record(self):
        owner = self.bridge.installation.gateway.owned.get(self.task_id)
        process = getattr(getattr(owner, "runner", None), "process", None)
        if process is None or process.released:
            return None
        record = process._read()
        if not record or record.get("status") == "stopped" or not record.get("pid"):
            return None
        return {key: record.get(key) for key in ("pid", "birth", "instance")}

    async def capture(self, stream):
        now = time.monotonic()
        native = self.native_record()
        phase = self.phase
        try:
            state = self.bridge.installation.gateway.status("local-user", self.task_id)["task"]
        except KeyError:
            state = None
        ledger = state["ledger"] if state else {}
        # Constant-size counters only; full media history remains in SQLite/audit.
        counts = {
            key: ledger.get(key)
            for key in (
                "batch_count",
                "seen_count",
                "unseen_count",
                "unseen_media_ms",
                "unresolved_gap_count",
            )
        }
        for key in ("latest_seen", "latest_ingested", "oldest_unseen"):
            counts[key] = ledger.get(key)
        pending = reserved_unsaved_ms(self.bridge.store, self.task_id)
        sample = {
            "at": self.bridge.store.clock(),
            "elapsed_seconds": now - self.started,
            "phase": phase,
            "phase_elapsed_seconds": now - self.phase_started,
            "status": state["status"] if state else "not_configured",
            "ledger": counts,
            "reserved_unsaved_media_ms": pending,
        }
        worker = asyncio.create_task(
            asyncio.to_thread(self.collector, self.bridge.store.root, native)
        )
        try:
            resources = await asyncio.shield(worker)
        except asyncio.CancelledError:
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
        native_stable = self.native_record() == native
        if not native_stable:
            resources["native"] = {"status": "identity_changed_during_sample"}
        sample["resources"] = resources
        data = (canonical(sample) + "\n").encode()
        if self.bytes + len(data) > self.max_bytes:
            raise RuntimeError(
                "measurement evidence budget exhausted; stop rather than silently drop samples"
            )
        stream.write(data)
        stream.flush()
        self.bytes += len(data)
        self.samples += 1
        self.trends.add(sample)
        if self.last_sample is not None:
            self.max_gap = max(self.max_gap, now - self.last_sample)
        self.last_sample = now
        if resources.get("error_type"):
            self.errors += 1

        def valid(values, names):
            return all(
                type(values.get(name)) in {int, float}
                and math.isfinite(values[name])
                and values[name] >= 0
                for name in names
            )

        host = resources.get("host", {})
        model = resources.get("native", {})
        process_fields = ("rss_bytes", "peak_rss_bytes", "cpu_seconds", "swap_bytes")
        complete = (
            native_stable
            and host.get("status") == "measured"
            and valid(host, process_fields)
            and valid(
                resources.get("system", {}),
                ("memory_available_bytes", "swap_used_bytes", "swap_in_pages", "swap_out_pages"),
            )
            and valid(resources.get("disk", {}), ("free_bytes", "total_bytes"))
            and (
                native is None
                or (model.get("status") == "measured" and valid(model, process_fields))
            )
        )
        if not complete:
            self.resource_unknown_samples += 1

    async def run(self):
        with (self.output / "samples.jsonl").open("xb") as stream:
            while True:
                self.wake.clear()
                await self.capture(stream)
                if self.stop.is_set():
                    break
                try:
                    await asyncio.wait_for(self.wake.wait(), self.interval)
                except TimeoutError:
                    continue

    def start(self):
        if self.task is not None:
            raise RuntimeError("measurement already started")
        self.task = asyncio.create_task(self.run())

    def check(self):
        if self.task.done():
            self.task.result()

    async def close(self):
        self.stop.set()
        self.wake.set()
        cancelled = False
        if self.task is not None:
            while not self.task.done():
                try:
                    await asyncio.shield(self.task)
                except asyncio.CancelledError:
                    cancelled = True
            self.task.result()
        if cancelled:
            raise asyncio.CancelledError
        return self.report()

    def report(self):
        return {
            "samples": self.samples,
            "bytes": self.bytes,
            "interval_seconds": self.interval,
            "maximum_observed_gap_seconds": self.max_gap,
            "collection_errors": self.errors,
            "samples_with_unknown_resources": self.resource_unknown_samples,
            "five_minute_segments": self.trends.report(),
            "scope": "sampled host/native main processes and whole-device counters; not exhaustive peak or process-tree attribution",
            "resource_acceptance": False,
            "timing_acceptance": False,
        }
