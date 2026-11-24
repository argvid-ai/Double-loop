"""Cloud-owned discovery/transport adapter. Edge establishes every network connection."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .contracts import (
    PROTOCOL,
    CaptureArguments,
    Completion,
    FirstResult,
    Hello,
    Invocation,
    Upload,
    canonical,
    capability_fingerprint,
    tool_schema,
    utcnow,
)
from .store import Conflict


class Gateway:
    def __init__(self, store, profile):
        self.store, self.profile = store, profile
        with store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS controls (seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                "id TEXT UNIQUE NOT NULL, task_id TEXT, body TEXT NOT NULL, result TEXT)"
            )

    def queue_control(self, request):
        from .inputs import InputEvent, RobotObservation, mock_stage, mock_transcript

        node = self.node()
        if not node["online"] or (
            "input.scene" if request.kind == "stage" else "inputs"
        ) not in node.get("capabilities", []):
            raise Conflict("edge input capability unavailable")
        task_id = None
        if request.task_id:
            self.store.assert_active(request.task_id)
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT id FROM remote WHERE cloud_task=? AND status IN ('queued','accepted')",
                    (request.task_id,),
                ).fetchone()
            if row is None:
                raise Conflict("no active edge task for this cloud task")
            task_id = row[0]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            sequence = db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM controls").fetchone()[0]
            control = {"id": uuid.uuid4().hex, "kind": request.kind, "task_id": task_id}
            scene = self.store.meta("edge_scene", {}) or {}
            if request.kind == "stage":
                observations = mock_stage(
                    request.stage, sequence, viewpoint=scene.get("viewpoint", "home")
                )
            elif request.kind == "transcript":
                observations = [mock_transcript(task_id, request.text, sequence)]
            elif request.kind == "motion":
                observations = [
                    InputEvent(
                        event_id=uuid.uuid4().hex,
                        provider="mock-robot",
                        sequence=sequence,
                        observed_at=utcnow(),
                        source_ref="fixture:motion-outcome",
                        simulated=True,
                        data=RobotObservation(
                            safe_to_execute=scene.get("safe_to_execute", False),
                            viewpoint=scene.get("viewpoint", "home"),
                            motion_outcome=request.motion_outcome,
                        ),
                    )
                ]
            else:
                observations = []
            control["observations"] = [e.model_dump(mode="json") for e in observations]
            db.execute(
                "INSERT INTO controls(id,task_id,body) VALUES (?,?,?)",
                (control["id"], request.task_id, canonical(control)),
            )
            self.store._audit(db, request.task_id, "control.queued", control)
        return {"id": control["id"], "status": "queued", "simulated": True}

    def controls(self):
        with self.store.connect() as db:
            return [
                {
                    "id": r["id"],
                    "task_id": r["task_id"],
                    "body": json.loads(r["body"]),
                    "result": json.loads(r["result"]) if r["result"] else None,
                }
                for r in db.execute("SELECT * FROM controls ORDER BY seq DESC LIMIT 100")
            ]

    def control_result(self, ident, result):
        if result.get("status") not in {"applied", "rejected"}:
            raise ValueError("invalid control result")
        with self.store.connect() as db:
            row = db.execute("SELECT task_id,result FROM controls WHERE id=?", (ident,)).fetchone()
            if not row:
                raise KeyError(ident)
            if row["result"]:
                if json.loads(row["result"]) != result:
                    raise Conflict("control result identity conflict")
                return {"duplicate": True}
            db.execute("UPDATE controls SET result=? WHERE id=?", (canonical(result), ident))
            self.store._audit(db, row["task_id"], "control.result", {"id": ident, **result})
        return {"duplicate": False}

    def hello(self, msg: Hello):
        if msg.node_id != self.profile.node_id or PROTOCOL not in msg.protocol_versions:
            raise Conflict("unknown node or incompatible protocol")
        if msg.capability_digest != capability_fingerprint(msg.capabilities, msg.tools):
            raise Conflict("capability digest mismatch")
        if "capture" in msg.capabilities:
            if (
                len(msg.tools) != 1
                or not msg.tools[0].modes
                or msg.tools[0].input_schema != tool_schema(CaptureArguments)
            ):
                raise Conflict("capture tool manifest incompatible with negotiated protocol")
        elif msg.tools:
            raise Conflict("tool manifest claims an unavailable capability")
        node = {
            **msg.model_dump(mode="json"),
            "lease_until": time.time() + self.profile.lease_seconds,
            "transport_session": f"link:{msg.node_id}",
            "selected_protocol": PROTOCOL,
        }
        old = self.store.meta("node")
        self.store.set_meta("node", node)
        changed = old and (
            old["profile_digest"] != msg.profile_digest
            or old["capability_digest"] != msg.capability_digest
        )
        self.store.audit(
            None,
            "node.capabilities_changed" if changed else "node.hello",
            {"node_id": msg.node_id, "pid": msg.pid, "capabilities": msg.capabilities},
        )
        return {
            "message_type": "CLOUD_WELCOME",
            "session_id": node["transport_session"],
            "selected_protocol": PROTOCOL,
            "policy_version": "local-replay/1",
            "lease_seconds": self.profile.lease_seconds,
        }

    def node(self):
        node = self.store.meta("node")
        return {**node, "online": node["lease_until"] > time.time()} if node else {"online": False}

    def keepalive(self, node_id):
        node = self.store.meta("node")
        if not node or node["node_id"] != node_id:
            raise Conflict("HELLO required")
        node["lease_until"] = time.time() + self.profile.lease_seconds
        self.store.set_meta("node", node)
        return {"lease_until": node["lease_until"]}

    def enqueue(self, task_id, args, call_id):
        task = self.store.assert_active(task_id)
        if task["status"] != "running":
            raise Conflict("only active agent runs may submit remote work")
        node = self.node()
        if not node["online"] or "capture" not in node.get("capabilities", []):
            raise Conflict("edge offline or capture capability unavailable")
        if node.get("profile_digest") != self.profile.fingerprint:
            raise Conflict("edge has not activated the task profile")
        arguments = CaptureArguments.model_validate(args)
        if arguments.completion_mode not in node["tools"][0]["modes"]:
            raise Conflict("completion mode not advertised by edge")
        if arguments.completion_mode != task["context"]["mode"]:
            raise Conflict("completion mode must match the user task")
        subject = task["context"].get("user_context", {}).get("subject_id", "pet-cat-mimi")
        if arguments.constraints.subject_id != subject:
            raise Conflict("capture subject does not match the task")
        request_key = f"closeup:{subject}"
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT id,request FROM edge_tasks WHERE task_id=? AND request_key=?",
                (task_id, request_key),
            ).fetchone()
            if old:
                original = Invocation.model_validate_json(
                    db.execute("SELECT request FROM remote WHERE id=?", (old["id"],)).fetchone()[0]
                )
                if (
                    original.arguments.constraints != arguments.constraints
                    or original.arguments.completion_mode != arguments.completion_mode
                ):
                    raise Conflict("same capture intent cannot change constraints")
                return original
            edge_id = uuid.uuid4().hex
            deadline = task["context"]["expires"]
            if arguments.deadline:
                deadline = min(deadline, arguments.deadline.timestamp())
            if arguments.completion_mode == "inline":
                deadline = min(deadline, time.time() + self.profile.inline_seconds)
            if deadline <= time.time():
                raise Conflict("requested deadline has already elapsed")
            request = Invocation(
                message_id=uuid.uuid4().hex,
                node_id=self.profile.node_id,
                sent_at=utcnow(),
                expires_at=datetime.fromtimestamp(deadline, timezone.utc),
                idempotency_key=f"{task_id}:{request_key}",
                original_call_id=call_id,
                task_id=edge_id,
                arguments=arguments,
            )
            db.execute(
                "INSERT INTO remote(id,cloud_task,request,binding,status) VALUES (?,?,?,?,?)",
                (edge_id, task_id, canonical(request), f"cloud:{task_id}", "queued"),
            )
            db.execute(
                "INSERT INTO edge_tasks VALUES (?,?,?,?,?,NULL)",
                (edge_id, task_id, request_key, canonical(request), "sent"),
            )
            self.store._audit(db, task_id, "bridge.invoke", request.model_dump(mode="json"))
        return request

    async def invoke(self, task_id, args, call_id):
        req = self.enqueue(task_id, args, call_id)
        while time.time() < req.expires_at.timestamp():
            self.store.assert_active(task_id)
            row = self.remote(req.task_id)
            body = row["result"] or {}
            if "first" in body:
                return {**body["first"], "task_id": req.task_id}
            if row["status"] in {"cancelled", "expired", "failed"}:
                raise Conflict(row["status"])
            await asyncio.sleep(0.15)
        with self.store.connect() as db:
            db.execute(
                "UPDATE remote SET status='expired' WHERE id=? AND status!='completed'",
                (req.task_id,),
            )
            db.execute(
                "UPDATE edge_tasks SET status='expired' WHERE id=? AND status!='completed'",
                (req.task_id,),
            )
        raise TimeoutError("DEADLINE_EXCEEDED")

    def remote(self, ident):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM remote WHERE id=?", (ident,)).fetchone()
        if row is None:
            raise KeyError(ident)
        return {
            **dict(row),
            "request": json.loads(row["request"]),
            "result": json.loads(row["result"]) if row["result"] else None,
        }

    def take(self):
        if not self.node()["online"]:
            raise Conflict("node lease expired; HELLO required")
        self.store.expire()
        now = time.time()
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM remote WHERE status='queued' AND last_sent<?", (now - 2,)
            ).fetchall()
            cancels = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM remote WHERE status IN ('cancelled','expired') AND last_sent<?",
                    (now - 10,),
                )
            ]
            for r in rows:
                db.execute("UPDATE remote SET last_sent=? WHERE id=?", (now, r["id"]))
            for ident in cancels:
                db.execute("UPDATE remote SET last_sent=? WHERE id=?", (now, ident))
            controls = [
                json.loads(r[0])
                for r in db.execute(
                    "SELECT body FROM controls WHERE result IS NULL ORDER BY seq LIMIT 20"
                )
            ]
        return {
            "tasks": [
                {"invocation": json.loads(r["request"]), "session_binding": r["binding"]}
                for r in rows
            ],
            "cancellations": cancels,
            "controls": controls,
        }

    def first_result(self, result: FirstResult):
        remote = self.remote(result.task_id)
        req = Invocation.model_validate(remote["request"])
        if result.node_id != req.node_id or result.original_call_id != req.original_call_id:
            raise Conflict("call correlation mismatch")
        if result.status == "completed":
            if req.arguments.completion_mode != "inline" or result.result is None:
                raise Conflict("completed FINAL is only valid for inline")
            msg = Completion(
                event_id=f"final:{req.task_id}",
                node_id=req.node_id,
                task_id=req.task_id,
                original_call_id=req.original_call_id,
                session_binding=remote["binding"],
                occurred_at=utcnow(),
                result=result.result,
            )
            return self.complete(msg, inline=True)
        if result.status == "accepted" and req.arguments.completion_mode != "deferred":
            raise Conflict("inline cannot return accepted")
        body = result.model_dump(mode="json")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT * FROM remote WHERE id=?", (req.task_id,)).fetchone()
            stored = json.loads(current["result"]) if current["result"] else {}
            if "first" in stored:
                if stored["first"] != body:
                    raise Conflict("first result identity conflict")
                return {"duplicate": True}
            if (
                current["status"] in {"cancelled", "expired"}
                or time.time() >= req.expires_at.timestamp()
            ):
                return {"ignored": "terminal_or_expired"}
            stored["first"] = body
            status = "accepted" if result.status == "accepted" else "failed"
            db.execute(
                "UPDATE remote SET status=?,result=? WHERE id=?",
                (status, canonical(stored), req.task_id),
            )
            db.execute("UPDATE edge_tasks SET status=? WHERE id=?", (status, req.task_id))
            self.store._audit(db, remote["cloud_task"], f"bridge.{status}", body)
        return {"duplicate": False}

    def upload(self, upload: Upload):
        package = upload.package
        remote = self.remote(package.task_id)
        req = Invocation.model_validate(remote["request"])
        if package.subject_id != req.arguments.constraints.subject_id:
            raise Conflict("asset subject mismatch")
        data = base64.b64decode(upload.content_base64, validate=True)
        if (
            len(data) != package.asset.size_bytes
            or hashlib.sha256(data).hexdigest() != package.asset.sha256
        ):
            raise Conflict("asset checksum mismatch")
        try:
            existing = self.store.asset(package.asset.asset_id)
        except KeyError:
            existing = None
        if existing:
            if existing["task_id"] != remote["cloud_task"] or existing[
                "info"
            ] != package.model_dump(mode="json"):
                raise Conflict("asset identity conflict")
            if (
                hashlib.sha256(Path(existing["path"]).read_bytes()).hexdigest()
                != package.asset.sha256
            ):
                raise Conflict("stored asset was corrupted")
            # Lost final ACKs can cause asset retransmission after task completion.
            return {
                "asset_ref": f"cloud-asset://{package.asset.asset_id}",
                "verified": True,
                "duplicate": True,
            }
        self.store.assert_active(remote["cloud_task"])
        if remote["status"] in {"expired", "cancelled", "failed"} or req.expires_at <= utcnow():
            raise Conflict("remote task no longer accepts assets")
        directory = self.store.root / "assets"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{package.asset.asset_id}.mp4"
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != package.asset.sha256:
                raise Conflict("asset identity conflict")
            candidate = path
        else:
            temporary = path.with_suffix(".partial")
            temporary.write_bytes(data)
            candidate = temporary
        from .media import check_clip

        facts = check_clip(candidate, 6)
        expected = package.asset
        if any(
            facts[k] != getattr(expected, k)
            for k in ["width", "height", "frames", "duration_ms", "codec", "fps"]
        ):
            raise Conflict("asset media facts mismatch")
        if candidate != path:
            temporary.replace(path)
        self.store.put_asset(
            package.asset.asset_id,
            remote["cloud_task"],
            path,
            package.model_dump(mode="json"),
            "closeup",
        )
        self.store.audit(
            remote["cloud_task"],
            "asset.verified",
            {"asset_id": package.asset.asset_id, "sha256": package.asset.sha256},
        )
        return {"asset_ref": f"cloud-asset://{package.asset.asset_id}", "verified": True}

    def complete(self, event: Completion, inline=False):
        remote = self.remote(event.task_id)
        req = Invocation.model_validate(remote["request"])
        if (event.node_id, event.original_call_id, event.session_binding) != (
            req.node_id,
            req.original_call_id,
            remote["binding"],
        ):
            raise Conflict("completion correlation mismatch")
        if inline != (req.arguments.completion_mode == "inline"):
            raise Conflict("completion mode mismatch")
        asset_id = event.result.asset_ref.rsplit("/", 1)[-1]
        asset = self.store.asset(asset_id)
        package = asset["info"]
        if (
            asset["task_id"] != remote["cloud_task"]
            or package["task_id"] != req.task_id
            or package["asset"]["sha256"] != event.result.asset_sha256
        ):
            raise Conflict("completion asset mismatch")
        if (
            event.result.asset_ref != package["asset"]["uri"]
            or event.result.capture_package_ref
            != f"edge-asset://capture/{package['package_id']}.json"
            or event.result.qc != package["qc"]
            or not package["qc"].get("passed")
        ):
            raise Conflict("completion package or QC mismatch")
        if (
            hashlib.sha256(Path(asset["path"]).read_bytes()).hexdigest()
            != event.result.asset_sha256
        ):
            raise Conflict("stored asset was corrupted")
        payload = event.model_dump(mode="json")
        # Occurrence time is supplied by the sender and immutable across retries.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            duplicate = db.execute(
                "SELECT body FROM inbox WHERE id=?", (event.event_id,)
            ).fetchone()
            if duplicate:
                if duplicate[0] != canonical(payload):
                    # Inline FINAL retries have no event timestamp on the wire.
                    old = json.loads(duplicate[0])
                    old["occurred_at"] = payload["occurred_at"]
                    if not inline or old != payload:
                        raise Conflict("event identity conflict")
                return {"duplicate": True}
            current = db.execute("SELECT * FROM remote WHERE id=?", (req.task_id,)).fetchone()
            cloud_status = db.execute(
                "SELECT status FROM tasks WHERE id=?", (remote["cloud_task"],)
            ).fetchone()[0]
            if (
                current["status"] in {"cancelled", "expired", "failed"}
                or cloud_status in {"cancelled", "expired", "unsupported"}
                or req.expires_at <= utcnow()
            ):
                self.store._audit(
                    db, remote["cloud_task"], "bridge.late_result", {"event_id": event.event_id}
                )
                return {"ignored": "terminal_or_expired"}
            stored = json.loads(current["result"]) if current["result"] else {}
            if current["status"] == "completed":
                if stored.get("completion", {}).get("result") != payload["result"]:
                    raise Conflict("remote task already completed with different result")
                return {"duplicate": True}
            if not inline and "first" not in stored:
                raise Conflict("completion arrived before durable acceptance")
            stored["completion"] = payload
            if inline:
                stored["first"] = {
                    "status": "completed",
                    "result": payload["result"],
                    "simulated": True,
                }
            db.execute("INSERT INTO inbox VALUES (?,?)", (event.event_id, canonical(payload)))
            db.execute(
                "UPDATE remote SET status='completed',result=? WHERE id=?",
                (canonical(stored), req.task_id),
            )
            db.execute(
                "UPDATE edge_tasks SET status='completed',result=? WHERE id=?",
                (canonical(payload["result"]), req.task_id),
            )
            if not inline:
                db.execute(
                    "INSERT INTO events VALUES (?,?,?,?,?,'queued',NULL,?)",
                    (
                        event.event_id,
                        remote["cloud_task"],
                        "edge.completed",
                        canonical(payload),
                        time.time(),
                        time.time(),
                    ),
                )
            self.store._audit(
                db, remote["cloud_task"], "bridge.final" if inline else "bridge.completed", payload
            )
        return {"duplicate": False, "asset_verified": True}
