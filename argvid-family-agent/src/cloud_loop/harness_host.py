"""Functional host: durable inputs, bounded cross-side mail and native runtime dispatch."""

from __future__ import annotations

import asyncio
import fcntl
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .contracts import canonical, digest
from .harness_contracts import PublishReportArgs, RuleArgs, RuleControlArgs, SaveTaskArgs
from .harness_observation import (
    READ_TOOLS as OBSERVATION_READ_TOOLS,
)
from .harness_observation import (
    TOOLS as OBSERVATION_TOOLS,
)
from .harness_observation import (
    ObservationBridge,
)
from .harness_runtime import HarnessRuntime
from .harness_state import HarnessStore
from .interaction_inputs import CONTROL_PHRASES
from .plugins import require_profile
from .store import Conflict

READ_TOOLS = {
    "get_delivery_policy",
    "lookup_sampling_plan",
    "read_report_period",
    "list_media_sources",
    "get_source_frame",
    "get_capabilities",
    "run_skill_script",
    "list_wake_rules",
    "read_record",
    "get_time",
    "read_autonomy",
    "list_conversations",
    "read_conversation",
    "get_task_context",
    "search_records",
    "peer_records",
    "get_scene_state",
    "get_robot_state",
    "media_list",
    "media_inspect",
    "inspect_media_frames",
}
READ_TOOLS.update(OBSERVATION_READ_TOOLS)


class HarnessHost:
    def __init__(
        self,
        profile,
        root,
        side,
        token,
        *,
        runtime_factory=HarnessRuntime,
        voice_sources=None,
        observation_factory=None,
    ):
        if profile.harness is None:
            raise ValueError("functional profile required")
        require_profile(profile)
        node_root = (Path(root) / side).resolve()
        node_root.mkdir(parents=True, exist_ok=True)
        with (node_root / "worker.lock").open("a") as initialization_lock:
            try:
                fcntl.flock(initialization_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("data directory already has a worker") from None
            self._initialize(
                profile,
                root,
                side,
                token,
                runtime_factory=runtime_factory,
                voice_sources=voice_sources,
                observation_factory=observation_factory,
            )

    def _initialize(
        self,
        profile,
        root,
        side,
        token,
        *,
        runtime_factory=HarnessRuntime,
        voice_sources=None,
        observation_factory=None,
    ):
        if profile.harness is None:
            raise ValueError("functional profile required")
        self.profile, self.side, self.token = profile, side, token
        from .harness_build import source_identity

        identity = source_identity()
        self.source_manifest, self.source_digest = identity["files"], identity["digest"]
        self.store = HarnessStore(Path(root) / side, side)
        self.settings = profile.harness
        from .harness_media_review import MediaReview

        self.media_reviews = MediaReview(self)
        from .harness_outputs import MediaOutputs

        self.media_outputs = MediaOutputs(self.store)
        self.binding = require_profile(profile)[side]
        self.base_binding = {**self.binding, "tools": list(self.binding["tools"])}
        if (
            "strategy.harness" in self.binding["capabilities"]
            and "run_skill_script" not in self.base_binding["tools"]
        ):
            self.base_binding["tools"].append("run_skill_script")
        self.configuration_lock = asyncio.Lock()
        if (
            runtime_factory is HarnessRuntime
            and side == "cloud"
            and self.settings.cloud_runtime == "pi"
        ):
            from .harness_pi_runtime import PiRuntime

            runtime_factory = PiRuntime
            self.binding["tools"] = list(
                dict.fromkeys(
                    [
                        *self.binding["tools"],
                        "bash",
                        "read_autonomy",
                        "configure_autonomy",
                        "message_work",
                    ]
                )
            )
            self.base_binding = {**self.binding, "tools": list(self.binding["tools"])}
        self.runtime_factory = runtime_factory
        if "native-context" not in getattr(profile, side).plugins:
            raise ValueError(
                "0.4 functional host requires native-context; use the frozen 0.3 source for older profiles"
            )
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pinned = db.execute(
                "SELECT value FROM metadata WHERE key='harness_profile_digest'"
            ).fetchone()
            if pinned and json.loads(pinned["value"]) != profile.fingerprint:
                active = db.execute(
                    "SELECT 1 FROM business_tasks WHERE status NOT IN ('completed','cancelled','expired') LIMIT 1"
                ).fetchone()
                queued = db.execute(
                    "SELECT 1 FROM harness_events WHERE status IN ('queued','running') LIMIT 1"
                ).fetchone()
                outgoing = db.execute(
                    "SELECT 1 FROM peer_mail WHERE status='pending' LIMIT 1"
                ).fetchone()
                observed = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='observation_tasks'"
                ).fetchone()
                observed = (
                    observed
                    and db.execute(
                        "SELECT 1 FROM observation_tasks WHERE status!='stopped' LIMIT 1"
                    ).fetchone()
                )
                observation_mail = db.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='observation_outbox'"
                ).fetchone()
                observation_mail = (
                    observation_mail
                    and db.execute(
                        "SELECT 1 FROM observation_outbox WHERE status IN ('pending','withdraw_pending') LIMIT 1"
                    ).fetchone()
                )
                if active or queued or outgoing or observed or observation_mail:
                    raise Conflict("active work pins the functional profile")
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('harness_profile_digest',?)",
                (canonical(profile.fingerprint),),
            )
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('harness_source_digest',?)",
                (canonical(self.source_digest),),
            )
        self.peer = "edge" if side == "cloud" else "cloud"
        # Installed adapters determine authority. A future microphone uses the
        # same VoiceInput schema; JSON flags alone cannot install that source.
        self.voice_sources = dict(
            {
                "mock-user": (True, "user_command"),
                "file-asr": (True, "user_command"),
                "media": (True, "media_content"),
            }
            if voice_sources is None
            else voice_sources
        )
        if "inputs" not in self.binding["capabilities"]:
            self.voice_sources = {}
        if any(
            not isinstance(v[0], bool) or v[1] not in {"user_command", "media_content"}
            for v in self.voice_sources.values()
        ):
            raise ValueError("invalid installed voice source policy")
        from .harness_input_store import InputMediaStore

        from .harness_health import RuntimeHealth

        self.health = RuntimeHealth(self.store)
        from .shared_media_storage import install_shared_storage

        install_shared_storage(self.store)
        self.input_media = InputMediaStore(self.store)
        from .observation_evidence import ObservationEvidence

        self.observation_evidence = ObservationEvidence(self.store)
        from .requested_observation_media import RequestedObservationMedia

        self.requested_observation_media = RequestedObservationMedia(self)
        self.observation_registry = None
        if side == "edge" and self.settings.observation_config_file:
            if observation_factory is not None:
                raise ValueError("choose configured or injected observation installation, not both")
            from .observation_installation import ObservationRegistry

            self.observation_registry = ObservationRegistry(
                self, self.settings.observation_config_file
            )
        from .harness_components import Components

        self.components = Components(self)
        self.runtime = runtime_factory(
            self.store, self.components.prepare_runtime(), self.execute, self.context
        )
        self.install_component_tools()
        self.autonomy = None
        if side == "cloud" and self.settings.cloud_runtime == "pi":
            from .harness_autonomy import Autonomy

            self.autonomy = Autonomy(self.store, self.settings, self.runtime.workspace)
            self.store.scheduler.autonomy = self.autonomy
        from .harness_effects import HarnessEffects

        from .media_storage import MediaStorage

        self.media_storage = MediaStorage(self)
        self.effects = HarnessEffects(self)
        from .edge_render import EdgeRenderer

        self.edge_renderer = EdgeRenderer(self)
        from .harness_mail import PeerBridge

        self.mail = PeerBridge(self)
        from .harness_records import PeerRecords

        self.peer_records = PeerRecords(self)
        from .harness_reports import MatterReports

        self.reports = MatterReports(self)
        from .harness_voice import VoiceGateway

        self.voice = VoiceGateway(self)
        from .harness_sources import SourceLibrary

        self.sources = SourceLibrary(self.store)
        from .harness_catalog import MediaCatalog

        self.catalog = MediaCatalog(self)
        if side == "edge":
            from .inputs import InputIngress

            self.inputs = InputIngress(self.store)
        self.stop = asyncio.Event()
        self.workers = []
        self.lock_file = None
        self.control_tasks = set()
        self.client = httpx.AsyncClient(
            base_url=(
                getattr(self.settings, "peer_url_" + self.peer)
                or f"http://127.0.0.1:{getattr(profile, self.peer + '_port')}"
            ),
            headers={"Authorization": "Bearer " + token},
            timeout=10,
            trust_env=False,
        )
        if self.observation_registry is not None:
            installation = self.observation_registry.installation()
        else:
            installation = observation_factory(self) if observation_factory else None
        self.observations = ObservationBridge(self, installation)
        from .observation_defaults import DefaultObservation

        self.default_observation = DefaultObservation(self)
        from .observation_capture_recovery import CaptureRecovery

        self.capture_recovery = CaptureRecovery(self)
        from .voice_audio import VoiceAudio

        self.voice_audio = VoiceAudio(self)
        self.channels = None
        if side == "cloud" and self.settings.feishu_config_file:
            from .harness_channels import FeishuBridge

            self.channels = FeishuBridge(self, self.settings.feishu_config_file)

    def candidate_image_review_capability(self):
        from .model_capabilities import model_accepts_images

        return {
            "configured": model_accepts_images(getattr(self.profile, self.side)),
            "tool": "inspect_media_frames",
            "input": "attached_saved_video_frames",
            "requires_live_camera": False,
            "required_for_edits": self.settings.media_review_required,
            "sampling_policy": self.settings.media_review_policy,
            "max_sample_gap_ms": self.settings.media_review_max_gap_ms
            if self.settings.media_review_policy == "coverage"
            else None,
            "minimum_distinct_frames_per_segment": 2
            if self.settings.media_review_policy == "coverage"
            else None,
            "project_rendering": {
                "available": bool(self.settings.remotion_runtime_dir),
                "tool": "start_job",
                "argument": "media_project",
                "execution_location": self.side,
                "asset_path": "public/<asset_id>.mp4",
                "runtime_directory": self.settings.remotion_runtime_dir,
                "browser_executable": self.settings.remotion_browser_executable,
            },
            "rendered_output_review": True,
            "approve_tool": "review_rendered_media",
            "renderers": ["ffmpeg"]
            + (
                ["remotion"]
                if self.settings.remotion_runtime_dir and self.settings.media_execution == "local"
                else []
            ),
            "execution_location": self.peer
            if self.settings.media_execution == "peer"
            else self.side,
        }

    def capabilities(self):
        from .harness_scripts import available_scripts
        from .model_capabilities import resolve_model_capabilities

        settings = getattr(self.profile, self.side)
        route = resolve_model_capabilities(settings)
        observation = self.observations.capabilities()

        enabled = (
            self.components.active["enabled_skills"]
            if "strategy.harness" in self.binding["capabilities"]
            else []
        )
        value = {
            **self.binding,
            "source_digest": self.source_digest,
            "provider": "llamacpp"
            if "llamacpp-qwen" in getattr(self.profile, self.side).plugins
            else "bridge-chat",
            "skills": enabled,
            "scripts": available_scripts(enabled),
            "context_tokens": (
                getattr(self.profile, self.side).model_capabilities.context_window
                if getattr(self.profile, self.side).model_capabilities
                else getattr(self.profile, self.side).context_tokens
            ),
            "output_tokens": getattr(self.profile, self.side).output_tokens,
            "harness_host": __import__("platform").node(),
            "legacy_media_compatibility": self.settings.legacy_media_compatibility,
            "candidate_image_review": self.candidate_image_review_capability(),
            "adapters": {
                "text": {"available": True, "simulated": False},
                "file_asr_ingress": {
                    "available": self.side == "edge" and "inputs" in self.binding["capabilities"],
                    "processing": "external local file adapter",
                    "acoustic_wake_verified": False,
                },
                "microphone": {
                    "available": None,
                    "status": "not_probed",
                    "probe_tool": "list_input_devices",
                },
                "streaming_asr_ingress": {
                    "available": self.side == "edge"
                    and "inputs" in self.binding["capabilities"]
                    and bool(self.settings.voice_stream_sources),
                    "sources": self.settings.voice_stream_sources,
                    "activation": "asr_text_gate",
                    "device_verified": False,
                },
                "tts": {
                    "available": self.settings.voice_audio_enabled,
                    "generation_side": "cloud",
                    "backend": self.settings.voice_tts_backend
                    if self.side == "cloud"
                    else "cloud_audio_download",
                    "physical_playback_verified": False,
                },
                "vision": {
                    "available": "image" in route["input_modalities"],
                    "scope": "configured_model_image_input",
                    "source": route["source"],
                },
                "semif": {
                    "available": observation["installed"],
                    "scope": "installed_observation_executor",
                    "model_admitted": observation["model_admitted"],
                },
                "physical_camera": {
                    "available": None,
                    "status": "not_probed",
                    "probe_tool": "list_input_devices",
                },
                "physical_robot": {"available": False},
                "replay_capture": {
                    "available": "capture" in self.binding["capabilities"]
                    and "start_job" in self.binding["tools"],
                    "simulated": True if "capture" in self.binding["capabilities"] else None,
                },
                "media_processing": {
                    "available": "media" in self.binding["capabilities"]
                    and "start_job" in self.binding["tools"],
                    "file_bytes_real": True if "media" in self.binding["capabilities"] else None,
                },
            },
        }
        value.update(
            authority="loaded_component_registry",
            model_invoked=False,
            available_adapters=[k for k, v in value["adapters"].items() if v["available"] is True],
            unavailable_adapters=[
                k for k, v in value["adapters"].items() if v["available"] is False
            ],
            unknown_adapters=[k for k, v in value["adapters"].items() if v["available"] is None],
        )
        value["component_revision"] = self.components.active["revision"]
        value["component_digest"] = digest(self.components.active)
        value["observation"] = observation
        value["model_route"] = route
        value["model_fallback_route"] = (
            resolve_model_capabilities(settings, fallback=True) if settings.model_fallback else None
        )
        value["workspace_files"] = getattr(self.runtime, "workspace_status", None)
        return value

    def install_component_tools(self):
        from .harness_components import ExternalTool

        names = self.components.effective_tools(self.components.active)
        definitions = self.components.definitions()
        self.binding = {**self.base_binding, "tools": sorted(names)}
        if hasattr(self.runtime, "tools"):
            for name in list(self.runtime.tools.tool_names):
                if name not in names:
                    self.runtime.tools.unregister(name)
            for definition in definitions.values():
                self.runtime.tools.register(ExternalTool(definition, self.runtime.execute))
            self.runtime.binding = {**self.binding}
            self.runtime.external_read_tools = {
                n for n, d in definitions.items() if d["mode"] == "read_only"
            }

    async def reload_components(self):
        await self.runtime.close()
        self.runtime = self.runtime_factory(
            self.store, self.components.prepare_runtime(), self.execute, self.context
        )
        self.install_component_tools()

    def open_work(self, message, principal, *, channel="web"):
        if message.conversation_id or message.task_id:
            raise Conflict("new work must not select another conversation or task")
        conversation = self.store.conversations.ensure(
            principal, "request:" + message.event_id, "work"
        )
        receipt = self.submit_user(
            message.model_copy(update={"conversation_id": conversation["id"]}),
            principal,
            channel=channel,
        )
        return {**receipt, "conversation_id": conversation["id"], "conversation": conversation}

    def submit_user(
        self,
        message,
        principal,
        *,
        channel="web",
        source=None,
        _db=None,
        _voice_forward=None,
        _text_forward=None,
    ):
        if self.side == "edge" and channel == "web":
            selected = (
                self.store.conversations.get(principal, message.conversation_id)
                if message.conversation_id
                else None
            )
            if selected is None or selected["kind"] == "main":
                if message.task_id:
                    raise Conflict("edge task input requires an explicit local work conversation")
                return self.voice.forward.enqueue_text(message, principal)
        body = message.model_dump(mode="json")
        if _text_forward is not None:
            if self.side != "cloud" or channel != "web" or _db is None:
                raise Conflict("invalid trusted text forwarding context")
            body["text_forward"] = _text_forward
        expired = False
        if channel == "voice":
            if _voice_forward is not None:
                # Private hook used only within authenticated cloud voice acceptance.
                if self.side != "cloud" or _db is None or message.purpose != "user_command":
                    raise Conflict("invalid trusted voice forwarding context")
                provider = "trusted-edge-voice"
                body["voice_forward"] = _voice_forward
                body["recognition_simulated"] = _voice_forward["adapter"] == "mock-transcript"
            else:
                provider = source or "mock-user"
                installed = self.voice_sources.get(provider)
                if installed is None or installed[0] != message.simulated:
                    raise Conflict("voice source not installed or simulation provenance mismatch")
                if installed[1] == "media_content":
                    body["purpose"] = "media_content"
            body["provider"] = provider
            expiry = message.observed_at.timestamp() + message.valid_for_ms / 1000
            expired = (
                expiry <= self.store.clock()
                or message.observed_at.timestamp() > self.store.clock() + 30
            )
            body["simulated"] = message.simulated
        media = source == "media" or body.get("purpose") == "media_content"
        action = CONTROL_PHRASES.get(message.text.strip().rstrip("。！!")) if not media else None
        read_only = message.mode == "query" or media
        kind = "media.transcript" if media else "voice" if channel == "voice" else "user"
        with nullcontext(_db) if _db is not None else self.store.connect() as db:
            if _db is None:
                db.execute("BEGIN IMMEDIATE")
            if (
                self.side == "edge"
                and db.execute(
                    "SELECT 1 FROM voice_forward_outbox WHERE id=?", (message.event_id,)
                ).fetchone()
            ):
                raise Conflict("input identity already belongs to cloud forwarding")
            previous = db.execute(
                "SELECT * FROM harness_events WHERE id=?", (message.event_id,)
            ).fetchone()
            if previous:
                result = self.store.submit(
                    message.event_id,
                    principal,
                    channel,
                    kind,
                    body,
                    task_id=previous["task_id"],
                    read_only=read_only,
                    _db=db,
                )
                return {
                    **result,
                    "reply": json.loads(previous["reply"]) if previous["reply"] else None,
                }
            if expired:
                raise Conflict("voice observation expired or in the future")
            task_id = message.task_id
            unbound_work = False
            if message.conversation_id:
                conversation = self.store.conversations.get(
                    principal, message.conversation_id, db=db
                )
                if conversation["kind"] == "work":
                    if task_id and conversation["task_id"] not in {None, task_id}:
                        raise Conflict("task does not belong to conversation")
                    task_id = task_id or conversation["task_id"]
                    unbound_work = not task_id
            clarification = None
            superseded = []
            observation_control = (
                self.side == "cloud"
                and not unbound_work
                and not task_id
                and self.observations.has_tasks(db, principal)
            )
            if action and not read_only and not task_id and not observation_control:
                candidates = [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,goal,status FROM business_tasks WHERE principal=? AND status NOT IN ('completed','cancelled','expired') ORDER BY updated DESC",
                        (principal,),
                    )
                ]
                if unbound_work:
                    candidates = []  # Never control another matter from an unbound work dialogue.
                executing = [t for t in candidates if t["status"] in {"running", "waiting_peer"}]
                choices = executing or candidates
                if len(choices) == 1:
                    task_id = choices[0]["id"]
                else:
                    clarification = {
                        "text": "没有可控制的当前任务。"
                        if not choices
                        else "当前有多个任务，请说明要控制哪一个；尚未改变任务状态。",
                        "choices": choices,
                        "model_invoked": False,
                        "needs_task_selection": bool(choices),
                    }
            if (
                action in {"pause", "cancel"}
                and not read_only
                and not task_id
                and not (clarification and clarification["needs_task_selection"])
            ):
                main_id = (
                    self.store.conversations.main(principal, db=db)["id"]
                    if self.side == "cloud" or not message.conversation_id
                    else None
                )
                scope = message.conversation_id or main_id
                superseded = [
                    r[0]
                    for r in db.execute(
                        """SELECT id FROM harness_events WHERE principal=? AND channel=?
                    AND kind IN ('user','voice') AND read_only=0 AND status IN ('queued','running')
                    AND COALESCE(session_key,json_extract(body,'$.conversation_id'),?)=?""",
                        (principal, channel, main_id, scope),
                    )
                ]
                for ident in superseded:
                    stopped = {
                        "text": "这条较早的操作请求已被后续控制指令中止。",
                        "reply_kind": "receipt",
                        "source": "superseded_input",
                        "event_id": ident,
                        "principal": principal,
                        "channel": channel,
                    }
                    if db.execute(
                        "SELECT 1 FROM observation_outbox WHERE source_event=? AND status IN ('pending','delivered')",
                        (ident,),
                    ).fetchone():
                        stopped.update(
                            text="已停止处理这条输入；端侧指令正在撤回，执行状态需等待端侧确认。",
                            remote_confirmation=False,
                        )
                    db.execute(
                        "UPDATE harness_events SET status='superseded',reply=?,finished=? WHERE id=?",
                        (canonical(stopped), self.store.clock(), ident),
                    )
                self.observations.cancel_sources(db, principal, superseded)
            if superseded and clarification:
                clarification["text"] = "已中止当前对话仍在处理的操作请求。" + clarification["text"]
            result = self.store.submit(
                message.event_id,
                principal,
                channel,
                kind,
                body,
                task_id=task_id,
                read_only=read_only,
                _db=db,
            )
            if result["duplicate"]:
                return result
            reply = clarification
            if media:
                reply = {
                    "text": "媒体转写已作为素材数据保存。",
                    "data_only": True,
                    "model_invoked": False,
                }
            elif action and not read_only and task_id:
                task = self.store._task(db, task_id, principal)
                if task["status"] in {"cancelled", "completed", "expired"}:
                    reply = {
                        "text": "任务已经结束，未重新执行控制。",
                        "task": task,
                        "model_invoked": False,
                    }
                else:
                    changed = self.store.control_business_task(
                        principal,
                        task["id"],
                        action,
                        task["version"],
                        source_event=message.event_id,
                        _db=db,
                    )
                    self.mail.queue_controls(db, changed, action)
                    reply = {
                        "text": {
                            "pause": "任务已在本侧暂停。",
                            "resume": "任务已在本侧恢复。",
                            "cancel": "任务已在本侧取消。",
                        }[action],
                        "task": changed,
                        "model_invoked": False,
                        "remote_confirmation": False,
                    }
                    if action == "resume":
                        self.store.submit(
                            "resume-" + digest(message.event_id)[:32],
                            principal,
                            channel,
                            "task.resumed",
                            {
                                "text": "用户已恢复本任务，检查最新状态后继续。",
                                "origin_event_id": message.event_id,
                                "task_version": changed["version"],
                            },
                            task_id=task["id"],
                            _db=db,
                        )
            running = getattr(self.runtime, "event", None)
            if (
                running
                and not running["read_only"]
                and (
                    running["id"] in superseded
                    or (reply is not None and task_id and running.get("task_id") == task_id)
                )
            ):
                db.execute(
                    "UPDATE harness_events SET status='superseded' WHERE id=? AND status='running'",
                    (running["id"],),
                )
                self.observations.cancel_sources(db, principal, [running["id"]])
                callback = asyncio.create_task(self.cancel_model_input(running["id"]))
                self.control_tasks.add(callback)
                callback.add_done_callback(self.control_tasks.discard)
            if reply is not None:
                reply.update(
                    channel=channel,
                    principal=principal,
                    event_id=message.event_id,
                    simulated=channel == "voice",
                    input_simulated=body.get("simulated", False),
                    reply_kind="receipt",
                )
                db.execute(
                    "UPDATE harness_events SET status='done',reply=?,finished=? WHERE id=?",
                    (canonical(reply), self.store.clock(), message.event_id),
                )
                self.store._audit(db, task_id, "harness.direct_input", reply)
                if channel == "voice":
                    self.store._audit(
                        db, task_id, "voice.output.mock", {**reply, "audio_generated": False}
                    )
                return {**result, "status": "done", "reply": reply}
        return result

    async def cancel_model_input(self, event_id):
        try:
            await self.runtime.cancel_event(event_id)
        except Exception as exc:
            self.store.audit(
                None,
                "control.model_cancel.error",
                {"event_id": event_id, "error_type": type(exc).__name__},
            )

    @staticmethod
    def compact_task(task):
        if not task:
            return None
        value = {
            k: task[k]
            for k in (
                "id",
                "kind",
                "goal",
                "version",
                "status",
                "constraints",
                "updated",
                "wait",
                "origin",
            )
        }
        labels = {
            "ready": "已就绪，未暂停",
            "waiting": "等待执行条件，未暂停",
            "waiting_peer": "等待另一侧结果，未暂停",
            "running": "正在执行，未暂停，尚未完成",
            "paused": "已暂停，不执行动作",
            "completed": "已完成",
            "cancelled": "已取消，不再执行",
            "expired": "已过期，不再执行",
            "failed": "本次执行失败",
        }
        value["current_execution_state"] = labels.get(task["status"], task["status"])
        value["paused"] = task["status"] == "paused"
        result = task.get("result") or {}
        value["result"] = {
            k: result[k]
            for k in (
                "verified",
                "asset_id",
                "asset_side",
                "operation",
                "stage",
                "sha256",
                "error",
                "simulated",
            )
            if k in result
        } or None
        if value["result"]:
            facts = (result.get("media_manifest") or result.get("package") or {}).get("asset", {})
            if facts:
                value["result"]["duration_ms"] = facts.get("duration_ms")
        value["schedule"] = task.get("schedule")
        if value["schedule"] and value["schedule"]["next_due"] is not None:
            value["schedule"] = {
                **value["schedule"],
                "next_due_iso": datetime.fromtimestamp(
                    value["schedule"]["next_due"], timezone.utc
                ).isoformat(),
            }
        return value

    def scene_projection(self):
        scene = self.store.scene()
        result = {
            **scene.model_dump(mode="json"),
            "conditions_currently_allow": scene.allowed,
            "checked_at": self.store.clock(),
            "observation_status": scene.observation_status,
        }
        for kind, fields in [
            ("scene", ["user_present", "subject_visible", "activity"]),
            ("robot", ["safe_to_execute", "viewpoint"]),
        ]:
            source = scene.field_sources.get(kind)
            if not source:
                for field in fields:
                    result[field] = None
        return result

    def context(self, event):
        if (
            not event["read_only"]
            and self.store.event(event["id"], event["principal"])["status"] != "running"
        ):
            raise Conflict("input was superseded")
        task = (
            self.store.business_task(event["task_id"], event["principal"])
            if event["task_id"]
            else None
        )
        if not event["read_only"]:
            event["_task_versions"] = {
                **event.get("_task_versions", {}),
                **self.store.event_versions(event["id"]),
            }
        if task and not event["read_only"]:
            versions = event.setdefault("_task_versions", {})
            if task["id"] in versions and versions[task["id"]] != task["version"]:
                raise Conflict("task was updated during this run")
            versions.setdefault(task["id"], task["version"])
            versions.update(self.store.pin_versions(event["id"], [task]))
        incoming = event["body"]
        source = incoming.get("peer_message") or {}
        peer_result = (source.get("payload") or {}).get("result") or {}
        value = {
            "side": self.side,
            "trigger_kind": event["kind"],
            "task": self.compact_task(task),
            "input": {
                k: incoming[k]
                for k in (
                    "text",
                    "original_question",
                    "origin_event_id",
                    "stale_for_execution",
                    "source_ref",
                    "purpose",
                    "observed_at",
                    "simulated",
                    "due",
                    "scheduled_local",
                    "occurrence_id",
                    "rule_revision",
                    "delivery_side",
                    "delivery_channel",
                    "job_id",
                    "task_version",
                    "final_occurrence",
                    "period_seconds",
                    "output_kind",
                    "observation_task_id",
                    "parent_task_id",
                    "output_key",
                )
                if k in incoming
            },
            "recent_tasks": []
            if task or event["read_only"]
            else [
                self.compact_task(t)
                for t in self.store.search(
                    event["principal"], "", "tasks", self.settings.retrieval_limit
                )["records"]
            ],
            "simulated_device": True,
            "simulated_device_scope": "legacy_scene_and_robot_contract_not_registered_media",
            "live_vision_available": False,
            "live_vision_meaning": "direct_live_camera_input_only",
            "registered_media_reading": {
                "local_tools": [
                    name
                    for name in (
                        "get_source_frame",
                        "inspect_media_frames",
                        "review_observation_period",
                    )
                    if name in self.binding["tools"]
                ],
                "source_truth": "per_media_tool_receipt_not_legacy_device_flag",
            },
            "candidate_image_review": self.candidate_image_review_capability(),
            "goal_creation_allowed": not event["read_only"],
            "independent_task_key": "creation_key",
            "background_workers": self.settings.background_workers,
            "workspace": str(self.runtime.workspace_for(event))
            if hasattr(self.runtime, "workspace_for")
            else None,
            "recording_ownership": "user_configured_shared_service; work_tasks_read_only",
            "shared_recording": self.observations.shared_recording.snapshot(event["principal"])
            if self.side == "cloud" else None,
            "media_outputs": self.media_outputs.context(task) if task else [],
            "existing_task_id": task["id"] if task else None,
        }
        if task and task["kind"] == "report" and event["kind"] == "timer":
            value["report_period"] = self.reports.period(event, task["id"])
        value["reply_channel"] = event["channel"]
        value["delivery_policy"] = (
            "reply to original input channel; no automatic finished-file transfer to edge"
        )
        value["user_reporting"] = self.store.delivery.read(
            event["principal"], task["id"] if task else None
        )
        if not event["read_only"]:
            for record in value["recent_tasks"]:
                event.setdefault("_task_versions", {}).setdefault(record["id"], record["version"])
            pinned = self.store.pin_versions(event["id"], value["recent_tasks"])
            event["_task_versions"].update(pinned)
        if source:
            value["peer_request"] = {
                k: source.get(k)
                for k in (
                    "id",
                    "sender",
                    "kind",
                    "phase",
                    "text",
                    "task_id",
                    "task_version",
                    "expires_at",
                )
            }
            value["peer_request"]["operation"] = source.get("payload", {}).get("operation")
            value["peer_request"]["media_recipe"] = source.get("payload", {}).get("media_recipe")
            value["peer_request"]["requires_capture"] = (
                (source.get("payload", {}).get("task") or {})
                .get("constraints", {})
                .get("requires_capture", False)
            )
            value["peer_request"]["transfer_result"] = source.get("payload", {}).get(
                "transfer_result", False
            )
            value["peer_request"]["result"] = {
                k: peer_result[k]
                for k in (
                    "verified",
                    "asset_id",
                    "asset_side",
                    "operation",
                    "stage",
                    "sha256",
                    "error",
                    "simulated",
                )
                if k in peer_result
            }
        if task:
            with self.store.connect() as db:
                value["local_jobs"] = [
                    dict(r)
                    for r in db.execute(
                        "SELECT id,kind,status,version FROM harness_jobs WHERE task_id=? AND version=?",
                        (task["id"], task["version"]),
                    )
                ]
                for job in value["local_jobs"]:
                    plan = db.execute(
                        "SELECT plan FROM harness_job_plans WHERE job_id=?", (job["id"],)
                    ).fetchone()
                    job["frozen_plan"] = json.loads(plan["plan"]) if plan else None
                messages = db.execute(
                    "SELECT id,status,body,result FROM peer_mail WHERE direction='out' AND json_extract(body,'$.task_id')=? AND json_extract(body,'$.task_version')=?",
                    (task["id"], task["version"]),
                ).fetchall()
            value["remote_work"] = [
                {
                    "request_id": r["id"],
                    "delivery_status": r["status"],
                    "phase": json.loads(r["result"]).get("phase") if r["result"] else "pending",
                }
                for r in messages
            ]
        if self.side == "edge":
            value["scene"] = self.scene_projection()
        else:
            value["assets"] = [
                {
                    "id": a["id"],
                    "kind": a["kind"],
                    **{
                        k: a["info"]["asset"].get(k) for k in ("duration_ms", "sha256", "simulated")
                    },
                }
                for a in self.effects.assets(event["task_id"], event["principal"])[-10:]
            ]
        return value

    async def execute(self, event, name, args, call_id):
        from .harness_authority import local_work, related_tasks, require_work_target, user_directed

        if name not in self.binding["tools"]:
            raise Conflict("tool not loaded")
        principal = event["principal"]
        from .harness_authority import require_recording_owner

        require_recording_owner(self.store, event, name, args)
        if name == "get_delivery_policy":
            return self.store.delivery.read(principal, args.get("task_id"))
        if name == "configure_delivery_policy":
            from .harness_delivery import DeliveryUpdate

            saved = self.store.event(event["id"], principal)
            if event["read_only"] or saved["read_only"] or saved["status"] != "running":
                raise Conflict("只读或已结束的输入不能修改汇报配置")
            owner = user_directed(saved)
            if not owner and (not local_work(saved) or not saved.get("task_id")):
                raise Conflict("修改汇报配置需要用户指令或已授权的任务")
            return self.store.delivery.configure(
                principal, DeliveryUpdate(**args), source=event["id"],
                agent_task=None if owner else saved["task_id"],
            )
        if name == "request_user_attention":
            from .harness_delivery import AttentionRequest

            return self.store.delivery.attention(event, AttentionRequest(**args))
        if name == "reply_with_voice":
            from .voice_reply import request_reply

            return request_reply(self, event, args, call_id)
        if name == "message_work":
            from .harness_authority import writable_work_handoff

            target = self.store.business_task(args["task_id"], principal)
            conversation = self.store.conversations.for_task(principal, target["id"])
            main = self.store.conversations.main(principal)
            ident = "work-message-" + digest({"event": event["id"], "call": call_id})[:32]
            # A completed artifact stays immutable, but a new human request can
            # create a separate follow-up task. Do not turn that request read-only.
            terminal = target["status"] in {"completed", "cancelled", "expired"}
            read_only = (
                not writable_work_handoff(self.store, event, target)
                or (terminal and event["kind"] not in {"user", "voice"})
            )
            receipt = self.store.submit(
                ident,
                principal,
                "system",
                "work.message",
                {
                    "text": args["text"],
                    "conversation_id": conversation["id"],
                    "origin_conversation_id": event.get("session_key") or main["id"],
                    "source_event_id": event["id"],
                    "source_kind": event["kind"],
                    "request_kind": args.get("request_kind", "instruction"),
                    "user_directed": user_directed(event),
                    "source_text": event["body"].get("text", ""),
                    "task_version": target["version"],
                    "target_status": target["status"],
                },
                task_id=target["id"],
                read_only=read_only,
            )
            return {
                "status": "queued",
                **receipt,
                "task_id": target["id"],
                "target_status": target["status"],
                "read_only": read_only,
                "terminal_task_policy": "Existing task and artifacts are immutable; create a new task for an authorized new version."
                if terminal
                else None,
            }
        if name in {"read_autonomy", "configure_autonomy"}:
            if not self.autonomy:
                raise Conflict("autonomy is not installed on this node")
            if name == "read_autonomy":
                return self.autonomy.read(principal)
            if event["read_only"]:
                raise Conflict("read-only input cannot change configuration")
            if event["kind"] != "autonomy" and not user_directed(event):
                raise Conflict(
                    "this background work can update its task wake rule; global autonomy requires user-directed control"
                )
            from .autonomy_contracts import ConfigureAutonomy

            return self.autonomy.configure(principal, ConfigureAutonomy(**args), event["id"])
        if event.get("kind") == "observation.alert":
            allowed = set(OBSERVATION_READ_TOOLS) | {"control_observation_task"}
            if name not in allowed:
                raise Conflict("health handling cannot change unrelated configuration or tasks")
            if name == "control_observation_task":
                issue = event["body"]["health"]
                scope = args.get("scope", "task")
                if scope == "task" and issue["component"] != "observation_task":
                    raise Conflict(
                        "a component fault cannot stop unrelated recording; use analysis scope for visual faults"
                    )
                if scope == "analysis" and issue["component"] not in {
                    "visual_worker",
                    "visual_analysis",
                    "visual_input",
                }:
                    raise Conflict("this fault cannot control visual analysis")
                if args.get("task_id") != issue.get("task_id") or args.get("action") not in {
                    "pause",
                    "resume",
                }:
                    raise Conflict("health handling is restricted to its existing task")
                current = await self.observations.invoke(
                    event, "query_observation", {"task_id": issue["task_id"]}
                )
                states = current.get("tasks", [])
                if len(states) != 1:
                    raise Conflict("health task state is unavailable")
                active_issue = next(
                    (r for r in current.get("health", []) if r.get("id") == issue.get("id")), None
                )
                if (
                    not active_issue
                    or not active_issue.get("active")
                    or active_issue.get("revision") != issue.get("revision")
                ):
                    raise Conflict("health incident has recovered or been superseded")
                task = states[0]["task"]
                if (task["version"], task["generation"]) != (
                    issue.get("version"),
                    issue.get("generation"),
                ):
                    raise Conflict("health event belongs to an older task generation")
                if task["status"] == "stopped" or (
                    task["status"] == "paused" and not task.get("reason")
                ):
                    raise Conflict(
                        "health handling cannot resume a manually stopped or paused task"
                    )
        if event.get("kind") == "timer" and name in {"attach_observation_media", "start_job"}:
            require_work_target(self.store, event, args["task_id"])
        if name in self.components.definitions():
            return await self.components.invoke(event, name, args)
        if event["read_only"] and name not in READ_TOOLS and name != "peer_request":
            raise Conflict("read-only event cannot execute this tool")
        if (
            not event["read_only"]
            and event.get("id")
            and self.store.event(event["id"], principal)["status"] != "running"
        ):
            raise Conflict("input was superseded")
        if not event["read_only"]:
            event["_task_versions"] = {
                **event.get("_task_versions", {}),
                **self.store.event_versions(event["id"]),
            }
        if name == "begin_media_output":
            from .harness_contracts import BeginMediaOutput

            return self.media_outputs.begin(event, BeginMediaOutput(**args))
        if name in OBSERVATION_TOOLS:
            if (
                name not in OBSERVATION_READ_TOOLS
                and name
                not in {
                    "attach_observation_media",
                    "review_observation_candidates",
                    "configure_media_storage",
                    "collect_media_storage",
                    "protect_media_file",
                }
                and not user_directed(event)
                and event["kind"] != "observation.alert"
            ):
                if name != "control_observation_task":
                    raise Conflict("observation changes require explicit user input")
                allowed_observations = set()
                with self.store.connect() as db:
                    for task_id in related_tasks(self.store, event):
                        row = db.execute(
                            "SELECT spec FROM wake_rules WHERE task_id=?", (task_id,)
                        ).fetchone()
                        if row:
                            allowed_observations.add(json.loads(row[0]).get("observation_task_id"))
                if (
                    name != "control_observation_task"
                    or args.get("task_id") not in allowed_observations
                ):
                    raise Conflict(
                        "background observation control requires an associated observation task"
                    )
            return await self.observations.invoke(event, name, args)
        target = args.get("task_id")
        read_only_peer_query = name == "peer_request" and args.get("kind") in {"query", "analysis"}
        if (
            target
            and not event["read_only"]
            and name not in READ_TOOLS
            and not read_only_peer_query
        ):
            if local_work(event):
                require_work_target(self.store, event, target)
            current = self.store.business_task(target, principal)
            expected = event.setdefault("_task_versions", {}).get(target)
            if expected is not None and expected != current["version"]:
                raise Conflict("task was updated after the model decision")
        if name == "list_media_sources":
            if args.get("target") == "peer":
                response = await self.client.post(
                    "/harness/sources/query",
                    json={"principal": principal, "source_id": args.get("source_id")},
                )
                response.raise_for_status()
                return {"side": self.peer, **response.json()}
            from .harness_sources import list_available_sources

            return list_available_sources(self, principal, args.get("source_id"))
        if name == "lookup_sampling_plan":
            from .sampling_lookup import lookup_sampling_plan

            return lookup_sampling_plan(args)
        if name == "get_source_frame":
            import base64

            from .harness_media_contracts import FrameRead
            from .harness_frame_read import read_frame
            from .harness_visual_review import VisualToolResult
            from .model_capabilities import model_accepts_images

            selection = FrameRead(**args)
            value, raw = await read_frame(self, principal, selection)
            if selection.media_ref:
                preview = f"/workbench/{value['origin_side']}/input-media/media/{selection.media_ref}/preview#t={selection.position_ms / 1000}"
            else:
                preview = f"/workbench/{value['origin_side']}/sources/{selection.source_id}/frame?position_ms={selection.position_ms}"
            receipt = {
                **value,
                "preview_url": preview,
            }
            if not model_accepts_images(getattr(self.profile, self.side)):
                return {**receipt, "image_delivery": "model_input_unavailable"}
            return VisualToolResult(
                {**receipt, "image_delivery": "actual_image_content_in_next_model_request"},
                [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64," + base64.b64encode(raw).decode(),
                        },
                    }
                ],
            )
        if name == "review_rendered_media":
            from .harness_media_review import ReviewRenderedMedia

            decision = await asyncio.to_thread(
                self.media_reviews.decide, event, ReviewRenderedMedia(**args)
            )
            if args["action"] == "approve":
                event["_reviewed_result"] = decision["result"]
            return decision
        if name == "inspect_media_frames":
            from .harness_visual_review import InspectMediaFrames, inspect_media_frames
            from .edge_render import ensure_local_asset
            from .model_capabilities import model_accepts_images

            if not model_accepts_images(getattr(self.profile, self.side)):
                raise Conflict("current model image input is not enabled in this deployment")
            available = {item["id"] for item in self.effects.assets(None, principal)}
            if args["asset_id"] not in available:
                raise Conflict("frame input is outside the available media catalogue")
            await ensure_local_asset(self, self.store.asset(args["asset_id"]))
            return await asyncio.to_thread(
                inspect_media_frames, self, event, InspectMediaFrames(**args)
            )
        if name == "get_capabilities":
            if args.get("target", "local") == "local":
                return {"online": True, **self.capabilities()}
            try:
                response = await self.client.get("/harness/capabilities")
                response.raise_for_status()
                result = response.json()
                if result.get("side") != self.peer:
                    raise ValueError("capability side mismatch")
                return {"online": True, **result}
            except (httpx.HTTPError, ValueError) as exc:
                return {
                    "online": False,
                    "side": self.peer,
                    "error_type": type(exc).__name__,
                    "authority": "peer_unavailable_no_capability_assumed",
                }
        if name == "run_skill_script":
            from .harness_scripts import run_skill_script

            return await run_skill_script(self, args)
        if name == "get_time":
            from zoneinfo import ZoneInfo

            now = datetime.fromtimestamp(
                self.store.clock(), ZoneInfo(args.get("timezone", "Asia/Shanghai"))
            )
            return {
                "iso": now.isoformat(),
                "timezone": str(now.tzinfo),
                "unix_seconds": now.timestamp(),
                "source": "host_clock",
                "side": self.side,
            }
        if name == "list_conversations":
            if self.side == "cloud":
                self.store.conversations.main(principal)
            return {"side": self.side, "conversations": self.store.conversations.list(principal)}
        if name == "read_conversation":
            conversation = self.store.conversations.get(principal, args["conversation_id"])
            with self.store.connect() as db:
                cursor = args.get("before_sequence")
                limit = args.get("limit", 100)
                rows = db.execute(
                    "SELECT rowid AS seq,id FROM harness_events WHERE principal=? AND session_key=? "
                    "AND (? IS NULL OR rowid<?) ORDER BY rowid DESC LIMIT ?",
                    (principal, args["conversation_id"], cursor, cursor, limit + 1),
                ).fetchall()
            page = rows[:limit]
            return {
                "conversation": conversation,
                "events": [self.store.event(r["id"], principal) for r in reversed(page)],
                "next_before_sequence": page[-1]["seq"] if len(rows) > limit else None,
                "authority": "conversation_history_not_current_task_state",
            }
        if name == "get_task_context":
            selected = {**event, "task_id": args.get("task_id") or event["task_id"]}
            result = self.context(selected)
            event["_task_versions"] = selected.get("_task_versions", {})
            return result
        if name == "peer_records":
            from .harness_record_view import search_view

            return search_view(await self.peer_records.remote_query(event, args))
        if name == "peer_update_record":
            return await self.peer_records.remote_update(event, args)
        if name == "read_record":
            table = {"history": "record_history", "memory": "user_memory"}[args["collection"]]
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT * FROM " + table + " WHERE id=? AND principal=?",
                    (args["record_id"], principal),
                ).fetchone()
                if row is None:
                    raise KeyError(args["record_id"])
                data = dict(row)
            text = data.pop("text")
            offset = args["offset"]
            part = text[offset : offset + args["max_chars"]]
            return {
                "record": data,
                "text": part,
                "offset": offset,
                "total_chars": len(text),
                "next_offset": offset + len(part) if offset + len(part) < len(text) else None,
                "authority": "historical_or_memory_record_not_current_execution_state",
            }
        if name == "search_records":
            result = self.store.search(principal, **args, exclude_source=event["id"])
            for record in result["records"]:
                if record["collection"] == "tasks":
                    event.setdefault("_task_versions", {}).setdefault(
                        record["id"], record["version"]
                    )
            if not event["read_only"]:
                event["_task_versions"].update(
                    self.store.pin_versions(
                        event["id"], [r for r in result["records"] if r["collection"] == "tasks"]
                    )
                )
            self.store.audit(
                event["task_id"],
                "records.retrieved",
                {
                    "event_id": event["id"],
                    "query": args,
                    "sources": [r["id"] for r in result["records"]],
                },
            )
            from .harness_record_view import search_view

            return search_view(result)
        if name == "remember":
            return self.store.remember(
                principal,
                args["key"],
                args["text"],
                event["id"],
                memory_id=args.get("memory_id"),
                expected_version=args.get("expected_version"),
                supersedes=args.get("supersedes", []),
            )
        if name == "save_task":
            # Observation inputs belong to an exact existing task version.
            # Saving them as a new recipe would increment that version and
            # immediately invalidate the input just selected by the model.
            for segment in (args.get("media_recipe") or {}).get("segments", []):
                if segment.get("asset_id"):
                    with self.store.connect() as db:
                        selected = db.execute(
                            "SELECT kind FROM assets WHERE id=?", (segment["asset_id"],)
                        ).fetchone()
                    if selected and selected["kind"] == "observation_input":
                        raise Conflict(
                            "observation input is already bound to the current goal version; use start_job(operation=compose, media_recipe=...) without saving a new task version"
                        )
            if (
                args["kind"] in {"capture", "compose"}
                and not args.get("media_recipe")
                and not self.settings.legacy_media_compatibility
            ):
                raise Conflict(
                    "0.4 capture/compose tasks require media_recipe: select registered sources, segments and output specification"
                )
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                result = self.store.save_business_task(
                    principal, SaveTaskArgs(**args), event["id"], _db=db
                )
                if args.get("task_id"):
                    self.mail.queue_controls(db, result, "cancel", notify_parent=False)
            if event["kind"] in {"user", "voice", "autonomy", "peer.task"}:
                event["task_id"] = result["id"]
            event.setdefault("_task_versions", {})[result["id"]] = result["version"]
            return result
        if name == "task_control":
            if not local_work(event) and event["kind"] != "peer.control":
                raise Conflict("control requires explicit input")
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                result = self.store.control_business_task(
                    principal,
                    args["task_id"],
                    args["action"],
                    args["expected_version"],
                    source_event=event["id"] + ":" + call_id,
                    _db=db,
                )
                self.mail.queue_controls(db, result, args["action"])
                db.execute(
                    "INSERT OR REPLACE INTO run_task_versions VALUES (?,?,?)",
                    (event["id"], result["id"], result["version"]),
                )
            event.setdefault("_task_versions", {})[result["id"]] = result["version"]
            return result
        if name == "list_wake_rules":
            return {
                "rules": self.store.scheduler.list(
                    principal,
                    task_id=args.get("task_id"),
                    include_deleted=args.get("include_deleted", False),
                ),
                "scheduler": "single_sqlite_owner_native_nanobot_calendar",
            }
        if name in {"set_wake_rule", "control_wake_rule"}:
            if not local_work(event):
                raise Conflict("rule changes require user or authorized autonomous input")
            if name == "set_wake_rule":
                rule = RuleArgs(**args)
                if rule.observation_task_id:
                    snapshot = await self.observations.invoke(
                        event, "query_observation", {"task_id": rule.observation_task_id}
                    )
                    tasks = snapshot.get("tasks", [])
                    if len(tasks) != 1:
                        raise Conflict("wait for a registered observation before binding delivery")
                    deadline = tasks[0]["task"]["config"].get("expires_at")
                    if deadline is not None:
                        remaining = deadline - snapshot["checked_at"]
                        if remaining <= 0:
                            raise Conflict(
                                "observation has already expired; use an immediate edit instead"
                            )
                        rule = rule.model_copy(
                            update={
                                "end_at": datetime.fromtimestamp(
                                    self.store.clock() + remaining + rule.finalize_grace_seconds,
                                    timezone.utc,
                                )
                            }
                        )
                result = self.store.scheduler.set(principal, rule, source_event=event["id"])
                if rule.start_on_observation:
                    runtime = snapshot["tasks"][0]
                    if runtime.get("worker_active") and runtime["task"].get("ledger", {}).get(
                        "batch_count", 0
                    ):
                        self.store.scheduler.observe_ready(principal, rule.observation_task_id)
                        result = self.store.scheduler.list(principal, task_id=rule.task_id)[0]
                return result
            return self.store.scheduler.control(
                principal, RuleControlArgs(**args), source_event=event["id"]
            )
        if name == "read_report_period":
            return await self.reports.read_period(event, args["task_id"])
        if name == "publish_report":
            result = self.reports.publish(event, PublishReportArgs(**args))
            event.setdefault("_published_reports", []).append(result["id"])
            return {**result, "delivery_owned_by": "report_tool"}
        if name == "set_timer":
            if not local_work(event):
                raise Conflict("timer changes require user input")
            return self.store.set_timer(
                principal, args["task_id"], args["interval_seconds"], args["enabled"]
            )
        if name == "peer_request":
            if event["read_only"] and event["kind"] in {"peer.result", "peer.failed", "job.result"}:
                raise Conflict(
                    "terminal notification must be delivered without another peer request"
                )
            return self.send_peer(event, args)
        if name in {"get_scene_state", "get_robot_state"}:
            projection = self.scene_projection()
            event["_observation_sources"] = projection.get("field_sources", {})
            event["_observation_checked_at"] = projection["checked_at"]
            return projection
        if name == "media_list":
            return {"assets": self.effects.assets(event["task_id"], principal)}
        if name == "media_inspect":
            from .media import inspect_video

            if args["asset_id"] not in {a["id"] for a in self.effects.assets(None, principal)}:
                raise Conflict("asset outside current principal scope")
            return await asyncio.to_thread(
                inspect_video, Path(self.store.asset(args["asset_id"])["path"])
            )
        if name == "start_job":
            task = self.store.business_task(args["task_id"], principal)
            operation = args.get("operation") or (
                "compose"
                if task["kind"] in {"goal", "edit"} and "media" in self.binding["capabilities"]
                else task["kind"]
            )
            if {"capture": "capture", "compose": "media"}.get(operation) not in self.binding[
                "capabilities"
            ]:
                raise Conflict("this task requires the other side or a concrete subtask")
            with self.store.connect() as db:
                prior = db.execute(
                    "SELECT id,status FROM harness_jobs WHERE task_id=? AND version=? AND kind=?",
                    (task["id"], task["version"], operation),
                ).fetchone()
            plan = None
            if args.get("media_project") is not None:
                from .harness_media_project import prepare_project

                plan = await asyncio.to_thread(
                    prepare_project,
                    self,
                    task,
                    operation,
                    args["media_project"],
                    self.runtime.workspace_for(event)
                    if hasattr(self.runtime, "workspace_for")
                    else None,
                )
            elif args.get("media_recipe") is not None or prior is None:
                from .harness_job_plan import prepare_job_plan

                plan = prepare_job_plan(self, task, operation, args.get("media_recipe"))
            if prior and prior["status"] == "completed" and plan is None:
                job = self.store.start_job(principal, task["id"], task["version"], operation)
                return {
                    "status": "completed",
                    "job_id": job["id"],
                    "result": json.loads(job["result"]),
                }
            if (
                operation == "compose"
                and not task["constraints"].get("media_recipe")
                and plan is None
            ):
                available = {a["kind"] for a in self.effects.assets(task["id"], principal)}
                missing = sorted({"wide", "closeup"} - available)
                if missing:
                    return {
                        "status": "needs_assets",
                        "task_id": task["id"],
                        "missing": missing,
                        "reason": "Only verified local assets associated with this task version can be composed. Remote records and other tasks do not supply bytes; request missing capture for this task.",
                    }
            if operation == "capture":
                scene = self.store.scene()
                reusable = self.effects.prior_capture(task)
                if not reusable and (
                    not scene.allowed
                    or (task["constraints"]["no_motion"] and scene.viewpoint != "front-mid")
                ):
                    with self.store.connect() as db:
                        db.execute(
                            "UPDATE business_tasks SET status='waiting' WHERE id=? AND version=? AND status IN ('ready','waiting')",
                            (task["id"], task["version"]),
                        )
                        db.execute(
                            "INSERT OR REPLACE INTO task_waits VALUES (?,?,?,?)",
                            (task["id"], task["version"], "capture", "fresh_scene_permission"),
                        )
                    return {
                        "status": "waiting",
                        "reason": "need fresh allowed scene and a permitted current viewpoint",
                    }
            job = self.store.start_job(
                principal,
                task["id"],
                event.get("_task_versions", {}).get(task["id"], task["version"]),
                operation,
                plan=plan,
            )
            if job["status"] == "completed":
                return {
                    "status": "completed",
                    "job_id": job["id"],
                    "result": json.loads(job["result"]),
                }
            return {"status": "accepted", "job_id": job["id"], "task_id": task["id"]}
        raise Conflict("tool is not yet bound in functional host")

    def ingest_observations(self, events):
        if self.side != "edge":
            raise Conflict("scene input belongs to edge")
        if "inputs" not in self.binding["capabilities"]:
            raise Conflict("input adapters not loaded")
        if any(e.data.kind == "transcript" for e in events):
            raise Conflict("use the voice adapter for user speech; media text is data only")
        previous = self.store.scene()
        results = self.inputs.ingest_many(events)
        scene = self.store.scene()
        if scene.allowed and (not previous.allowed or previous.revision != scene.revision):
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                tasks = db.execute(
                    "SELECT b.* FROM business_tasks b JOIN task_waits w ON w.task_id=b.id AND w.version=b.version WHERE b.status='waiting' AND w.operation='capture'"
                ).fetchall()
                for task in tasks:
                    no_motion = json.loads(task["constraints"])["no_motion"]
                    if no_motion and scene.viewpoint != "front-mid":
                        continue
                    if previous.allowed and (not no_motion or previous.viewpoint == "front-mid"):
                        continue
                    if db.execute(
                        "SELECT 1 FROM harness_events WHERE task_id=? AND kind='scene.changed' AND status IN ('queued','running') AND json_extract(body,'$.task_version')=?",
                        (task["id"], task["version"]),
                    ).fetchone():
                        continue
                    self.store.submit(
                        "scene-"
                        + digest({"events": [e.event_id for e in events], "task": task["id"]})[:32],
                        task["principal"],
                        "system",
                        "scene.changed",
                        {
                            "text": "现场条件更新，请检查已保存任务与最新许可。",
                            "task_version": task["version"],
                        },
                        task_id=task["id"],
                        _db=db,
                    )
        return results

    def send_peer(self, event, args):
        return self.mail.send(event, args)

    def receive_peer(self, message):
        return self.mail.receive(message)

    def reply_to_peer(self, event, result, *, _db=None):
        return self.mail.reply_event(event, result, _db=_db)

    def check_reply_freshness(self, event, result):
        """A valid observation can expire while the model or transport is busy."""
        peer = (event["body"].get("peer_message") or {}).get("payload", {}).get("result") or {}
        sources = event.get("_observation_sources") or peer.get("observation_sources")
        if (
            not event["read_only"]
            or not sources
            or result.get("pending_receipt")
            or result.get("error")
        ):
            return result
        result["observation_sources"] = sources
        checked_at = event.get("_observation_checked_at") or peer.get("observation_checked_at")
        result["observation_checked_at"] = checked_at
        expired = []
        for name, source in sources.items():
            try:
                valid_until = datetime.fromisoformat(source["valid_until"]).timestamp()
                stale = valid_until <= self.store.clock() and (
                    checked_at is None or valid_until > checked_at
                )
            except (KeyError, TypeError, ValueError):
                stale = True
            if stale:
                expired.append(name)
        if expired:
            result["raw_model_text"] = result.get("raw_model_text") or result.get("text")
            result["text"] = (
                "本次分析使用的现场观测在答复交付前已过期，当前是否允许拍摄无法确认。需要重新获取用户在场、主体可见和设备许可等观测；此前分析仅保留作历史，未执行拍摄或移动。"
            )
            result["freshness_guard"] = {
                "expired_sources": expired,
                "checked_at": self.store.clock(),
            }
        return result

    def prepare_periodic_output(self, event):
        if event["kind"] != "timer" or event["body"].get("output_kind") != "video":
            return None
        from .harness_contracts import BeginMediaOutput

        task = self.store.business_task(event["task_id"], event["principal"])
        outputs = self.media_outputs.context(task)
        current = next((o for o in outputs if o["task_version"] == task["version"]), None)
        result = task.get("result") or {}
        if not current and result.get("review_required") and result.get("review_status") != "approved":
            return task  # Pre-migration render: continue its existing review.
        if current and result.get("review_status") != "approved":
            output = current  # A timeout must not discard an unfinished film.
        else:
            output = self.media_outputs.begin(event, BeginMediaOutput(
                task_id=task["id"], expected_version=task["version"],
                output_key=event["id"], brief=task["goal"],
            ))
        event["body"]["output_key"] = output["output_key"]
        return self.store.business_task(task["id"], event["principal"])

    async def process_one(self, lane=None):
        if getattr(self.runtime, "concurrent", False):
            async with self.configuration_lock:
                event = self.store.claim_event(self.settings, lane=lane)
            if event is None:
                return False
            return await self._process_claimed(event)
        async with self.configuration_lock:
            event = self.store.claim_event(self.settings)
            return await self._process_claimed(event) if event else False

    async def _process_claimed(self, event):
        try:
            return await self._process_one(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            from .harness_worker_recovery import finish_failed_step

            return await finish_failed_step(self, event, exc)

    async def _process_one(self, event=None):
        event = event or self.store.claim_event(self.settings)
        if event is None:
            return False
        try:
            if event["kind"] == "job.result":
                task = self.store.business_task(event["task_id"], event["principal"])
                receipt = event["body"].get("result") or {}
                with self.store.connect() as db:
                    job = db.execute(
                        "SELECT version,status,kind FROM harness_jobs WHERE id=?",
                        (event["body"].get("job_id", event["id"].removeprefix("result-")),),
                    ).fetchone()
                if (
                    not job
                    or job["version"] != task["version"]
                    or task["status"] in {"paused", "cancelled"}
                    or task.get("result") != receipt
                ):
                    text = "收到旧作业回执，当前任务状态已变化，该结果未作为当前任务完成。"
                elif receipt.get("verified") is True and job["status"] == "completed":
                    text = (
                        "十二秒竖屏成片已生成并核验，可在云端网页查看；素材来自回放。"
                        if self.side == "cloud"
                        else "六秒回放素材已生成并核验；这是模拟采集。"
                    )
                    if receipt.get("media_manifest"):
                        duration = (
                            (receipt.get("media_manifest") or {})
                            .get("asset", {})
                            .get("duration_ms", task["constraints"]["duration_ms"])
                        )
                        text = f"{duration / 1000:g}秒文件产物已生成并核验；这是回放处理，未验证现场拍摄或视觉质量。"
                        if job["kind"] == "capture" and task["kind"] in {"goal", "compose"}:
                            text += " 这只是采集步骤，剩余制作步骤仍需继续。"
                    if task["kind"] == "goal":
                        text += " 长期事项仍保持打开，后续按已保存的规则推进。"
                    if receipt.get("reused_from_job"):
                        text = "已复用此前核验的六秒回放素材，没有重新采集。"
                else:
                    text = "本次工具作业未完成，请查看失败记录后重试。"
                result = {
                    "text": text,
                    "reply_kind": "result",
                    "model_invoked": False,
                    "source": "committed_job_receipt",
                    "error": receipt.get("error"),
                }
            else:
                self.prepare_periodic_output(event)
                if self.side == "cloud":
                    await self.observations.shared_recording.refresh(event["principal"], force=True)
                result = await self.runtime.run(event)
        except asyncio.CancelledError:
            if self.stop.is_set() or asyncio.current_task().cancelling():
                raise
            result = {
                "text": "本次模型调用已取消，后续输入仍可继续。",
                "error": None,
                "model_invoked": True,
                "source": "cancelled_model_run",
            }
        except Exception as exc:
            result = {"text": "本轮处理失败，输入已保存。", "error": type(exc).__name__}
        if event["kind"] == "timer" and event.get("task_id") and not result.get("error"):
            task = self.store.business_task(event["task_id"], event["principal"])
            if event["body"].get("output_kind") == "video":
                if task["status"] == "cancelled":
                    result = {
                        **result,
                        "period_skipped": True,
                        "period_skip_reason": "period_cancelled",
                    }
                with self.store.connect() as db:
                    submitted = db.execute(
                        "SELECT 1 FROM harness_jobs WHERE task_id=? AND version=? AND kind='compose' "
                        "AND (status IN ('queued','running','waiting') OR "
                        "(status='completed' AND json_extract(result,'$.verified')=1))",
                        (task["id"], task["version"]),
                    ).fetchone()
                if (
                    not result.get("period_skipped")
                    and not submitted
                    and not (
                        task["status"] == "completed" and (task.get("result") or {}).get("verified")
                    )
                ):
                    result = {
                        **result,
                        "text": "本期没有提交视频制作作业，也没有已核验成片；尚未完成视频交付。",
                        "error": "RequiredVideoMissing",
                    }
            if task["kind"] == "report":
                with self.store.connect() as db:
                    published = db.execute(
                        "SELECT 1 FROM matter_reports WHERE source_event=?", (event["id"],)
                    ).fetchone()
                if not published:
                    result = {
                        **result,
                        "text": "本期报告尚未提交，不能视为已完成播报。",
                        "error": "RequiredReportMissing",
                    }
        if event["kind"] == "media.review" and not result.get("pending_receipt"):
            reviewed = self.store.business_task(event["task_id"], event["principal"])
            if (reviewed.get("result") or {}).get("review_status") == "approved":
                result = {**result, "reviewed_result": reviewed["result"]}
            else:
                result = {
                    **result,
                    "text": "影片尚未完成复看批准，或修正尚未提交；不能视为已交付。",
                    "error": result.get("error") or "MediaReviewIncomplete",
                    "delivery_incomplete": True,
                }
        if event["kind"] not in {"user", "voice"} and result.get("text", "").strip() == "NO_REPLY":
            result = {**result, "text": "", "quiet": True}
        if event.get("_reviewed_result"):
            result = {**result, "reviewed_result": event["_reviewed_result"], "quiet": False}
            if not result.get("text"):
                result["text"] = "新的影片已复看批准，已存入成片库。"
        if event.get("_published_reports"):
            result = {**result, "published_reports": event["_published_reports"]}
        result = self.check_reply_freshness(event, result)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            from .voice_reply import prepare_reply

            result = prepare_reply(self, event, result, db)
            self.media_outputs.finish_deadline(event, result, db=db)
            result = self.store.finish_event(event, result, error=result.get("error"), _db=db)
            self.reply_to_peer(event, result, _db=db)
            self.store.project_origin_notification(event, result, db=db)
            voice_queued = self.voice_audio.queue(event, result, _db=db) if event["channel"] == "voice" else False
        if self.channels:
            self.channels.wake.set()
        if event["channel"] == "voice" and not voice_queued:
            simulated_voice = bool(event["body"].get("simulated", True))
            self.store.audit(
                event["task_id"],
                "voice.output.mock" if simulated_voice else "voice.output.unavailable",
                {
                    "event_id": event["id"],
                    "principal": event["principal"],
                    "text": result["text"],
                    "simulated": simulated_voice,
                    "audio_generated": False,
                },
            )
        return True

    async def deliver_once(self):
        self.store.delivery.flush()
        self.voice.recover_pending()
        await self.voice.forward.deliver_once()
        await self.mail.deliver_once()
        await self.reports.deliver_once()
        await self.observations.deliver_once()
        await self.observations.notifications.deliver_once()
        await self.observations.shared_recording.refresh()
        await self.observations.poll_once()
        await self.observations.poll_health_once()

    async def _events(self, lane=None):
        from .harness_worker_recovery import RecoveringSteps

        retry = RecoveringSteps(self)
        while not self.stop.is_set():
            if not await retry.run("events_" + (lane or "single"), lambda: self.process_one(lane=lane)):
                await asyncio.sleep(0.1)

    async def _network(self):
        from .harness_worker_recovery import RecoveringSteps

        retry = RecoveringSteps(self)
        steps = {
            "result_delivery": self.store.delivery.flush,
            "peer_expiry": self.store.expire_peer_tasks,
            "timers": self.store.due_timers,
            "voice_recovery": self.voice.recover_pending,
            "voice_forward": self.voice.forward.deliver_once,
            "mail": self.mail.deliver_once,
            "reports": self.reports.deliver_once,
            "observation_commands": self.observations.deliver_once,
            "observation_notifications": self.observations.notifications.deliver_once,
            "shared_recording_status": self.observations.shared_recording.refresh,
            "observation_poll": self.observations.poll_once,
            "health_poll": self.observations.poll_health_once,
        }
        while not self.stop.is_set():
            for name, operation in steps.items():
                await retry.run(name, operation)
            await asyncio.sleep(0.2)

    async def _voice_audio(self):
        while not self.stop.is_set():
            await self.voice_audio.process_one()
            await asyncio.sleep(0.2)

    async def _voice_notifications(self):
        # Secondary results cannot delay new user input or control delivery.
        while not self.stop.is_set():
            await self.voice.notifications.poll_once()
            await asyncio.sleep(0.2)

    async def _effects(self):
        from .harness_worker_recovery import RecoveringSteps

        retry = RecoveringSteps(self)
        while not self.stop.is_set():
            if not await retry.run("effects", self.effects.process_one):
                await asyncio.sleep(0.1)

    async def _default_observation(self):
        from .harness_worker_recovery import RecoveringSteps

        retry = RecoveringSteps(self)
        while not self.stop.is_set():
            await retry.run("bounded_capture", self.capture_recovery.tick)
            await retry.run("default_capture", self.default_observation.tick)
            await asyncio.sleep(1)

    async def _media_maintenance(self):
        """Infrequent derived-media maintenance, independent of frame processing."""
        from .audit_archive import AuditArchive

        archive = AuditArchive(self.store, self.settings)
        while not self.stop.is_set():
            try:
                result = await asyncio.to_thread(archive.run_once)
                self.health.record(
                    "local-user",
                    "audit_archive",
                    "capacity_exhausted" if result.get("budget_exhausted") else "healthy",
                )
            except Exception as exc:
                self.health.record("local-user", "audit_archive", type(exc).__name__)
            shared = getattr(self.store, "shared_media_storage", None)
            if shared and self.side == "edge":
                try:
                    await asyncio.to_thread(shared.collect_libraries, self.input_media, "local-user")
                    self.health.record("local-user", "shared_libraries", "healthy")
                except Exception as exc:
                    self.health.record("local-user", "shared_libraries", type(exc).__name__)
            for category in self.media_storage.configuration()["policy"]["pools"]:
                try:
                    receipt = await asyncio.to_thread(self.media_storage.collect, category)
                    self.health.record(
                        "local-user",
                        "media_storage_" + category,
                        "healthy" if receipt["writable"] else "capacity_exhausted",
                    )
                except BlockingIOError:
                    pass
                except Exception as exc:
                    self.health.record(
                        "local-user", "media_storage_" + category, type(exc).__name__
                    )
            try:
                await asyncio.wait_for(
                    self.stop.wait(), timeout=60 if shared else self.settings.media_maintenance_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def _raw_retention(self):
        """Recording maintenance must not depend on model/task startup or latency."""
        shared = getattr(self.store, "shared_media_storage", None)
        if shared and self.side != "edge":
            return  # One maintenance owner for this physical disk.
        previous_error = None
        while not self.stop.is_set():
            try:
                work = asyncio.create_task(
                    asyncio.to_thread(self.input_media.retention.collect, "local-user")
                )
                self.control_tasks.add(work)
                work.add_done_callback(self.control_tasks.discard)
                storage = await asyncio.shield(work)
                if storage.get("writable") is False:
                    raise Conflict("protected or busy recording pool is full")
                self.health.record("local-user", "recording_retention", "healthy")
                previous_error = None
            except Exception as exc:
                error = type(exc).__name__
                self.health.record("local-user", "recording_retention", error)
                if error != previous_error:
                    self.store.audit(
                        None, "recording.retention.worker_error", {"error_type": error}
                    )
                previous_error = error
            await asyncio.sleep(
                60 if shared else getattr(getattr(self, "settings", None), "observation_maintenance_seconds", 10)
            )

    async def _retention(self):
        while not self.stop.is_set():
            try:
                await self.observations.retention_once()
            except Exception as exc:
                self.store.audit(
                    None, "observation.retention.worker_error", {"error_type": type(exc).__name__}
                )
            await asyncio.sleep(self.settings.observation_maintenance_seconds)

    def recover_user_ingress(self):
        """Do not resume legacy main-chat runs on edge; keep their history readable.

        Explicit work conversations and task/background execution are preserved.
        New ingress is already in the forwarding outbox, not this event queue.
        """
        if self.side != "edge":
            return 0
        with self.store.connect() as db:
            result = db.execute(
                """UPDATE harness_events SET status='superseded',
                error='legacy_edge_main_read_only',finished=?
                WHERE kind IN ('user','voice') AND status IN ('queued','running')
                AND NOT EXISTS (SELECT 1 FROM harness_conversations c
                    WHERE c.id=COALESCE(harness_events.session_key,json_extract(harness_events.body,'$.conversation_id'))
                    AND c.principal=harness_events.principal AND c.kind='work')""",
                (self.store.clock(),),
            )
            return result.rowcount

    async def start(self):
        if self.lock_file is not None or self.workers:
            raise RuntimeError("host already started; create a new instance for restart")
        self.lock_file = (self.store.root / "worker.lock").open("a")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            self.lock_file = None
            raise RuntimeError("data directory already has a worker") from None
        if (
            self.store.meta("harness_profile_digest") != self.profile.fingerprint
            or self.store.meta("harness_source_digest") != self.source_digest
        ):
            self.lock_file.close()
            self.lock_file = None
            raise RuntimeError(
                "configuration changed after initialization; recreate the host before starting"
            )
        try:
            self.recover_user_ingress()
            self.store.recover_harness()
            self.voice.recover_pending()
            if self.side == "cloud":
                self.store.conversations.main("local-user")
            self.store.audit(
                None,
                "gateway.started",
                {"side": self.side, "scheduler": "matter_rules", "model_polling": False},
            )
            self.effects.load_fixtures()
            self.catalog.seed_demo_replay("local-user")
            await self.observations.recover()
            from .harness_worker_recovery import supervise_worker

            def worker(name, operation):
                return asyncio.create_task(supervise_worker(self, name, operation))

            self.workers = [
                *(
                    [
                        worker("interactive", lambda: self._events("interactive")),
                        *(
                            worker("background", lambda: self._events("background"))
                            for _ in range(self.settings.background_workers)
                        ),
                    ]
                    if getattr(self.runtime, "concurrent", False)
                    else [worker("events", self._events)]
                ),
                worker("network", self._network),
                worker("effects", self._effects),
                worker("media_maintenance", self._media_maintenance),
            ]
            if self.channels:
                await self.channels.start()
            if self.settings.voice_audio_enabled:
                self.workers.append(worker("voice_audio", self._voice_audio))
            if self.side == "edge":
                self.workers.append(worker("voice_notifications", self._voice_notifications))
                self.workers.append(worker("default_capture", self._default_observation))
                self.workers.append(worker("raw_retention", self._raw_retention))
            if (
                self.observations.installation is not None
                and self.observations.installation.retention is not None
            ):
                self.workers.append(worker("retention", self._retention))
        except BaseException:
            await self.close()
            raise

    async def close(self):
        import logging
        logger = logging.getLogger("uvicorn.error")
        logger.warning("Muse shutdown phase: %s channels", self.side)
        self.stop.set()
        if self.channels:
            await self.channels.close()
        logger.warning("Muse shutdown phase: %s workers", self.side)
        for task in self.workers:
            task.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        logger.warning("Muse shutdown phase: %s control_tasks", self.side)
        await asyncio.gather(*self.control_tasks, return_exceptions=True)
        logger.warning("Muse shutdown phase: %s resources", self.side)
        if getattr(self, "paired_voice", None) is not None and not await self.paired_voice.close():
            raise RuntimeError("speech Provider release remains unconfirmed; ownership retained")
        observation_close = await self.observations.close()
        await self.runtime.close()
        await self.client.aclose()
        if observation_close.get("confirmed") is not True and not (
            observation_close.get("lock_released") is True
            and observation_close.get("pending_callbacks") == 0
            and not observation_close.get("recovery_committing")
        ):
            raise RuntimeError("observation resources are still closing; worker ownership retained")
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None
        logger.warning("Muse shutdown phase: %s complete", self.side)
