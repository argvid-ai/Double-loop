"""Dialogue scheduling and durable effects. NanoBot alone owns model/tool iteration."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path

import httpx

from .capture_sources import VideoFileSource
from .contracts import IntentArguments, UserMessage
from .interaction_inputs import CONTROL_PHRASES, ConversationIngress
from .interaction_runtime import InteractionRuntime
from .interaction_store import ConversationStore
from .media import check_clip
from .plugins import require_profile
from .replay import ReplayDevice
from .store import Conflict


class ConversationService:
    def __init__(self, profile, root, token, *, runtime_factory=InteractionRuntime):
        self.profile, self.root, self.token = profile, root, token
        self.store = ConversationStore(root / "edge")
        self.binding = require_profile(profile)["edge"]
        if "strategy.dialogue" not in self.binding["capabilities"]:
            raise ValueError("dialogue plugins required")
        saved = self.store.meta("interaction_profile_digest")
        if saved and saved != profile.fingerprint:
            active = [
                c
                for c in self.store.conversations()
                if c["intent"]["kind"]
                and c["intent"]["status"] not in {"completed", "cancelled", "unsupported"}
            ]
            if active:
                raise Conflict("active conversation intents pin their profile")
        self.store.set_meta("interaction_profile_digest", profile.fingerprint)
        self.inputs = ConversationIngress(self.store)
        self.device = ReplayDevice(self.store, VideoFileSource(self.store.root))
        self.runtime = runtime_factory(
            self.store, profile, self.execute, self.projection, summary_delegate=self._cloud_summary
        )
        self.stop = asyncio.Event()
        self.input_wake = asyncio.Event()
        self.effect_wake = asyncio.Event()
        self.workers = []
        self.control_tasks = set()
        self.cloud_error = None
        self.cloud = httpx.AsyncClient(
            base_url=profile.gateway_url or f"http://127.0.0.1:{profile.cloud_port}",
            headers={"Authorization": "Bearer " + token},
            timeout=15,
            trust_env=False,
        )

    async def _cloud_summary(self, body):
        response = await self.cloud.post(
            "/interaction/summary",
            json=body.model_dump(),
            timeout=self.profile.cloud.request_timeout + 5,
        )
        response.raise_for_status()
        return response.json()

    def projection(self, cid, *, rich=False):
        c = self.store.conversation(cid)
        intent = c["intent"]
        scene = self.store.scene()
        package = (
            self.store.action("capture:" + intent["capture_task"])
            if intent["capture_task"]
            else None
        )
        operation = self.store.capture_operation(intent["capture_task"])
        block = None
        if not intent["kind"]:
            block = "尚未保存拍摄或成片目标"
        elif intent["status"] == "cancelled":
            block = "任务已取消"
        elif intent["paused"]:
            block = "任务已暂停"
        elif intent.get("pending_question"):
            block = "等待用户确认：" + intent["pending_question"]
        elif package:
            block = "已有经过工具验证的近景，请复用"
        elif operation and operation["status"] == "failed":
            block = "上次拍摄处理失败；需查看错误并明确发起新的尝试"
        elif "capture" not in self.binding["capabilities"]:
            block = "未装载拍摄工具"
        elif scene.subject_id != intent["subject_id"]:
            block = "当前回放没有请求的目标"
        elif not scene.user_present:
            block = "用户不在场"
        elif not scene.safe_to_execute:
            block = "设备拒绝执行"
        elif not scene.subject_visible:
            block = "目标不可见"
        elif not scene.allowed:
            block = "现场观测过期或目标活动不满足要求"
        elif intent["status"] == "capturing":
            block = "已有拍摄在执行，不要重复提交"
        jobs = [j for j in self.store.delegations() if j["id"] == intent["cloud_job"]]
        job = jobs[0] if jobs else None
        state = {
            "intent": {
                k: intent[k]
                for k in [
                    "version",
                    "kind",
                    "goal",
                    "subject_id",
                    "duration_ms",
                    "orientation",
                    "requires_user_presence",
                    "flash_allowed",
                    "status",
                    "paused",
                    "pending_question",
                ]
            },
            "can_capture": block is None,
            "block_reason": block,
            "scene_revision": scene.revision,
            "scene": {
                "subject_id": scene.subject_id,
                "user_present": scene.user_present,
                "safe_to_execute": scene.safe_to_execute,
                "subject_visible": scene.subject_visible,
                "conditions_currently_allow": scene.allowed,
            },
            "capture": {
                "asset_id": package["asset"]["asset_id"],
                "duration_ms": package["asset"]["duration_ms"],
                "verified_receipt": True,
            }
            if package
            else None,
            "capture_operation": operation,
            "cloud": {"id": job["id"], "status": job["status"], "result": job["result"]}
            if job
            else {"status": "not_requested"},
            "live_camera": False,
            "input_mode": "mock_scene_audio_robot",
            "media_source": "licensed_video_replay",
            "model": self.profile.edge.model,
            "simulated": True,
        }
        if rich:
            state.update(
                scene=scene.model_dump(mode="json"),
                intent=intent,
                summary=c["summary"],
                capture_package=package,
                profile_digest=self.profile.fingerprint,
                cloud_transport_error=self.cloud_error,
            )
        return state

    def submit(self, cid, message: UserMessage):
        action = CONTROL_PHRASES.get(message.text.strip().rstrip("。！!"))
        if action:
            return self.control(cid, action, message.event_id, source_text=message.text)
        result = self.store.submit_message(cid, message)
        if not result["duplicate"]:
            self._cancel_model(cid)
            self.input_wake.set()
        return result

    def _cancel_model(self, cid):
        running = getattr(self.runtime, "current_event", None)
        if not running or running["conversation_id"] != cid:
            return
        expected_event_id = running["id"]

        async def cancel():
            try:
                await self.runtime.cancel_current(cid, expected_event_id=expected_event_id)
            except Exception as exc:
                self.store.audit(cid, "conversation.cancel.error", {"error": type(exc).__name__})

        task = asyncio.create_task(cancel())
        self.control_tasks.add(task)
        task.add_done_callback(self.control_tasks.discard)

    def control(self, cid, action, event_id, *, source_text=None):
        start = time.monotonic()
        result = self.store.control(cid, action, event_id=event_id, source_text=source_text)
        if not result["duplicate"]:
            self._cancel_model(cid)
            self._wake_control(cid, action, event_id)
        return {**result, "applied_seconds": time.monotonic() - start, "model_invoked": False}

    def _pending_user_ids(self, cid):
        return [
            v["id"]
            for v in self.store.inputs(cid)
            if v["kind"] == "user"
            and v["status"] in {"queued", "running", "coalesced", "interrupted"}
        ]

    def _wake_control(self, cid, action, receipt_id, *, exclude_input=None):
        if action == "resume":
            intent = self.store.conversation(cid)["intent"]
            pending = [ident for ident in self._pending_user_ids(cid) if ident != exclude_input]
            self.store.enqueue_event(
                cid,
                "resume:" + receipt_id,
                "restart",
                {"pending_user_ids": pending},
                intent_version=intent["version"],
            )
        self.input_wake.set()
        self.effect_wake.set()

    def ingest(self, event):
        result = self.inputs.ingest(event)
        if result.get("duplicate"):
            return result
        if event.data.kind == "transcript":
            self._cancel_model(event.task_id)
            if result.get("control"):
                self._wake_control(event.task_id, result["control"], event.event_id)
            self.input_wake.set()
            self.effect_wake.set()
        else:
            for c in self.store.conversations():
                i = c["intent"]
                if i["kind"] and i["status"] in {"ready", "waiting"} and not i["paused"]:
                    self.store.enqueue_event(
                        c["id"],
                        "scene:" + event.event_id + ":" + c["id"],
                        "scene.changed",
                        {"scene_revision": self.store.scene().revision},
                        intent_version=i["version"],
                    )
            self.input_wake.set()
        return result

    async def verified_capture(self, cid):
        intent = self.store.conversation(cid)["intent"]
        task = intent["capture_task"]
        package = self.store.action("capture:" + task) if task else None
        if not package:
            raise Conflict("no verified close-up available")
        asset = self.store.asset(package["asset"]["asset_id"])
        if asset["task_id"] != task or package["subject_id"] != intent["subject_id"]:
            raise Conflict("capture ownership/subject mismatch")
        facts = await asyncio.to_thread(check_clip, Path(asset["path"]), 6)
        if facts["sha256"] != package["asset"]["sha256"]:
            raise Conflict("capture bytes changed")
        return package, asset

    async def execute(self, turn, name, args, call_id):
        cid, revision = turn.cid, turn.revision
        if name == "get_task_context":
            return self.projection(cid, rich=True)
        if name == "set_request":
            return {
                "status": "saved",
                "intent": self.store.set_intent(
                    cid, revision, IntentArguments.model_validate(args)
                ),
            }
        if name == "control_request":
            result = self.store.control(cid, args["action"], revision=revision)
            self._wake_control(cid, args["action"], call_id, exclude_input=turn.event["id"])
            return result
        if name == "read_history":
            values = []
            for item in self.store.inputs(cid):
                if args.get("before_seq") and item["seq"] >= args["before_seq"]:
                    continue
                text = item["body"].get("text", "")
                if text:
                    values.append({"seq": item["seq"], "role": "user", "text": text})
            with self.store.connect() as db:
                replies = [
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT payload FROM audit WHERE task_id=? AND kind='conversation.turn.finished' ORDER BY seq",
                        (cid,),
                    )
                ]
            for payload in replies:
                if payload.get("answer"):
                    seq = payload["input_seq"]
                    if not args.get("before_seq") or seq < args["before_seq"]:
                        values.append({"seq": seq, "role": "assistant", "text": payload["answer"]})
            query = args.get("query", "")
            if query:
                values = [v for v in values if query.casefold() in v["text"].casefold()]
            selected = set(sorted({v["seq"] for v in values})[-args.get("limit", 5) :])
            values = sorted([v for v in values if v["seq"] in selected], key=lambda x: x["seq"])
            excerpts = []
            for value in values:
                offset = (
                    max(0, value["text"].casefold().find(query.casefold()) - 100) if query else 0
                )
                excerpts.append(
                    {
                        **value,
                        "text": value["text"][offset : offset + 300],
                        "excerpt": len(value["text"]) > 300,
                        "start_char": offset,
                        "full_length": len(value["text"]),
                    }
                )
            return {
                "messages": excerpts,
                "source": "current_conversation_persistent_records",
                "simulated": True,
            }
        if name == "start_capture":
            intent = self.store.assert_turn(cid, revision)
            if intent["paused"] or intent["status"] == "cancelled":
                raise Conflict("intent is paused or cancelled")
            if intent["capture_task"] and self.store.action("capture:" + intent["capture_task"]):
                package, _ = await self.verified_capture(cid)
                return {
                    "status": "completed",
                    "asset_id": package["asset"]["asset_id"],
                    "reused": True,
                    "simulated": True,
                }
            state = self.projection(cid)
            if not state["can_capture"]:
                raise Conflict(state["block_reason"])
            if args["expected_revision"] != state["scene_revision"]:
                raise Conflict("SCENE_CHANGED: refresh state")
            result = self.store.queue_capture(cid, revision, self.profile.fingerprint, args)
            self.effect_wake.set()
            return {
                "status": "accepted"
                if result["status"] in {"queued", "running"}
                else result["status"],
                "task_id": result["task_id"],
                "simulated": True,
            }
        if name == "publish_status":
            intent = self.store.assert_turn(cid, revision)
            if args["status"] == "completed":
                if intent["kind"] == "capture":
                    await self.verified_capture(cid)
                elif intent["kind"] == "compose":
                    job = next(
                        (j for j in self.store.delegations() if j["id"] == intent["cloud_job"]),
                        None,
                    )
                    if not job or job["status"] != "completed":
                        raise Conflict("cloud work is not verified complete")
                    asset = self.store.asset(job["result"]["local_asset_id"])
                    facts = await asyncio.to_thread(check_clip, Path(asset["path"]), 12)
                    if facts["sha256"] != job["result"]["sha256"]:
                        raise Conflict("cloud artifact changed")
            intent = self.store.set_status(cid, revision, **args)
            return {
                "status": intent["status"],
                "intent_version": intent["version"],
                "simulated": True,
            }
        if name == "delegate_cloud":
            package, _ = await self.verified_capture(cid)
            intent = self.store.assert_turn(cid, revision)
            ident = hashlib.sha256(f"{cid}:{intent['version']}:compose".encode()).hexdigest()[:32]
            request = {
                "id": ident,
                "conversation_id": cid,
                "intent_version": intent["version"],
                "goal": intent["goal"],
                "subject_id": intent["subject_id"],
                "duration_ms": intent["duration_ms"],
                "orientation": intent["orientation"],
                "capture_asset_id": package["asset"]["asset_id"],
                "capture_sha256": package["asset"]["sha256"],
                "expires_at": time.time() + self.profile.max_task_seconds,
                "profile_digest": self.profile.fingerprint,
                "simulated": True,
            }
            job = self.store.queue_delegation(cid, revision, request)
            return {
                "status": "accepted"
                if job["status"] in {"pending", "accepted", "running"}
                else job["status"],
                "job_id": job["id"],
                "acceptance_scope": "edge_outbox",
                "cloud_accepted": job["status"] != "pending",
                "simulated": True,
            }
        raise ValueError("unregistered conversation tool")

    async def _inputs_worker(self):
        while not self.stop.is_set():
            self.input_wake.clear()
            event = self.store.claim_input()
            if event is None:
                try:
                    await asyncio.wait_for(self.input_wake.wait(), 1)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                result = await self.runtime.run(event)
                self.store.finish_input(event, "failed" if result.get("failed") else "done", result)
            except asyncio.CancelledError:
                self.store.finish_input(event, "interrupted", {"error": "Interrupted"})
                if self.stop.is_set():
                    return
            except Exception as exc:
                superseded = (
                    self.store.conversation(event["conversation_id"])["revision"]
                    != event["revision"]
                )
                self.store.finish_input(
                    event, "interrupted" if superseded else "failed", {"error": type(exc).__name__}
                )
            finally:
                self.effect_wake.set()

    async def _effects_worker(self):
        while not self.stop.is_set():
            self.effect_wake.clear()
            effect = self.store.claim_effect()
            if effect is None:
                try:
                    await asyncio.wait_for(self.effect_wake.wait(), 1)
                except asyncio.TimeoutError:
                    pass
                continue
            cid = effect["conversation_id"]
            try:
                if self.store.action("capture:" + effect["task_id"]):
                    # A crash may occur after bytes+receipt commit but before
                    # the durable effect/event is marked complete. Do not
                    # replay motion using its now-obsolete scene revision.
                    package, _ = await self.verified_capture(cid)
                else:
                    package = await self.device.execute(
                        effect["task_id"], "capture_workflow", effect["arguments"]
                    )
                result = {
                    "asset_id": package["asset"]["asset_id"],
                    "sha256": package["asset"]["sha256"],
                    "simulated": True,
                }
                status = self.store.finish_effect(effect, "completed", result)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                result = {
                    "error": type(exc).__name__,
                    "reason": str(exc)
                    if isinstance(exc, Conflict)
                    else "capture processing failed",
                }
                status = self.store.finish_effect(effect, "failed", result)
            if status != "superseded":
                self.input_wake.set()

    async def _cloud_worker(self):
        def failed(job, reason):
            result = {"error": reason}
            self.store.update_delegation(job["id"], "failed", result)
            self.input_wake.set()

        while not self.stop.is_set():
            for job in self.store.delegations():
                if job["status"] == "superseded" and not job["cancel_sent"]:
                    if job["next_try"] > time.time():
                        continue
                    try:
                        response = await self.cloud.post(
                            "/interaction/delegations/" + job["id"] + "/cancel"
                        )
                        if response.status_code != 404:
                            response.raise_for_status()
                        self.store.cancellation_sent(job["id"])
                    except httpx.HTTPError:
                        self.store.delivery_failure(
                            job["id"], maximum=self.profile.delivery_max_attempts
                        )
                    continue
                if job["status"] not in {"pending", "accepted", "running"}:
                    continue
                if job["body"]["expires_at"] <= time.time():
                    failed(job, "DelegationExpired")
                    continue
                if job["next_try"] > time.time():
                    continue
                try:
                    if job["attempts"] >= self.profile.delivery_max_attempts:
                        health = await self.cloud.get("/health")
                        health.raise_for_status()
                        if health.json().get("profile_digest") != self.profile.fingerprint:
                            raise ValueError("cloud profile mismatch")
                        self.store.delivery_reconnected(job["id"])
                    if job["status"] == "pending":
                        response = await self.cloud.post(
                            "/interaction/delegations", json=job["body"]
                        )
                        response.raise_for_status()
                        self.store.update_delegation(job["id"], "accepted")
                    current = next(j for j in self.store.delegations() if j["id"] == job["id"])
                    if current["status"] == "superseded":
                        continue
                    response = await self.cloud.get("/interaction/delegations/" + job["id"])
                    response.raise_for_status()
                    data = response.json()
                    if data["id"] != job["id"] or data["intent_version"] != job["intent_version"]:
                        raise ValueError("cloud binding mismatch")
                    if data["status"] == "completed":
                        asset_id = "cloud-" + job["id"]
                        path = self.store.root / "assets" / (asset_id + ".mp4")
                        path.parent.mkdir(exist_ok=True)
                        temp = path.with_suffix(".partial.mp4")
                        async with self.cloud.stream(
                            "GET", "/interaction/delegations/" + job["id"] + "/asset"
                        ) as stream:
                            stream.raise_for_status()
                            size = 0
                            with temp.open("wb") as f:
                                async for chunk in stream.aiter_bytes():
                                    size += len(chunk)
                                    if size > 64 * 1024**2:
                                        raise ValueError("artifact exceeds bounded transfer limit")
                                    f.write(chunk)
                        facts = await asyncio.to_thread(check_clip, temp, 12)
                        if facts["sha256"] != data["asset"]["sha256"]:
                            raise ValueError("cloud artifact checksum mismatch")
                        temp.replace(path)
                        self.store.put_asset(
                            asset_id,
                            job["conversation_id"],
                            path,
                            {
                                "asset": {
                                    **data["asset"],
                                    "asset_id": asset_id,
                                    "uri": "edge-asset://" + asset_id,
                                },
                                "cloud_asset_ref": data["asset"]["uri"],
                                "source_job": job["id"],
                                "intent_version": job["intent_version"],
                                "simulated": True,
                            },
                            "deliverable",
                        )
                        result = {
                            "verified": True,
                            "local_asset_id": asset_id,
                            "sha256": facts["sha256"],
                            "cloud_task_id": data["task_id"],
                            "simulated": True,
                        }
                        applied = self.store.update_delegation(job["id"], "completed", result)
                        if applied:
                            self.input_wake.set()
                    elif data["status"] == "failed":
                        failed(job, "CloudTaskFailed")
                    else:
                        self.store.update_delegation(
                            job["id"], "accepted" if data["status"] == "accepted" else "running"
                        )
                    self.cloud_error = None
                    self.store.delivery_reconnected(job["id"])
                except (httpx.HTTPError, ValueError, KeyError, OSError, Conflict) as exc:
                    self.cloud_error = type(exc).__name__
                    permanent = isinstance(exc, (ValueError, KeyError, OSError, Conflict)) or (
                        isinstance(exc, httpx.HTTPStatusError)
                        and 400 <= exc.response.status_code < 500
                        and exc.response.status_code not in {408, 429}
                    )
                    if permanent:
                        failed(job, "CloudResponseRejected")
                    else:
                        self.store.delivery_failure(
                            job["id"], maximum=self.profile.delivery_max_attempts
                        )
            try:
                await asyncio.wait_for(self.stop.wait(), 0.5)
            except asyncio.TimeoutError:
                pass

    async def start(self):
        self.store.recover_interactions()
        self.workers = [
            asyncio.create_task(fn())
            for fn in [self._inputs_worker, self._effects_worker, self._cloud_worker]
        ]
        self.input_wake.set()
        self.effect_wake.set()

    def worker_health(self):
        return {
            name: ("stopped" if task.done() else "running")
            for name, task in zip(["dialogue", "capture", "cloud_transport"], self.workers)
        }

    async def close(self):
        self.stop.set()
        self.input_wake.set()
        self.effect_wake.set()
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        await asyncio.gather(*self.control_tasks, return_exceptions=True)
        await self.runtime.close()
        await self.cloud.aclose()
