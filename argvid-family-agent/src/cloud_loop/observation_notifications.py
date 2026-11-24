"""Deliver existing execution events through the existing peer network loop.

No status sweep or new monitor process: the durable audit journal is the source.
Hints only wake an existing cloud task projection; they never create user work.
"""

import asyncio

import httpx
from pydantic import Field

from .contracts import Contract, digest, canonical
from .observation_contracts import Reference
from .store import Conflict


class ObservationUpdates(Contract):
    request_id: Reference
    task_ids: tuple[Reference, ...] = Field(default=(), max_length=32)
    health_changed: bool = False


class ObservationNotifications:
    kinds = (
        "observation.started",
        "observation.failed",
        "observation.resources.closed",
        "observation.analysis.unavailable",
        "observation.analysis.control",
        "runtime.health.changed",
    )

    def __init__(self, bridge):
        self.bridge, self.store = bridge, bridge.store
        self.cursor = self.store.meta("observation_event_delivery_cursor", 0)
        self.next_delivery = 0
        self.failures = 0
        self.lock = asyncio.Lock()

    def batch(self):
        with self.store.connect() as db:
            upper = db.execute("SELECT COALESCE(MAX(seq),0) FROM audit").fetchone()[0]
            rows = db.execute(
                "SELECT seq,task_id,kind FROM audit WHERE seq>? AND seq<=? AND kind IN ("
                + ",".join("?" for _ in self.kinds)
                + ") ORDER BY seq LIMIT 32",
                (self.cursor, upper, *self.kinds),
            ).fetchall()
        through = rows[-1]["seq"] if len(rows) == 32 else upper
        if not rows:
            self.cursor = through  # no write for an empty delivery queue
            return None, through
        task_ids = tuple(dict.fromkeys(r["task_id"] for r in rows if r["task_id"]))
        # The store path distinguishes independent installations reusing seq numbers.
        request_id = (
            "observation-update-" + digest([str(self.store.path), self.cursor, through])[:32]
        )
        return ObservationUpdates(
            request_id=request_id,
            task_ids=task_ids,
            health_changed=any(r["kind"] == "runtime.health.changed" for r in rows),
        ), through

    async def deliver_once(self):
        if self.bridge.host.side != "edge" or self.store.clock() < self.next_delivery:
            return False
        async with self.lock:
            self.next_delivery = self.store.clock() + 1
            body, through = await asyncio.to_thread(self.batch)
            if body is None:
                return False
            try:
                response = await self.bridge.host.client.post(
                    "/harness/observation/updates", json=body.model_dump(mode="json")
                )
                response.raise_for_status()
                reply = response.json()
                if reply.get("accepted") is not True or reply.get("request_id") != body.request_id:
                    raise ValueError("invalid execution event acknowledgement")
            except (httpx.HTTPError, ValueError):
                self.failures += 1
                self.next_delivery = self.store.clock() + min(30, 2 ** min(self.failures, 5))
                return False
            await asyncio.to_thread(
                self.store.set_meta, "observation_event_delivery_cursor", through
            )
            self.cursor, self.failures = through, 0
            return True

    def receive(self, body):
        if self.bridge.host.side != "cloud":
            raise Conflict("execution updates belong to the cloud")
        wire = canonical(body)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT body FROM inbox WHERE id=?", (body.request_id,)).fetchone()
            if old:
                if old[0] != wire:
                    raise Conflict("execution update identity changed")
                return {"request_id": body.request_id, "accepted": True, "duplicate": True}
            for task_id in body.task_ids:
                db.execute(
                    "UPDATE observation_remote_tasks SET next_poll=0 WHERE principal='local-user' AND task_id=? AND active=1",
                    (task_id,),
                )
            db.execute("INSERT INTO inbox VALUES (?,?)", (body.request_id, wire))
        if body.health_changed:
            self.bridge.next_health_poll = 0
        return {"request_id": body.request_id, "accepted": True}
