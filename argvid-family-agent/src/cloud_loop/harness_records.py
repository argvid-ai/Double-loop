"""Owner-checked record RPCs; natural-language peer work remains in PeerBridge."""

import json
from datetime import timedelta

import httpx

from .contracts import canonical, digest, utcnow
from .harness_contracts import PeerRecordMutation, PeerRecordUpdate
from .store import Conflict


class PeerRecords:
    def __init__(self, host):
        self.host, self.store = host, host.store

    def query(self, body):
        return self.store.search(body.principal, **body.query.model_dump())

    def apply(self, body):
        update = body.update
        needed = "remember" if update.resource == "memory" else "task_control"
        if needed not in self.host.binding["tools"]:
            raise Conflict("owner write capability unavailable")
        semantic = {**update.model_dump(exclude={"expected_version"}), "principal": body.principal}
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM record_mutations WHERE id=?", (body.id,)).fetchone()
            if old:
                previous = PeerRecordMutation.model_validate_json(old["request"])
                if {
                    **previous.update.model_dump(exclude={"expected_version"}),
                    "principal": previous.principal,
                } != semantic:
                    raise Conflict("mutation identity conflict")
                receipt = json.loads(old["receipt"])
                if update.resource == "memory":
                    row = db.execute(
                        "SELECT * FROM user_memory WHERE id=? AND principal=?",
                        (update.record_id, body.principal),
                    ).fetchone()
                    current = dict(row) if row else None
                else:
                    current = self.store._task(db, update.record_id, body.principal)
                return {
                    **receipt,
                    "duplicate": True,
                    "current_record": current,
                    "current_changed": current is None
                    or current["version"] != receipt["record"]["version"],
                }
            if body.expires_at <= utcnow():
                raise Conflict("mutation request expired")
            source = "rpc-" + body.id
            self.store.submit(
                source,
                body.principal,
                "peer",
                "peer.record_mutation",
                {"text": "已认证对方节点请求修改本用户对象。", "operation": update.model_dump()},
                task_id=update.record_id if update.resource == "task" else None,
                _db=db,
            )
            if update.resource == "memory":
                row = db.execute(
                    "SELECT * FROM user_memory WHERE id=? AND principal=?",
                    (update.record_id, body.principal),
                ).fetchone()
                if not row:
                    raise KeyError(update.record_id)
                value = self.store.remember(
                    body.principal,
                    row["key"],
                    update.value,
                    source,
                    memory_id=update.record_id,
                    expected_version=update.expected_version,
                    _db=db,
                )
            else:
                value = self.store.control_business_task(
                    body.principal,
                    update.record_id,
                    update.action,
                    update.expected_version,
                    source_event=source,
                    _db=db,
                )
                self.host.mail.queue_controls(db, value, update.action)
                if update.action == "resume":
                    route = db.execute(
                        "SELECT channel FROM task_routes WHERE task_id=?", (value["id"],)
                    ).fetchone()
                    self.store.submit(
                        "resume-" + body.id,
                        body.principal,
                        route["channel"] if route else "system",
                        "task.resumed",
                        {
                            "text": "用户通过另一侧请求恢复本任务，检查最新状态后继续。",
                            "origin_event_id": source,
                            "task_version": value["version"],
                        },
                        task_id=value["id"],
                        _db=db,
                    )
            receipt = {
                "status": "confirmed",
                "operation_id": body.id,
                "authority": self.host.side,
                "resource": update.resource,
                "record": value,
                "duplicate": False,
            }
            db.execute(
                "INSERT INTO record_mutations VALUES (?,?,?,?,?)",
                (body.id, body.principal, canonical(body), canonical(receipt), self.store.clock()),
            )
            db.execute(
                "UPDATE harness_events SET status='done',reply=?,finished=? WHERE id=?",
                (
                    canonical(
                        {
                            "text": "对象变更已由权威侧事务确认。",
                            "receipt": receipt,
                            "model_invoked": False,
                        }
                    ),
                    self.store.clock(),
                    source,
                ),
            )
            self.store._audit(
                db,
                update.record_id if update.resource == "task" else None,
                "record.mutation.confirmed",
                {
                    "event_id": source,
                    "operation_id": body.id,
                    "principal": body.principal,
                    "resource": update.resource,
                    "record_id": value["id"],
                    "version": value["version"],
                },
            )
            return receipt

    async def remote_query(self, event, args):
        response = await self.host.client.post(
            "/harness/records/query", json={"principal": event["principal"], "query": args}
        )
        response.raise_for_status()
        result = response.json()
        self.store.audit(
            event["task_id"],
            "peer.records.read",
            {
                "event_id": event["id"],
                "side": self.host.peer,
                "query": args,
                "sources": [r["id"] for r in result["records"]],
            },
        )
        return result

    async def remote_update(self, event, args):
        if event["read_only"] or event["kind"] not in {"user", "voice"}:
            raise Conflict("remote record changes require explicit local user input")
        update = PeerRecordUpdate.model_validate(args)
        ident = (
            "mutation-"
            + digest(
                {
                    "event": event["id"],
                    "principal": event["principal"],
                    "update": update.model_dump(exclude={"expected_version"}),
                }
            )[:32]
        )
        request = PeerRecordMutation(
            id=ident,
            principal=event["principal"],
            update=update,
            expires_at=utcnow() + timedelta(seconds=30),
        )
        self.store.audit(
            event["task_id"],
            "peer.record_mutation.started",
            {
                "event_id": event["id"],
                "operation_id": ident,
                "side": self.host.peer,
                "update": update.model_dump(),
            },
        )
        try:
            response = await self.host.client.post(
                "/harness/records/update", json=request.model_dump(mode="json")
            )
            response.raise_for_status()
            result = response.json()
            if (
                result.get("status") != "confirmed"
                or result.get("operation_id") != ident
                or result.get("authority") != self.host.peer
                or result.get("resource") != update.resource
                or result.get("record", {}).get("id") != update.record_id
            ):
                raise ValueError("invalid owner confirmation")
        except httpx.HTTPStatusError as exc:
            result = {
                "status": "rejected"
                if exc.response.status_code in {400, 401, 403, 404, 409, 422}
                else "unconfirmed",
                "operation_id": ident,
                "http_status": exc.response.status_code,
            }
        except (httpx.HTTPError, ValueError):
            result = {"status": "unconfirmed", "operation_id": ident}
        self.store.audit(
            event["task_id"], "peer.record_mutation.result", {"event_id": event["id"], **result}
        )
        return result
