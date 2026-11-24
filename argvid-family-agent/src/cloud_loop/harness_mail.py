"""Bounded peer requests, durable replies and version-bound asset transfers."""

from __future__ import annotations

import base64
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .contracts import canonical, digest
from .harness_contracts import PeerMessage
from .media import check_clip, file_hash
from .store import Conflict


class PeerBridge:
    def __init__(self, host):
        self.host, self.store = host, host.store

    def now(self):
        return datetime.fromtimestamp(self.store.clock(), timezone.utc)

    @staticmethod
    def put(db, message):
        existing = db.execute(
            "SELECT direction FROM peer_mail WHERE id=?", (message.id,)
        ).fetchone()
        if existing:
            if existing["direction"] != "out":
                raise Conflict("outbound identity collision")
            return
        db.execute(
            "INSERT INTO peer_mail(id,direction,body,status) VALUES (?,'out',?,'pending')",
            (message.id, canonical(message)),
        )

    def send(self, event, args):
        args = dict(args)
        # Consultation from a bound work turn keeps that matter's identity even
        # if the model omits the optional ID. Authorization is still checked
        # below: ordinary read-only questions never gain execution permission.
        if (
            args["kind"] in {"query", "analysis"}
            and not args.get("task_id")
            and event.get("task_id")
        ):
            args["task_id"] = event["task_id"]
        if event["read_only"] and args["kind"] == "task":
            raise Conflict("query cannot delegate execution")
        if args.get("media_recipe"):
            if args["kind"] != "task":
                raise Conflict("media recipe requires a delegated task")
            from .harness_media_contracts import MediaRecipe

            MediaRecipe.model_validate(args["media_recipe"])
        upstream = event["body"].get("peer_message") or {}
        hops = upstream.get("hops", -1) + 1
        if hops >= self.host.settings.peer_max_hops:
            raise Conflict("peer request hop limit reached")
        task = (
            self.store.business_task(args["task_id"], event["principal"])
            if args.get("task_id")
            else None
        )
        # The durable task is the authority for a concrete media plan. A model
        # need not repeat it in the delegation tool; in particular an explicit
        # transfer_result flag must not disappear when recipe is omitted.
        args = dict(args)
        if (
            args["kind"] == "task"
            and task
            and task["constraints"].get("requires_capture")
            and not args.get("operation")
        ):
            raise Conflict(
                "compound task: explicitly choose operation=capture for a capture substep or compose for the complete workflow"
            )
        if (
            args["kind"] == "task"
            and task
            and task["kind"] in {"goal", "compose"}
            and args.get("operation") == "capture"
            and task["constraints"].get("media_recipe")
            and not args.get("media_recipe")
        ):
            raise Conflict(
                "capture substep requires media_recipe selecting the intended saved segments; question text alone is not a structured plan"
            )
        if (
            args["kind"] == "task"
            and task
            and (args.get("media_recipe") or task["constraints"].get("media_recipe"))
        ):
            from .harness_media_contracts import MediaRecipe

            args["media_recipe"] = MediaRecipe.model_validate(
                args.get("media_recipe") or task["constraints"]["media_recipe"]
            ).model_dump(mode="json")
            args["operation"] = args.get("operation") or (
                task["kind"] if task["kind"] in {"capture", "compose"} else None
            )
            if args["operation"] not in {"capture", "compose"}:
                raise Conflict("long-term goal delegation requires an explicit operation")
        if (
            args["kind"] == "task"
            and not args.get("media_recipe")
            and not self.host.settings.legacy_media_compatibility
        ):
            raise Conflict(
                "0.4 delegated media work requires an explicit recipe; select this operation plan first"
            )
        if args["kind"] == "task" and (
            not task or task["status"] in {"paused", "cancelled", "completed", "expired"}
        ):
            raise Conflict("delegation requires an active authorized goal")
        if args.get("media_recipe") and task:
            parent_recipe = task["constraints"].get("media_recipe")
            if task["kind"] == "report":
                raise Conflict("report does not authorize media processing")
            if parent_recipe and args["media_recipe"] != parent_recipe:
                delegated = args["media_recipe"]
                if (
                    args["operation"] != "capture"
                    or any(x not in parent_recipe["segments"] for x in delegated["segments"])
                    or any(
                        delegated[k] != parent_recipe[k]
                        for k in ("width", "height", "fps", "audio")
                    )
                ):
                    raise Conflict("delegation may only select authorized media segments")
        if args["kind"] == "task" and task and task["kind"] == "report":
            raise Conflict("report matters cannot delegate media actions")
        continue_task = False
        if (
            args["kind"] in {"query", "analysis"}
            and task
            and not event["read_only"]
            and task["status"] not in {"paused", "cancelled", "completed", "expired"}
        ):
            with self.store.connect() as db:
                route = db.execute(
                    "SELECT source_event FROM task_routes WHERE task_id=?", (task["id"],)
                ).fetchone()
            continue_task = (
                event.get("task_id") == task["id"]
                and event["kind"]
                in {"timer", "scene.changed", "task.resumed", "task.step_completed", "peer.result"}
            ) or bool(route and route["source_event"] == event["id"])
        # Task dedupe belongs to business version, not a transient model call
        # or changing natural-language paraphrase after restart.
        identity = (
            {"task": task["id"], "version": task["version"], "peer": self.host.peer}
            if args["kind"] == "task"
            else {"event": event["id"], "args": args}
        )
        if args.get("operation") or args.get("media_recipe"):
            identity = {
                **identity,
                "operation": args.get("operation"),
                "media_recipe": args.get("media_recipe"),
                "transfer_result": args.get("transfer_result", False),
            }
        ident = "request-" + digest(identity)[:32]
        now = self.now()
        timeout = (
            self.host.profile.max_task_seconds
            if args["kind"] == "task"
            else self.host.settings.peer_timeout_seconds
        )
        deadline = now + timedelta(seconds=timeout)
        if upstream.get("expires_at"):
            deadline = min(deadline, datetime.fromisoformat(upstream["expires_at"]))
        if deadline <= now:
            raise Conflict("parent request expired")
        message = PeerMessage(
            id=ident,
            correlation_id=event["id"],
            sender=self.host.side,
            recipient=self.host.peer,
            principal=event["principal"],
            kind=args["kind"],
            text=args["question"],
            task_id=task["id"] if task else None,
            task_version=task["version"] if task else None,
            sent_at=now,
            expires_at=deadline,
            hops=hops,
            payload={
                "origin_event_id": event["id"],
                "task": task,
                "resume_authorized_task": continue_task,
                **(
                    {
                        "operation": args["operation"],
                        "media_recipe": args["media_recipe"],
                        "transfer_result": args.get("transfer_result", False),
                    }
                    if args.get("media_recipe")
                    else {}
                ),
            },
        )
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if args["kind"] == "task" or continue_task:
                current = self.store._task(db, task["id"], event["principal"])
                expected = event.get("_task_versions", {}).get(task["id"], task["version"])
                if (
                    current["version"] != expected
                    or current["version"] != task["version"]
                    or current["status"] in {"paused", "cancelled", "completed", "expired"}
                ):
                    raise Conflict("task changed before delegation commit")
            old = db.execute("SELECT status,result FROM peer_mail WHERE id=?", (ident,)).fetchone()
            if old and old["status"] in {"expired", "failed", "superseded"}:
                raise Conflict(
                    "request already ended; explicit new input or task version is required"
                )
            if old and old["result"]:
                result = PeerMessage.model_validate_json(old["result"])
                if result.phase in {"completed", "failed", "cancelled"}:
                    return {
                        "status": result.phase,
                        "request_id": ident,
                        "result": result.model_dump(mode="json"),
                    }
            self.put(db, message)
            if args["kind"] == "task" or continue_task:
                db.execute(
                    "UPDATE business_tasks SET status='waiting_peer',updated=? WHERE id=? AND version=?",
                    (self.store.clock(), task["id"], task["version"]),
                )
            self.store._audit(
                db,
                event["task_id"],
                "peer.request.queued",
                {"request_id": ident, "kind": args["kind"]},
            )
        return {
            "status": "accepted",
            "request_id": ident,
            "peer_received": bool(old and old["status"] == "sent"),
        }

    def receive(self, message):
        if message.sender != self.host.peer or message.recipient != self.host.side:
            raise Conflict("wrong peer direction")
        if message.hops >= self.host.settings.peer_max_hops:
            raise Conflict("peer hop limit exceeded")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT body FROM peer_mail WHERE id=?", (message.id,)).fetchone()
            if old:
                if old["body"] != canonical(message):
                    raise Conflict("peer identity conflict")
                return {"accepted": True, "duplicate": True}
            if message.expires_at <= self.now() or message.sent_at > self.now() + timedelta(
                seconds=30
            ):
                raise Conflict("peer message expired or future-dated")
            task_id, read_only, channel = None, message.kind != "task", "peer"
            body = {"text": message.text, "peer_message": message.model_dump(mode="json")}
            if message.kind == "control":
                return self.receive_control(db, message)
            if message.kind == "task":
                if (
                    not message.payload.get("media_recipe")
                    and not self.host.settings.legacy_media_compatibility
                ):
                    raise Conflict("legacy task envelopes are disabled for this node")
                operation = message.payload.get("operation")
                needed = (
                    {"capture": "capture", "compose": "media"}.get(operation)
                    if operation
                    else ("capture" if self.host.side == "edge" else "media")
                )
                if needed not in self.host.binding["capabilities"]:
                    raise Conflict("requested peer capability unavailable")
                parent = message.payload.get("task")
                if (
                    not parent
                    or parent.get("id") != message.task_id
                    or parent.get("version") != message.task_version
                    or parent.get("principal") != message.principal
                    or parent.get("status") in {"paused", "cancelled", "completed", "expired"}
                ):
                    raise Conflict("missing or invalid task authority")
                constraints = parent.get("constraints", {})
                if (
                    constraints.get("flash_allowed") is not False
                    or constraints.get("requires_user_presence") is not True
                ):
                    raise Conflict("unsupported parent constraints")
            if message.kind == "result":
                request = db.execute(
                    "SELECT body,result,status FROM peer_mail WHERE id=? AND direction='out'",
                    (message.correlation_id,),
                ).fetchone()
                if not request:
                    raise Conflict("unsolicited peer result")
                origin = PeerMessage.model_validate_json(request["body"])
                if origin.principal != message.principal:
                    raise Conflict("reply principal mismatch")
                if (
                    request["status"] in {"expired", "failed", "superseded"}
                    or origin.expires_at <= self.now()
                ):
                    raise Conflict("original peer request has ended")
                if message.expires_at > origin.expires_at:
                    raise Conflict("reply cannot extend the original request deadline")
                if message.phase == "request":
                    raise Conflict("result requires explicit completion phase")
                previous = (
                    PeerMessage.model_validate_json(request["result"])
                    if request["result"]
                    else None
                )
                if previous and previous.phase in {"completed", "failed", "cancelled"}:
                    if message.phase == "accepted" or (
                        message.phase == previous.phase
                        and message.payload == previous.payload
                        and message.text == previous.text
                    ):
                        db.execute(
                            "INSERT INTO peer_mail(id,direction,body,status) VALUES (?,'in',?,'superseded')",
                            (message.id, canonical(message)),
                        )
                        return {"accepted": True, "duplicate": False, "superseded": True}
                    raise Conflict("request already has a terminal result")
                task_id = origin.task_id
                current = self.store._task(db, task_id, message.principal) if task_id else None
                stale = bool(
                    current
                    and (
                        current["version"] != origin.task_version
                        or current["status"] in {"paused", "cancelled", "completed", "expired"}
                    )
                )
                if origin.kind == "task" and message.phase == "completed" and not stale:
                    result = message.payload.get("result") or {}
                    if result.get("verified") is not True:
                        raise Conflict("task completion lacks verified result")
                    if origin.payload.get("media_recipe"):
                        from .harness_media_transfer import validate_manifest

                        _, facts = validate_manifest(result.get("media_manifest"), origin)
                        if (result.get("asset_id"), result.get("sha256")) != (
                            facts["asset_id"],
                            facts["sha256"],
                        ):
                            raise Conflict("result identity differs from manifest")
                    if origin.payload.get("transfer_result") or (
                        not origin.payload.get("media_recipe") and self.host.side == "cloud"
                    ):
                        asset = db.execute(
                            """SELECT sha256 FROM transferred_assets WHERE request_id=? AND asset_id=?
                            AND principal=? AND task_id=? AND task_version=?""",
                            (
                                origin.id,
                                result.get("asset_id"),
                                origin.principal,
                                origin.task_id,
                                origin.task_version,
                            ),
                        ).fetchone()
                        if not asset or asset["sha256"] != result.get("sha256"):
                            raise Conflict("capture bytes must be verified before completion")
                # Receipt/progress can update state but never starts another
                # planning turn or masquerades as the final task result.
                db.execute(
                    "UPDATE peer_mail SET result=?,status='sent' WHERE id=?",
                    (canonical(message), origin.id),
                )
                db.execute(
                    "INSERT INTO peer_mail(id,direction,body,status) VALUES (?,'in',?,'received')",
                    (message.id, canonical(message)),
                )
                if message.phase == "accepted":
                    if current and not stale and origin.kind == "task":
                        db.execute(
                            "UPDATE business_tasks SET status='waiting_peer',updated=? WHERE id=?",
                            (self.store.clock(), current["id"]),
                        )
                    return {"accepted": True, "duplicate": False}
                route = db.execute(
                    "SELECT channel,session_key,task_id FROM harness_events WHERE id=? AND principal=?",
                    (origin.payload["origin_event_id"], origin.principal),
                ).fetchone()
                if not route:
                    raise Conflict("original reply route missing")
                channel = route["channel"]
                read_only = origin.kind != "task" or stale or message.phase != "completed"
                if origin.payload.get("resume_authorized_task") and current and not stale:
                    read_only = message.phase != "completed"
                    db.execute(
                        "UPDATE business_tasks SET status=?,updated=? WHERE id=?",
                        (
                            "ready" if message.phase == "completed" else "waiting",
                            self.store.clock(),
                            current["id"],
                        ),
                    )
                if current and not stale and origin.kind == "task":
                    remote_deliverable = message.phase == "completed" and (
                        (
                            origin.payload.get("operation") == current["kind"]
                            and current["kind"] in {"capture", "compose"}
                            and origin.payload.get("media_recipe")
                            == current["constraints"].get("media_recipe")
                        )
                        or (
                            not origin.payload.get("media_recipe")
                            and self.host.side == "edge"
                            and current["kind"] == "compose"
                        )
                    )
                    stored_result = message.payload.get("result") if remote_deliverable else None
                    if message.phase == "completed" and (message.payload.get("result") or {}).get(
                        "media_manifest"
                    ):
                        stored_result = {
                            **message.payload["result"],
                            "operation": origin.payload.get("operation"),
                            "asset_side": self.host.peer,
                            "stage": "completed"
                            if remote_deliverable
                            else "period_complete"
                            if origin.payload.get("operation") == "compose"
                            else "capture_step",
                        }
                        if origin.payload.get("transfer_result"):
                            local_id = (
                                "received-"
                                + digest(
                                    {"request": origin.id, "asset": stored_result["asset_id"]}
                                )[:32]
                            )
                            local = db.execute(
                                "SELECT info FROM assets WHERE id=?", (local_id,)
                            ).fetchone()
                            if not local:
                                raise Conflict("verified local asset reference missing")
                            stored_result = {
                                **stored_result,
                                "remote_asset_id": stored_result["asset_id"],
                                "asset_id": local_id,
                                "asset_side": self.host.side,
                                "media_manifest": json.loads(local["info"]),
                            }
                    db.execute(
                        "UPDATE business_tasks SET status=?,result=COALESCE(?,result),updated=? WHERE id=?",
                        (
                            "completed"
                            if remote_deliverable
                            else "ready"
                            if message.phase == "completed"
                            else "waiting",
                            canonical(stored_result) if stored_result is not None else None,
                            self.store.clock(),
                            current["id"],
                        ),
                    )
                    if remote_deliverable:
                        read_only = True
                body = {
                    "text": (
                        "对方工具结果已验证落库，继续本侧已授权目标；不要重复委托。"
                        if not read_only
                        else "对方回复已保存，请回答原问题或说明失败；不要再次问同一问题。"
                    ),
                    "peer_message": message.model_dump(mode="json"),
                    "original_question": origin.text,
                    "origin_event_id": origin.payload["origin_event_id"],
                    "stale_for_execution": stale,
                    "task_version": origin.task_version,
                }
                if origin.kind in {"query", "analysis"} and not origin.payload.get(
                    "resume_authorized_task"
                ):
                    # The subject of a question is not the destination conversation.
                    # A asks about B: answer A, without scheduling B's work.
                    body["consulted_task_id"] = origin.task_id
                    task_id = route["task_id"]
                    if route["session_key"]:
                        body["conversation_id"] = route["session_key"]
            else:
                db.execute(
                    "INSERT INTO peer_mail(id,direction,body,status) VALUES (?,'in',?,'received')",
                    (message.id, canonical(message)),
                )
            self.store.submit(
                message.id,
                message.principal,
                channel,
                "peer." + message.kind,
                body,
                task_id=task_id,
                read_only=read_only,
                _db=db,
            )
        return {"accepted": True, "duplicate": False}

    def queue_reply(self, db, source, *, text, phase, result=None, error=None, suffix="final"):
        if source.expires_at <= self.now():
            self.store._audit(
                db, None, "peer.reply.expired", {"request_id": source.id, "phase": phase}
            )
            return
        reply = PeerMessage(
            id="reply-" + digest({"request": source.id, "suffix": suffix})[:32],
            correlation_id=source.id,
            sender=self.host.side,
            recipient=self.host.peer,
            principal=source.principal,
            kind="result",
            phase=phase,
            text=text[:8000],
            sent_at=self.now(),
            expires_at=source.expires_at,
            hops=source.hops,
            payload={"result": result, "error": error, "reply_to_event": source.correlation_id},
        )
        self.put(db, reply)

    def reply_event(self, event, result, *, _db=None):
        # A query can delegate one bounded subquery. Its eventual answer is
        # routed through the original inbound request, not the active session.
        source = event["body"].get("peer_message")
        if event["kind"] == "peer.result" and event["channel"] == "peer":
            original = self.store.event(event["body"]["origin_event_id"], event["principal"])
            source = original["body"].get("peer_message")
        elif event["kind"] not in {"peer.query", "peer.analysis", "peer.task"}:
            return
        if not source:
            return
        source = PeerMessage.model_validate(source)
        with nullcontext(_db) if _db is not None else self.store.connect() as db:
            if source.kind == "task":
                binding = db.execute(
                    "SELECT task_id FROM peer_task_bindings WHERE request_id=?", (source.id,)
                ).fetchone()
                if binding and not result.get("error"):
                    self.queue_reply(
                        db,
                        source,
                        text="对侧工作事项已保存，按实际能力与执行条件推进。",
                        phase="accepted",
                        result={"task_id": binding["task_id"]},
                        suffix="accepted",
                    )
                    return
                self.queue_reply(
                    db,
                    source,
                    text=result.get("text") or "无法接受此任务。",
                    phase="failed",
                    error=result.get("error") or "TaskNotAccepted",
                )
            elif not result.get("pending_receipt"):
                self.queue_reply(
                    db,
                    source,
                    text=result.get("text") or "未取得答复。",
                    phase="failed" if result.get("error") else "completed",
                    error=result.get("error"),
                    result={
                        "observation_sources": result["observation_sources"],
                        "observation_checked_at": result.get("observation_checked_at"),
                    }
                    if result.get("observation_sources")
                    else None,
                )

    def job_finished(self, db, job, result, error):
        route = db.execute(
            "SELECT peer_request FROM task_routes WHERE task_id=?", (job["task_id"],)
        ).fetchone()
        if route and route["peer_request"]:
            source = PeerMessage.model_validate_json(route["peer_request"])
            if (
                not error
                and job["kind"] == "capture"
                and source.payload.get("operation") == "compose"
            ):
                return
            self.queue_reply(
                db,
                source,
                text="工具作业失败。" if error else "工具已完成，真实素材已核验；采集为模拟回放。",
                phase="failed" if error else "completed",
                result=result,
                error=error,
            )

    def queue_controls(self, db, task, action, *, notify_parent=True):
        if notify_parent and action in {"pause", "cancel", "resume"}:
            route = db.execute(
                "SELECT peer_request FROM task_routes WHERE task_id=?", (task["id"],)
            ).fetchone()
            if route and route["peer_request"]:
                source = PeerMessage.model_validate_json(route["peer_request"])
                self.queue_reply(
                    db,
                    source,
                    text={
                        "pause": "现场用户已暂停任务。",
                        "cancel": "现场用户已取消任务。",
                        "resume": "现场用户已恢复任务。",
                    }[action],
                    phase={"pause": "paused", "cancel": "cancelled", "resume": "accepted"}[action],
                    result={
                        "task_id": task["id"],
                        "status": task["status"],
                        "source": "local_user_control",
                    },
                    suffix="control-" + str(task["version"]),
                )
        rows = db.execute("SELECT body,result FROM peer_mail WHERE direction='out'").fetchall()
        for row in rows:
            origin = PeerMessage.model_validate_json(row["body"])
            if (
                origin.kind != "task"
                or origin.task_id != task["id"]
                or origin.principal != task["principal"]
            ):
                continue
            if origin.task_version >= task["version"]:
                continue
            if origin.expires_at <= self.now():
                continue
            if row["result"] and PeerMessage.model_validate_json(row["result"]).phase in {
                "completed",
                "cancelled",
                "failed",
            }:
                continue
            forwarded_action = "cancel" if action == "resume" else action
            message = PeerMessage(
                id="control-"
                + digest({"request": origin.id, "version": task["version"], "action": action})[:32],
                correlation_id=origin.id,
                sender=self.host.side,
                recipient=self.host.peer,
                principal=task["principal"],
                kind="control",
                text=forwarded_action,
                sent_at=self.now(),
                expires_at=self.now() + timedelta(seconds=self.host.profile.max_task_seconds),
                task_id=origin.task_id,
                task_version=origin.task_version,
                payload={"action": forwarded_action, "parent_version": task["version"]},
            )
            self.put(db, message)
            db.execute(
                "UPDATE peer_mail SET status='superseded' WHERE id=? AND status IN ('pending','sent')",
                (origin.id,),
            )

    def receive_control(self, db, message):
        inbound = db.execute(
            "SELECT body FROM peer_mail WHERE id=? AND direction='in'", (message.correlation_id,)
        ).fetchone()
        if not inbound:
            raise Conflict("control references unknown delegated request")
        original = PeerMessage.model_validate_json(inbound["body"])
        if (
            original.kind != "task"
            or original.principal != message.principal
            or original.task_id != message.task_id
            or original.task_version != message.task_version
        ):
            raise Conflict("control authority mismatch")
        action = message.payload.get("action")
        if action not in {"pause", "cancel"}:
            raise Conflict("remote resume requires a new authorized task version")
        binding = db.execute(
            "SELECT task_id FROM peer_task_bindings WHERE request_id=?", (original.id,)
        ).fetchone()
        if binding:
            task = self.store._task(db, binding["task_id"], message.principal)
            if task["status"] not in {"cancelled", "completed", "expired"}:
                changed = self.store.control_business_task(
                    message.principal,
                    task["id"],
                    action,
                    task["version"],
                    source_event=message.id,
                    _db=db,
                )
                self.queue_controls(db, changed, action, notify_parent=False)
        # Persist even if control beats the model creating the task. The save
        # boundary checks this tombstone before allowing task creation.
        db.execute(
            "INSERT INTO peer_mail(id,direction,body,status) VALUES (?,'in',?,'received')",
            (message.id, canonical(message)),
        )
        db.execute(
            "UPDATE harness_events SET status='superseded' WHERE id=? AND status='queued'",
            (original.id,),
        )
        return {"accepted": True, "duplicate": False, "control": action}

    def transfer(self, upload):
        if self.host.side != "cloud":
            raise Conflict("capture uploads belong to cloud")
        with self.store.connect() as db:
            request = db.execute(
                "SELECT body FROM peer_mail WHERE id=? AND direction='out'", (upload.request_id,)
            ).fetchone()
        if not request:
            raise Conflict("upload lacks originating request")
        original = PeerMessage.model_validate_json(request["body"])
        if original.kind != "task" or original.principal != upload.principal:
            raise Conflict("upload authority mismatch")
        task = self.store.business_task(original.task_id, upload.principal)
        if (
            task["version"] != original.task_version
            or task["status"] in {"paused", "cancelled", "completed", "expired"}
            or original.expires_at <= self.now()
        ):
            raise Conflict("upload task expired or superseded")
        package = upload.package
        content = base64.b64decode(upload.content_base64, validate=True)
        if (
            len(content) != package.asset.size_bytes
            or __import__("hashlib").sha256(content).hexdigest() != package.asset.sha256
        ):
            raise Conflict("upload checksum or size mismatch")
        # Scope IDs to the request rather than trusting remote filenames/paths.
        asset_id = (
            "received-" + digest({"request": original.id, "asset": package.asset.asset_id})[:32]
        )
        target = self.store.root / "assets" / f"{asset_id}.mp4"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".incoming.mp4")
        try:
            temporary.write_bytes(content)
            facts = check_clip(temporary, 6)
            for key in ("sha256", "width", "height", "frames", "duration_ms"):
                if facts[key] != getattr(package.asset, key):
                    raise Conflict("upload media facts mismatch")
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                fresh = self.store._task(db, task["id"], upload.principal)
                if fresh["version"] != original.task_version or fresh["status"] in {
                    "paused",
                    "cancelled",
                    "completed",
                    "expired",
                }:
                    raise Conflict("upload superseded before commit")
                existing = db.execute(
                    "SELECT * FROM transferred_assets WHERE request_id=? AND asset_id=?",
                    (original.id, package.asset.asset_id),
                ).fetchone()
                if existing:
                    if (
                        existing["sha256"] != package.asset.sha256
                        or not target.is_file()
                        or file_hash(target) != package.asset.sha256
                    ):
                        raise Conflict("registered upload changed")
                    return {"verified": True, "duplicate": True, "asset_id": asset_id}
                temporary.replace(target)
                info = package.model_dump(mode="json")
                info["asset"]["asset_id"] = asset_id
                info["asset"]["uri"] = f"cloud-asset://{asset_id}"
                info["remote_asset_id"] = package.asset.asset_id
                db.execute(
                    "INSERT INTO assets VALUES (?,?,?,?,?)",
                    (asset_id, task["id"], str(target), canonical(info), "closeup"),
                )
                db.execute(
                    "INSERT INTO transferred_assets VALUES (?,?,?,?,?,?)",
                    (
                        original.id,
                        package.asset.asset_id,
                        task["id"],
                        task["version"],
                        upload.principal,
                        package.asset.sha256,
                    ),
                )
                self.store._audit(
                    db,
                    task["id"],
                    "asset.transferred",
                    {
                        "request_id": original.id,
                        "asset_id": asset_id,
                        "remote_asset_id": package.asset.asset_id,
                        "sha256": package.asset.sha256,
                        "verified": True,
                    },
                )
            return {"verified": True, "duplicate": False, "asset_id": asset_id}
        finally:
            temporary.unlink(missing_ok=True)

    def delivery_failed(self, db, message, status):
        db.execute("UPDATE peer_mail SET status=? WHERE id=?", (status, message.id))
        if message.kind not in {"query", "analysis", "task"}:
            return
        if (
            message.kind == "task" or message.payload.get("resume_authorized_task")
        ) and message.task_id:
            db.execute(
                "UPDATE business_tasks SET status='waiting',updated=? WHERE id=? AND version=? AND status='waiting_peer'",
                (self.store.clock(), message.task_id, message.task_version),
            )
        origin = db.execute(
            "SELECT channel FROM harness_events WHERE id=? AND principal=?",
            (message.payload["origin_event_id"], message.principal),
        ).fetchone()
        if origin:
            self.store.submit(
                "failure-" + message.id,
                message.principal,
                origin["channel"],
                "peer.failed",
                {
                    "text": "对方请求未能完成，请说明失败，不自动重试。",
                    "request_id": message.id,
                    "reason": status,
                    "origin_event_id": message.payload["origin_event_id"],
                },
                task_id=message.task_id,
                read_only=True,
                _db=db,
            )

    async def deliver_once(self):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM peer_mail WHERE direction='out' AND status IN ('pending','sent') ORDER BY rowid"
            ).fetchall()
        for row in rows:
            message = PeerMessage.model_validate_json(row["body"])
            final = PeerMessage.model_validate_json(row["result"]).phase if row["result"] else None
            if message.kind in {"query", "analysis", "task"} and final in {
                "completed",
                "failed",
                "cancelled",
            }:
                continue
            if row["status"] == "sent" and message.kind in {"result", "control"}:
                continue
            if message.expires_at <= self.now():
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    self.delivery_failed(db, message, "expired")
                continue
            if row["status"] == "sent" or row["next_try"] > self.store.clock():
                continue
            if message.kind == "task":
                task = self.store.business_task(message.task_id, message.principal)
                if task["version"] != message.task_version or task["status"] in {
                    "paused",
                    "cancelled",
                    "completed",
                    "expired",
                }:
                    with self.store.connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        self.delivery_failed(db, message, "superseded")
                    continue
            try:
                result = message.payload.get("result") or {}
                if (
                    message.kind == "result"
                    and message.phase == "completed"
                    and result.get("media_manifest")
                ):
                    with self.store.connect() as db:
                        origin = db.execute(
                            "SELECT body FROM peer_mail WHERE id=? AND direction='in'",
                            (message.correlation_id,),
                        ).fetchone()
                    request = PeerMessage.model_validate_json(origin["body"]) if origin else None
                    if request and request.payload.get("transfer_result"):
                        manifest = result["media_manifest"]
                        asset = self.store.asset(manifest["asset"]["asset_id"])
                        content = await __import__("asyncio").to_thread(
                            Path(asset["path"]).read_bytes
                        )
                        response = await self.host.client.post(
                            "/harness/media/receive",
                            json={
                                "request_id": message.correlation_id,
                                "principal": message.principal,
                                "manifest": manifest,
                                "content_base64": base64.b64encode(content).decode(),
                            },
                            timeout=60,
                        )
                        response.raise_for_status()
                if (
                    message.kind == "result"
                    and message.phase == "completed"
                    and result.get("package")
                    and self.host.side == "edge"
                ):
                    package = result["package"]
                    asset = self.store.asset(package["asset"]["asset_id"])
                    content = await __import__("asyncio").to_thread(Path(asset["path"]).read_bytes)
                    response = await self.host.client.post(
                        "/harness/assets/receive",
                        json={
                            "request_id": message.correlation_id,
                            "principal": message.principal,
                            "package": package,
                            "content_base64": base64.b64encode(content).decode(),
                        },
                        timeout=60,
                    )
                    response.raise_for_status()
                response = await self.host.client.post(
                    "/harness/peer", json=message.model_dump(mode="json")
                )
                response.raise_for_status()
                with self.store.connect() as db:
                    db.execute("UPDATE peer_mail SET status='sent' WHERE id=?", (message.id,))
            except (httpx.HTTPError, OSError) as exc:
                attempts = row["attempts"] + 1
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    db.execute(
                        "UPDATE peer_mail SET attempts=?,next_try=? WHERE id=?",
                        (attempts, self.store.clock() + min(30, 2**attempts), message.id),
                    )
                    permanent = isinstance(
                        exc, httpx.HTTPStatusError
                    ) and exc.response.status_code in {400, 401, 403, 404, 409, 422}
                    self.store._audit(
                        db,
                        message.task_id,
                        "peer.delivery.error",
                        {
                            "request_id": message.id,
                            "attempt": attempts,
                            "error_type": type(exc).__name__,
                            "http_status": exc.response.status_code
                            if isinstance(exc, httpx.HTTPStatusError)
                            else None,
                        },
                    )
                    if permanent or attempts >= self.host.profile.delivery_max_attempts:
                        self.delivery_failed(db, message, "failed")
