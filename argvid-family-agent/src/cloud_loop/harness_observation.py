"""Durable cloud control of an installed edge observation Gateway.

This transports task configuration, not model history or media bytes. No default
installation pretends an unadmitted audiovisual model is ready.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal

import httpx
from pydantic import Field, create_model

from .contracts import Contract, canonical, digest
from .observation_attachment import AttachObservationMedia, ObservationAttachments
from .observation_contracts import (
    Counter,
    ObservationCommand,
    ObservationConfig,
    ObservationRun,
    Reference,
    Version,
)
from .observation_evidence import EvidenceSearch, PeriodReview
from .observation_defaults import (
    ConfigureDefaultObservation,
    DefaultObservationRequest,
    ReadDefaultObservation,
)
from .observation_semantics import (
    ControlObservationTask,
    ListObservationExecutors,
    StartObservation,
)
from .store import Conflict
from .raw_retention import ConfigureRawRetention
from .observation_candidates import ReviewObservationCandidates
from .media_storage import (
    CollectMediaStorage,
    MediaStorageQuery,
    MediaStorageUpdate,
    ProtectMediaFile,
)

# Reuse the authoritative configuration field constraints, excluding identity
# assigned by the receiving node. Full config validators also run on receive.
ObservationSpec = create_model(
    "ObservationSpec",
    __base__=Contract,
    **{
        key: (field.annotation, deepcopy(field))
        for key, field in ObservationConfig.model_fields.items()
        if key not in {"principal", "session_ref"}
    },
)


class ConfigureObservation(Contract):
    config: ObservationSpec
    expected_version: Counter = 0


class ControlObservation(Contract):
    task_id: Reference
    version: Version
    generation: Counter
    action: Literal["start", "pause", "resume", "stop"]


class AnalysisControl(ObservationRun):
    request_id: Reference
    action: Literal["pause", "resume"]


class QueryObservation(Contract):
    health_after: Counter = 0
    task_id: Reference | None = None
    limit: int = Field(default=20, ge=1, le=100)


class ObservationMediaQuery(Contract):
    project_id: Reference


class ObservationMarkerQuery(Contract):
    decision: Literal["keep", "discard", "uncertain"] | None = None
    task_id: Reference
    after_sequence: int = Field(default=-1, strict=True, ge=-1)
    limit: int = Field(default=100, strict=True, ge=1, le=500)


class ObservationEnvelope(Contract):
    request_id: Reference
    principal: Reference
    operation: Literal["configure", "control", "start_executor"]
    configuration: ConfigureObservation | None = None
    control: ControlObservation | None = None
    start: StartObservation | None = None

    def arguments(self):
        if (
            self.operation == "start_executor"
            and self.start is not None
            and self.configuration is None
            and self.control is None
        ):
            return self.start
        if self.start is not None:
            raise ValueError("named executor request cannot mix low-level configuration")
        if (
            self.operation == "configure"
            and self.configuration is not None
            and self.control is None
        ):
            return self.configuration
        if self.operation == "control" and self.control is not None and self.configuration is None:
            return self.control
        raise ValueError("observation operation and body disagree")


TOOLS = {
    "protect_media_file": (
        ProtectMediaFile,
        "按read_media_storage返回的池和文件名设置或取消人工保护，收藏文件不参加自动回收。",
    ),
    "read_media_storage": (
        MediaStorageQuery,
        "读取存储现状。查询视频数量或空间时优先target=local，query_summary给出现存原片/候选/已批准成片数量，以及分开的全机可用空间、共享额度余量和原片整理水位。registered_files不等于已批准成片数；GB为十进制。原片回收历史另用read_recording_policy。",
    ),
    "configure_media_storage": (
        MediaStorageUpdate,
        "按期望版本更新派生媒体/成片池的空间与保留策略。automatic_delete决定是否按额度/期限回收已登记且未使用、未保护的文件。",
    ),
    "collect_media_storage": (
        CollectMediaStorage,
        "按已配置策略回收指定池的已登记文件，返回实际删除清单与剩余空间；在用、人工保护、未登记历史文件保留。",
    ),
    "list_input_devices": (
        ReadDefaultObservation,
        "只读扫描端侧实际摄像头/声卡节点与采集能力，说明手机需要配对和浏览器授权；扫描不会打开录像或自动绑定。capture_verified=false不能说设备已录制成功，平台视频节点不能直接称USB摄像头。",
    ),
    "read_recording_policy": (
        ReadDefaultObservation,
        "读取端侧受管原录像池配置、版本、最近容量测量usage及回收历史history。history含累计回收数量/字节和最近20条原片范围；usage.expired仅是最近一次清理结果。读取不会触发删除。回收范围是已登记的受管录制副本。",
    ),
    "configure_recording_policy": (
        ConfigureRawRetention,
        "配置端侧原录像池的容量、保留年龄或双触发策略。100GB为100000000000字节，可修改；命中、待定及正在读取不删除。用户已经允许未分析旧片过期时可沿用allow_unanalysed_expiry；没有该授权先澄清。启用后将实际回收最旧未保护录像，不能用于删除导入原片。先read_recording_policy读取版本。",
    ),
    "read_default_observation": (
        ReadDefaultObservation,
        "读取端侧默认观察任务、配置版本和实际状态。运行时段与录像保留时间是不同配置；os_boot_verified=false表示未验证系统上电。",
    ),
    "configure_default_observation": (
        ConfigureDefaultObservation,
        "把已安装并已登记的观察任务设为默认任务，启用后由端侧服务启动或在指定时区时段恢复。先读取当前默认配置及任务版本。停用会暂停该默认任务；人工暂停和停止不会自动复活。hours省略表示全天。应用回执不是正在运行或已完成系统开机安装的证明。",
    ),
    "search_observation_evidence": (
        EvidenceSearch,
        "按目标词、物体引用、来源/地点及录像实际时间跨任务检索观察证据。找钥匙应先查此工具而不是默认新建录像。返回候选、project_id、media_ref和原媒体范围；task_id是观察任务ID，用query_observation查询，不能传给业务get_task_context。剪辑另以save_task(kind=edit)取得业务task_id。候选不证明物体身份或不存在；Mock与时间未知必须如实说明。",
    ),
    "review_observation_period": (
        PeriodReview,
        "查询明确时区起止范围内各来源的录像覆盖、已观察/未分析/未录到时间及分页事件证据；用于最近24小时回顾。先get_time确定边界，按next_after_id翻页。没有录到不表示没有发生事情，摘要不能补造，工具不会剪辑或唤醒端侧模型。",
    ),
    "start_observation": (
        StartObservation,
        "按名称启动已安装的家庭观察执行单元。先查询目录task_contract：Qwen提交decision（三候选），CLIP提交visual_rule(kind=text_similarity，英文positive_text/negative_text及阈值)，BLIP提交visual_rule(kind=visual_question，英文question及keep_answers/discard_answers)；二者只传一个。原生模型不接聊天历史，不支持的能力不能通过改目标冒充。不填写模型路径、工具或存储配置。keep描述包含所有符合目标的情况；discard只描述明确不符合；protect只描述证据不足，不能把符合目标的某种活动单独放到protect。sampling从执行单元目录中的已安装档ID选择；默认standard，支持目录公开的其他档。排队或受理后等待实际启动通知，不轮询。",
    ),
    "list_observation_executors": (
        ListObservationExecutors,
        "查询端侧已安装的命名观察执行单元与能力、采样档和声音限制；不加载模型，不启动任务。",
    ),
    "control_observation_task": (
        ControlObservationTask,
        "scope=task暂停、恢复或停止整个观察；scope=analysis仅暂停/恢复连续录制任务的视觉分析，录制与语音继续，不能用于有限文件回放。视觉异常或积压优先使用analysis范围，不能因语音故障停止录像。适配器查询实际状态并绑定版本/运行代次，失败不投递。暂停不等于停止；用户要求停止时，暂停中的任务也须stop以收尾保存，不需要再次询问。",
    ),
    "configure_observation": (
        ConfigureObservation,
        "向端侧持久提交连续观察配置；收到配置不等于启动。只用已安装的源、模型、工具及存储引用。",
    ),
    "control_observation": (
        ControlObservation,
        "启动、暂停、恢复或停止端侧观察；返回排队不等于动作已完成，查询实际状态。",
    ),
    "query_observation": (
        QueryObservation,
        "按需查询端侧观察状态；离线不推断停止。motion是云台运动控制，不是运动检测；观察停止与视频保存分别确认。",
    ),
    "list_observation_markers": (
        ObservationMarkerQuery,
        "按录像任务读取标记与连续候选。recording_state是实际录制状态，latest_available_sequence是索引末尾；本页末尾不是最新进度。has_more=true须继续翻页或明确只看了部分，不能等下轮把下一页叫新产生的画面。查询不含图片；评价画面需取图。candidate_review_identity直接给出审阅工具所需的录像task_id/task_version，不能用业务剪辑任务编号。",
    ),
    "review_observation_candidates": (
        ReviewObservationCandidates,
        "记录候选审阅结果。task_id/task_version必须从list_observation_markers.candidate_review_identity复制，是录像任务身份，不是剪辑业务任务身份。retain继续保护，release仅释放指定标记的保护，其他任务/人工保护不变，原片之后按既有容量策略回收。传错身份时根据返回的expected_recording修正，不重复原参数。",
    ),
    "list_observation_media": (
        ObservationMediaQuery,
        "按需查询项目在端侧保存的视频索引；不自动上传视频，也不启动端侧模型。",
    ),
    "attach_observation_media": (
        AttachObservationMedia,
        "明确选段后把端侧素材核验并绑定到当前云端edit/goal版本。asset_id是已存在的来源编号，不是新片段名称：raw_input必须逐字复制标记/证据的media_ref，retained复制已导出视频asset_id。task_id/expected_version则是接收素材的云端剪辑业务身份，不是录像身份。可给同一原片实际覆盖内的start_ms/end_ms；跨文件分别绑定。返回的是新的本地asset_id，后续剪辑用此返回值，取段从0开始。找不到原片须重新查真实media_ref，不重复编名或猜版本。已有完整任务沿用并以begin_media_output登记作品，不为绑定另建任务；start_job才冻结配方。查询不传文件，不能覆盖旧固定配方。",
    ),
}
READ_TOOLS = {
    "read_media_storage",
    "list_input_devices",
    "read_recording_policy",
    "read_default_observation",
    "query_observation",
    "list_observation_media",
    "list_observation_executors",
    "list_observation_markers",
    "search_observation_evidence",
    "review_observation_period",
}


def executor_task_id(principal, request_id):
    return "record-" + digest([principal, request_id])[:32]


def marker_completion_text(runtime):
    """Describe confirmed finite replay completion using node-owned mark totals."""
    task = runtime.get("task") or {}
    ledger = task.get("ledger") or {}
    if (
        task.get("status") != "stopped"
        or task.get("reason") != "configured_replay_completed"
        or (task.get("config") or {}).get("output_mode") != "markers"
        or (runtime.get("shutdown") or {}).get("confirmed") is not True
        or ledger.get("unseen_media_ms") != 0
        or ledger.get("unresolved_gap_count") != 0
    ):
        return None
    summary = runtime.get("marker_summary") or {}
    counts = []
    durations = []
    for name in ("keep", "discard", "uncertain"):
        value = summary.get(name)
        if not isinstance(value, dict):
            return None
        count, duration = value.get("count"), value.get("duration_ms")
        if type(count) is not int or type(duration) is not int or min(count, duration) < 0:
            return None
        counts.append(count)
        durations.append(duration)
    total = sum(counts)
    if not total or total != ledger.get("seen_count"):
        return None
    keep, discard, uncertain = (f"{value / 1000:g}" for value in durations)
    return (
        f"本次回放已完成，端侧已停止。已标记{total}批（{sum(durations) / 1000:g}秒）："
        f"保留{keep}秒、不选{discard}秒、待定{uncertain}秒。"
        "原始媒体及已有音轨保留，尚未合并或剪辑。"
    )


@dataclass
class ObservationInstallation:
    gateway: object
    validate: object  # trusted resolver checks all configured resource references
    archive: object | None = None
    retention: object | None = None
    catalogue: object | None = None
    validate_start: object | None = None
    close_resources: object | None = None
    gimbal: object | None = None
    model_status: object | None = None  # read-only async projection; never starts a model
    resolve_start: object | None = None  # registered resources, not model-chosen host paths
    decisions: object | None = None  # input-independent marker index on this node


class ObservationBridge:
    def __init__(self, host, installation=None):
        self.host, self.store = host, host.store
        from .harness_health import RuntimeHealth

        self.health = getattr(host, "health", None) or RuntimeHealth(self.store)
        self.installation = installation
        self.delivery_lock = asyncio.Lock()
        self.command_lock = asyncio.Lock()
        self.monitor_lock = asyncio.Lock()
        self.next_health_poll = 0
        from .observation_notifications import ObservationNotifications

        self.notifications = ObservationNotifications(self)
        from .shared_recording_status import SharedRecordingStatus

        self.shared_recording = SharedRecordingStatus(self)
        self.attachments = ObservationAttachments(host)
        if installation is not None:
            installation.gateway.health_settings = getattr(host, "settings", None)
            if (
                host.side != "edge"
                or installation.gateway.store is not self.store
                or not callable(installation.validate)
            ):
                raise ValueError("observation installation must belong to this edge node")
            if (
                installation.gimbal is not None
                and getattr(installation.gimbal, "store", None) is not self.store
            ):
                raise ValueError("gimbal must use the installed node state")
            if (
                installation.decisions is not None
                and getattr(installation.decisions, "store", None) is not self.store
            ):
                raise ValueError("marker index must use the installed node state")
            if installation.retention is not None and (
                installation.retention.store is not self.store
                or installation.retention.archive is not installation.archive
            ):
                raise ValueError("retention worker must use the installed node archive")
            if installation.retention is not None and installation.retention.on_failure is None:
                installation.retention.on_failure = self._retention_failure
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS observation_outbox (
                id TEXT PRIMARY KEY,principal TEXT NOT NULL,task_id TEXT NOT NULL,body TEXT NOT NULL,
                status TEXT NOT NULL,receipt TEXT,attempts INTEGER NOT NULL DEFAULT 0,
                next_try REAL NOT NULL DEFAULT 0,error TEXT,created REAL NOT NULL)""")
            columns = {row[1] for row in db.execute("PRAGMA table_info(observation_outbox)")}
            if "source_event" not in columns:
                db.execute("ALTER TABLE observation_outbox ADD COLUMN source_event TEXT")
            if "depends_on" not in columns:
                db.execute("ALTER TABLE observation_outbox ADD COLUMN depends_on TEXT")
            if "send_attempted" not in columns:
                db.execute(
                    "ALTER TABLE observation_outbox ADD COLUMN send_attempted INTEGER NOT NULL DEFAULT 0"
                )
                # Older rows did not distinguish a lost/in-flight HTTP reply
                # from never-sent work. Treat their outcome as uncertain.
                db.execute("UPDATE observation_outbox SET send_attempted=1")
            db.execute("""UPDATE observation_outbox SET source_event=(SELECT json_extract(payload,'$.event_id')
                FROM audit WHERE kind='observation.command.queued' AND json_extract(payload,'$.request_id')=observation_outbox.id
                ORDER BY seq LIMIT 1) WHERE source_event IS NULL""")
            db.execute("""CREATE TABLE IF NOT EXISTS observation_withdrawals (
                principal TEXT NOT NULL,request_id TEXT NOT NULL,body TEXT NOT NULL,
                PRIMARY KEY(principal,request_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS observation_remote_tasks (
                principal TEXT NOT NULL,task_id TEXT NOT NULL,source_event TEXT NOT NULL,revision TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,next_poll REAL NOT NULL DEFAULT 0,
                snapshot TEXT,signature TEXT,transition INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(principal,task_id))""")
            if "transition" not in {
                row[1] for row in db.execute("PRAGMA table_info(observation_remote_tasks)")
            }:
                db.execute(
                    "ALTER TABLE observation_remote_tasks ADD COLUMN transition INTEGER NOT NULL DEFAULT 0"
                )
            db.execute("""CREATE TABLE IF NOT EXISTS observation_executor_requests (
                principal TEXT NOT NULL,request_id TEXT NOT NULL,body TEXT NOT NULL,config TEXT NOT NULL,
                apply_request_id TEXT NOT NULL,PRIMARY KEY(principal,request_id))""")

    def capabilities(self):
        value = {
            "installed": self.installation is not None,
            "control_tools_loaded": set(TOOLS) <= set(self.host.binding["tools"]),
            "model_admitted": False if self.installation is None else None,
            "readiness_authority": "per-task actual Gateway startup; no inference from HTTP availability",
        }
        if self.installation and self.installation.catalogue:
            value["catalogue"] = self.installation.catalogue()
        return value

    def _installed(self):
        if self.host.side != "edge" or self.installation is None:
            raise Conflict("edge observation runtime is not installed")
        return self.installation

    async def receive(self, envelope):
        async with self.command_lock:
            return await self._receive(envelope)

    async def _receive(self, envelope):
        envelope = ObservationEnvelope.model_validate(envelope.model_dump())
        args = envelope.arguments()
        if envelope.principal != "local-user":
            raise Conflict("observation principal mismatch")
        installed = self._installed()
        with self.store.connect() as db:
            if db.execute(
                "SELECT 1 FROM observation_withdrawals WHERE principal=? AND request_id=?",
                (envelope.principal, envelope.request_id),
            ).fetchone():
                raise Conflict("observation command was withdrawn")
        if envelope.operation == "start_executor":
            if installed.resolve_start is None:
                raise Conflict("named observation executors are not installed")
            with self.store.connect() as db:
                previous = db.execute(
                    "SELECT * FROM observation_executor_requests WHERE principal=? AND request_id=?",
                    (envelope.principal, envelope.request_id),
                ).fetchone()
            if previous:
                if previous["body"] != canonical(envelope):
                    raise Conflict("executor request identity conflict")
                config = ObservationConfig.model_validate_json(previous["config"])
                apply_id = previous["apply_request_id"]
            else:
                config = installed.resolve_start(
                    args,
                    envelope.principal,
                    executor_task_id(envelope.principal, envelope.request_id),
                )
                if inspect.isawaitable(config):
                    config = await config
                config = ObservationConfig.model_validate(config.model_dump())
                apply_id = "executor-config-" + digest(envelope.request_id)[:40]
                with self.store.connect() as db:
                    db.execute(
                        "INSERT INTO observation_executor_requests VALUES (?,?,?,?,?)",
                        (
                            envelope.principal,
                            envelope.request_id,
                            canonical(envelope),
                            canonical(config),
                            apply_id,
                        ),
                    )
            await installed.gateway.apply(config, request_id=apply_id, expected_version=0)
            result = await installed.gateway.control(
                ObservationCommand(
                    principal=envelope.principal,
                    request_id=envelope.request_id,
                    task_id=config.task_id,
                    version=config.version,
                    generation=0,
                    action="start",
                )
            )
        elif envelope.operation == "configure":
            config = ObservationConfig.model_validate(
                {
                    **args.config.model_dump(),
                    "principal": envelope.principal,
                    "session_ref": "observation:"
                    + digest([envelope.principal, args.config.task_id])[:32],
                }
            )
            with self.store.connect() as db:
                known = db.execute(
                    "SELECT 1 FROM observation_commands WHERE principal=? AND request_id=?",
                    (envelope.principal, envelope.request_id),
                ).fetchone()
            if not known:
                if config.notification_policy != "lifecycle_only":
                    raise Conflict("registered observation notification policies are not installed")
                validated = installed.validate(config)
                if inspect.isawaitable(validated):
                    await validated
            result = await installed.gateway.apply(
                config, request_id=envelope.request_id, expected_version=args.expected_version
            )
        else:
            with self.store.connect() as db:
                known = db.execute(
                    "SELECT 1 FROM observation_commands WHERE principal=? AND request_id=?",
                    (envelope.principal, envelope.request_id),
                ).fetchone()
            if (
                not known
                and args.action in {"start", "resume"}
                and installed.validate_start is not None
            ):
                current = installed.gateway.status(envelope.principal, args.task_id)["task"]
                validation = installed.validate_start(
                    ObservationConfig.model_validate(current["config"])
                )
                if inspect.isawaitable(validation):
                    await validation
            result = await installed.gateway.control(
                ObservationCommand(
                    **args.model_dump(),
                    principal=envelope.principal,
                    request_id=envelope.request_id,
                )
            )
        return {
            "request_id": envelope.request_id,
            "accepted": True,
            "result": result,
            "running_confirmed": False,
            "note": "acceptance receipt; query task for actual live state",
        }

    async def withdraw(self, envelope):
        """Fence late delivery and stop only the run created by this command."""
        envelope = ObservationEnvelope.model_validate(envelope.model_dump())
        args = envelope.arguments()
        if envelope.principal != "local-user":
            raise Conflict("observation principal mismatch")
        installed = self._installed()
        async with self.command_lock:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                effective_request = envelope.request_id
                if envelope.operation == "start_executor":
                    original = db.execute(
                        "SELECT * FROM observation_executor_requests WHERE principal=? AND request_id=?",
                        (envelope.principal, envelope.request_id),
                    ).fetchone()
                    if original and original["body"] != canonical(envelope):
                        raise Conflict("executor withdrawal identity conflict")
                    task_id = executor_task_id(envelope.principal, envelope.request_id)
                    config = (
                        ObservationConfig.model_validate_json(original["config"])
                        if original
                        else None
                    )
                    control = ObservationCommand(
                        principal=envelope.principal,
                        request_id=envelope.request_id,
                        task_id=task_id,
                        version=config.version if config else 1,
                        generation=0,
                        action="start",
                    )
                    expected = {"kind": "control", **control.model_dump(mode="json")}
                    started = db.execute(
                        "SELECT 1 FROM observation_commands WHERE principal=? AND request_id=?",
                        (envelope.principal, envelope.request_id),
                    ).fetchone()
                    if original and not started:
                        effective_request = original["apply_request_id"]
                        expected = {
                            "kind": "apply",
                            "config": config.model_dump(mode="json"),
                            "expected_version": 0,
                        }
                elif envelope.operation == "configure":
                    config = ObservationConfig.model_validate(
                        {
                            **args.config.model_dump(),
                            "principal": envelope.principal,
                            "session_ref": "observation:"
                            + digest([envelope.principal, args.config.task_id])[:32],
                        }
                    )
                    expected = {
                        "kind": "apply",
                        "config": config.model_dump(mode="json"),
                        "expected_version": args.expected_version,
                    }
                    task_id = config.task_id
                else:
                    control = ObservationCommand(
                        **args.model_dump(),
                        principal=envelope.principal,
                        request_id=envelope.request_id,
                    )
                    expected = {"kind": "control", **control.model_dump(mode="json")}
                    task_id = args.task_id
                old = db.execute(
                    "SELECT body FROM observation_withdrawals WHERE principal=? AND request_id=?",
                    (envelope.principal, envelope.request_id),
                ).fetchone()
                if old and old["body"] != canonical(envelope):
                    raise Conflict("withdrawal identity conflict")
                command = db.execute(
                    "SELECT body FROM observation_commands WHERE principal=? AND request_id=?",
                    (envelope.principal, effective_request),
                ).fetchone()
                effect = db.execute(
                    "SELECT * FROM observation_command_effects WHERE principal=? AND request_id=?",
                    (envelope.principal, effective_request),
                ).fetchone()
                if command and json.loads(command["body"]) != expected:
                    raise Conflict("withdrawal differs from accepted command")
                db.execute(
                    "INSERT OR IGNORE INTO observation_withdrawals VALUES (?,?,?)",
                    (envelope.principal, envelope.request_id, canonical(envelope)),
                )
            runtime = None
            if command:
                if effect is None:
                    raise Conflict(
                        "historical command has no effect ownership; query and explicitly control the task"
                    )
                runtime = installed.gateway.status(envelope.principal, task_id)
                task = runtime["task"]
                same_run = (
                    task["version"] == effect["version"]
                    and task["generation"] == effect["generation"]
                )
                owns = same_run and (
                    effect["created_run"]
                    or (
                        effect["created_task"]
                        and task["generation"] == 0
                        and task["status"] == "accepted"
                    )
                )
                if owns and task["status"] != "stopped":
                    await installed.gateway.control(
                        ObservationCommand(
                            principal=envelope.principal,
                            task_id=task_id,
                            version=task["version"],
                            generation=task["generation"],
                            action="stop",
                            request_id="withdraw-" + digest(envelope.request_id)[:40],
                        )
                    )
                    runtime = installed.gateway.status(envelope.principal, task_id)
            return {
                "request_id": envelope.request_id,
                "withdrawn": True,
                "runtime": runtime,
                "note": "late delivery fenced; resource shutdown is reported separately in runtime",
            }

    def has_tasks(self, db, principal):
        return bool(
            db.execute(
                "SELECT 1 FROM observation_outbox WHERE principal=? AND status NOT IN ('rejected','withdrawn') LIMIT 1",
                (principal,),
            ).fetchone()
        )

    def cancel_sources(self, db, principal, event_ids):
        for ident in event_ids:
            db.execute(
                "UPDATE observation_outbox SET status='withdraw_pending',next_try=0 WHERE principal=? AND source_event=? AND status IN ('pending','delivered')",
                (principal, ident),
            )

    def local_query(self, principal, args):
        installed = self._installed()
        if principal != "local-user":
            raise Conflict("observation principal mismatch")
        if args.task_id:
            tasks = [installed.gateway.status(principal, args.task_id)]
        else:
            with self.store.connect() as db:
                ids = [
                    row[0]
                    for row in db.execute(
                        "SELECT task_id FROM observation_tasks WHERE principal=? ORDER BY updated DESC LIMIT ?",
                        (principal, args.limit),
                    )
                ]
            tasks = [installed.gateway.status(principal, ident) for ident in ids]
        if installed.retention is not None:
            for item in tasks:
                item["retention"] = installed.retention.status(item["task"]["task_id"])
        decisions = self._marker_index()
        if decisions is not None:
            for item in tasks:
                if item["task"]["config"].get("output_mode") == "markers":
                    page = decisions.markers(principal, item["task"]["task_id"], limit=1)
                    item["marker_summary"] = page["summary"]
        return {
            "reachable": True,
            "side": "edge",
            "checked_at": self.store.clock(),
            "tasks": tasks,
            "health": self.health.snapshot(principal),
            "health_events": self.health.events(principal, args.health_after),
        }

    def local_markers(self, principal, args):
        self._installed()
        decisions = self._marker_index()
        if principal != "local-user" or decisions is None:
            raise Conflict("marker registry is unavailable")
        options = {"after_sequence": args.after_sequence, "limit": args.limit}
        if args.decision is not None:
            options["decision"] = args.decision
        return decisions.markers(principal, args.task_id, **options)

    def _marker_index(self):
        installed = self._installed()
        if installed.decisions is not None:
            return installed.decisions
        # Compatibility with explicitly injected older installations.
        registry = getattr(self.host, "observation_registry", None)
        return registry.decisions if registry is not None else None

    def marker_frame(self, principal, task_id, marker_id, index=0):
        self._installed()
        from .observation_evidence import ObservationEvidence

        return ObservationEvidence(self.store).frame(principal, task_id, marker_id, index)

    def local_media(self, principal, args):
        installed = self._installed()
        if principal != "local-user":
            raise Conflict("observation principal mismatch")
        if installed.archive is None:
            raise Conflict("observation archive is not installed")
        return {
            "reachable": True,
            "side": "edge",
            "checked_at": self.store.clock(),
            "media": installed.archive.list_media(principal, args.project_id),
        }

    def local_asset(self, principal, args, *, check_active=lambda: None):
        installed = self._installed()
        if args.source_kind == "raw_input":
            return self.host.requested_observation_media.ensure(
                principal, args, check_active=check_active
            )
        if installed.archive is None:
            raise Conflict("observation archive is not installed")
        job = installed.archive.get(principal, args.asset_id)
        if job["project_id"] != args.project_id or job["status"] != "saved":
            raise KeyError(args.asset_id)
        installed.archive.media_path(principal, args.asset_id)
        info = self.store.asset(args.asset_id)["info"]
        return {
            "asset_id": args.asset_id,
            "project_id": args.project_id,
            "sha256": info["asset"]["sha256"],
            "size_bytes": info["asset"]["size_bytes"],
            "simulated": info["simulated"],
            "manifest": info,
        }

    def receipt(self, principal, request_id):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM observation_outbox WHERE id=? AND principal=?",
                (request_id, principal),
            ).fetchone()
        if row is None:
            raise KeyError(request_id)
        return {
            "request_id": row["id"],
            "status": row["status"],
            "receipt": json.loads(row["receipt"]) if row["receipt"] else None,
            "error": row["error"],
        }

    def enqueue(self, event, name, args):
        if event["read_only"]:
            raise Conflict("query cannot authorize observation changes")
        if self.host.side != "cloud":
            raise Conflict("observation control belongs to the cloud user task")
        args = TOOLS[name][0].model_validate(args)
        operation = (
            "start_executor"
            if name == "start_observation"
            else ("configure" if name == "configure_observation" else "control")
        )
        request_id = (
            "observe-"
            + digest(
                {
                    "event": event["id"],
                    "principal": event["principal"],
                    "operation": operation,
                    "args": args.model_dump(),
                }
            )[:40]
        )
        envelope = ObservationEnvelope(
            request_id=request_id,
            principal=event["principal"],
            operation=operation,
            configuration=args if operation == "configure" else None,
            control=args if operation == "control" else None,
            start=args if operation == "start_executor" else None,
        )
        task_id = (
            executor_task_id(event["principal"], request_id)
            if operation == "start_executor"
            else (args.config.task_id if operation == "configure" else args.task_id)
        )
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.store.event(event["id"], event["principal"])["status"] != "running":
                raise Conflict("input was superseded")
            dependency = None
            if operation == "control" and args.action in {"start", "resume"}:
                dependency = db.execute(
                    """SELECT id FROM observation_outbox WHERE principal=? AND task_id=? AND source_event=?
                    AND json_extract(body,'$.operation')='configure' ORDER BY rowid DESC LIMIT 1""",
                    (event["principal"], task_id, event["id"]),
                ).fetchone()
                if dependency:
                    request_id = (
                        "observe-"
                        + digest(
                            {
                                "event": event["id"],
                                "principal": event["principal"],
                                "operation": operation,
                                "args": args.model_dump(),
                                "configuration_request": dependency["id"],
                            }
                        )[:40]
                    )
                    envelope = envelope.model_copy(update={"request_id": request_id})
            inserted = db.execute(
                "INSERT OR IGNORE INTO observation_outbox(id,principal,task_id,body,status,created,source_event) VALUES (?,?,?,?,?,?,?)",
                (
                    request_id,
                    event["principal"],
                    task_id,
                    canonical(envelope),
                    "pending",
                    self.store.clock(),
                    event["id"],
                ),
            ).rowcount
            if inserted and dependency:
                db.execute(
                    "UPDATE observation_outbox SET depends_on=? WHERE id=?",
                    (dependency["id"], request_id),
                )
            if inserted:
                db.execute(
                    "UPDATE observation_remote_tasks SET revision=?,active=1,next_poll=0 WHERE principal=? AND task_id=?",
                    (request_id + ":queued", event["principal"], task_id),
                )
            self.store._audit(
                db,
                event.get("task_id"),
                "observation.command.queued",
                {
                    "event_id": event["id"],
                    "request_id": request_id,
                    "operation": operation,
                    "observation_task_id": task_id,
                },
            )
        result = self.receipt(event["principal"], request_id)
        return {**result, "task_id": task_id} if operation == "start_executor" else result

    async def invoke(self, event, name, args):
        if name in {
            "read_media_storage",
            "configure_media_storage",
            "collect_media_storage",
            "protect_media_file",
        }:
            typed = TOOLS[name][0].model_validate(args)
            if name != "read_media_storage" and event["read_only"]:
                raise Conflict("storage changes require writable work")
            body = typed.model_dump(mode="json", exclude={"target"})
            if typed.target == "local":
                if name == "protect_media_file":
                    return self.host.media_storage.protect(typed)
                if name == "read_media_storage":
                    from .storage_summary import storage_with_summary
                    return await asyncio.to_thread(storage_with_summary, self.host, event["principal"])
                if name == "collect_media_storage":
                    return await asyncio.to_thread(self.host.media_storage.collect, typed.category)
                from .media_storage import ConfigureMediaStorage

                return self.host.media_storage.configure(ConfigureMediaStorage.model_validate(body))
            if name == "read_media_storage":
                response = await self.host.client.get("/harness/media/storage")
            else:
                suffix = {
                    "collect_media_storage": "collect",
                    "protect_media_file": "protect",
                    "configure_media_storage": "policy",
                }[name]
                response = await self.host.client.post(
                    "/harness/media/storage/" + suffix, json=body
                )
            response.raise_for_status()
            return response.json()
        if name == "review_observation_candidates":
            typed = ReviewObservationCandidates.model_validate(args)
            if event["read_only"]:
                raise Conflict("candidate review requires writable work")
            if self.host.side == "edge":
                return self._marker_index().reviews.apply(event["principal"], typed)
            response = await self.host.client.post(
                "/harness/observation/candidates/review", json=typed.model_dump(mode="json")
            )
            if response.status_code == 409:
                # Preserve the actionable, server-authored identity correction.
                error = response.json().get("error", "candidate review conflict")
                if str(error).startswith("candidate_review:"):
                    return {"applied": False, **json.loads(error.removeprefix("candidate_review:"))}
            response.raise_for_status()
            return response.json()
        if name == "list_input_devices":
            TOOLS[name][0].model_validate(args)
            response = await self.host.client.get("/harness/input-media/devices")
            response.raise_for_status()
            return response.json()

        if name in {"read_recording_policy", "configure_recording_policy"}:
            typed = TOOLS[name][0].model_validate(args)
            if name == "configure_recording_policy":
                if event["read_only"] or self.host.side != "cloud":
                    raise Conflict("recording policy changes require cloud user input")
                response = await self.host.client.post(
                    "/harness/recordings/policy", json=typed.model_dump(mode="json")
                )
            else:
                response = await self.host.client.get("/harness/recordings/policy")
            response.raise_for_status()
            return response.json()

        if name in {"read_default_observation", "configure_default_observation"}:
            typed = TOOLS[name][0].model_validate(args)
            if name == "configure_default_observation":
                if event["read_only"] or self.host.side != "cloud":
                    raise Conflict("default policy changes require cloud user input")
                body = DefaultObservationRequest(
                    request_id="default-config-" + digest([event["id"], typed.model_dump()])[:32],
                    configuration=typed,
                ).model_dump(mode="json")
                response = await self.host.client.post("/harness/observation/default", json=body)
            else:
                response = await self.host.client.get("/harness/observation/default")
            response.raise_for_status()
            return response.json()

        if name in {"search_observation_evidence", "review_observation_period"}:
            typed = TOOLS[name][0].model_validate(args)
            if self.host.side == "edge":
                function = (
                    self.host.observation_evidence.search
                    if name == "search_observation_evidence"
                    else self.host.observation_evidence.period
                )
                return function(event["principal"], typed)
            path = "search" if name == "search_observation_evidence" else "period"
            try:
                response = await self.host.client.post(
                    "/harness/observation/evidence/" + path, json=typed.model_dump(mode="json")
                )
                response.raise_for_status()
                return {"reachable": True, **response.json()}
            except httpx.HTTPError:
                return {"reachable": False, "evidence": None, "error": "evidence_index_unavailable"}
        if name == "attach_observation_media":
            return await self.attachments.attach(event, args)
        if name == "control_observation_task":
            typed = ControlObservationTask.model_validate(args)
            if typed.scope == "analysis":
                if typed.action == "stop":
                    raise Conflict("analysis scope supports pause/resume; task stop is explicit")
                current = await self.invoke(event, "query_observation", {"task_id": typed.task_id})
                tasks = current.get("tasks", [])
                if len(tasks) != 1:
                    raise Conflict("analysis control requires confirmed current task")
                task = tasks[0]["task"]
                issue = (
                    event.get("body", {}).get("health")
                    if event.get("kind") == "observation.alert"
                    else None
                )
                if issue and (task["version"], task["generation"]) != (
                    issue.get("version"),
                    issue.get("generation"),
                ):
                    raise Conflict("analysis health event is stale")
                body = AnalysisControl(
                    principal=event["principal"],
                    task_id=typed.task_id,
                    version=task["version"],
                    generation=task["generation"],
                    action=typed.action,
                    request_id="analysis-control-" + digest([event["id"], typed.model_dump()])[:32],
                )
                if self.host.side == "edge":
                    return await self._installed().gateway.control_analysis(body)
                response = await self.host.client.post(
                    "/harness/observation/analysis-control", json=body.model_dump(mode="json")
                )
                response.raise_for_status()
                return response.json()

            with self.store.connect() as db:
                frozen = db.execute(
                    """SELECT body FROM observation_outbox WHERE principal=? AND source_event=? AND task_id=?
                    AND json_extract(body,'$.operation')='control' AND json_extract(body,'$.control.action')=?
                    ORDER BY rowid LIMIT 1""",
                    (event["principal"], event["id"], typed.task_id, typed.action),
                ).fetchone()
            if frozen:
                return self.enqueue(
                    event, "control_observation", json.loads(frozen["body"])["control"]
                )
            state = await self.invoke(event, "query_observation", {"task_id": typed.task_id})
            tasks = state.get("tasks")
            if (
                state.get("reachable") is not True
                or not isinstance(tasks, list)
                or len(tasks) != 1
                or tasks[0]["task"]["task_id"] != typed.task_id
                or tasks[0]["task"]["principal"] != event["principal"]
            ):
                raise Conflict("cannot control observation without confirmed current task state")
            task = tasks[0]["task"]
            if event.get("kind") == "observation.alert":
                issue = event["body"]["health"]
                if (
                    (task["version"], task["generation"])
                    != (issue.get("version"), issue.get("generation"))
                    or task["status"] == "stopped"
                    or (task["status"] == "paused" and not task.get("reason"))
                ):
                    raise Conflict("health control lost its task generation or manual-stop fence")
            return self.enqueue(
                event,
                "control_observation",
                {
                    "task_id": typed.task_id,
                    "action": typed.action,
                    "version": task["version"],
                    "generation": task["generation"],
                },
            )
        if name not in READ_TOOLS:
            return self.enqueue(event, name, args)
        typed = TOOLS[name][0].model_validate(args)
        if name == "list_observation_executors":
            if self.host.side == "edge":
                return {
                    "reachable": True,
                    "executors": (self.capabilities().get("catalogue") or {}).get("executors", []),
                }
            try:
                response = await self.host.client.get("/harness/observation/catalogue")
                response.raise_for_status()
                value = response.json()
                return {
                    "reachable": True,
                    "executors": (value.get("catalogue") or {}).get("executors", []),
                    "checked_at": self.store.clock(),
                }
            except (httpx.HTTPError, ValueError, AttributeError):
                return {
                    "reachable": False,
                    "executors": None,
                    "error": "executor_catalogue_unavailable",
                }
        if self.host.side == "edge":
            if name == "list_observation_markers":
                return self.local_markers(event["principal"], typed)
            projection = self.local_query if name == "query_observation" else self.local_media
            return await asyncio.to_thread(projection, event["principal"], typed)
        endpoint = "query" if name == "query_observation" else "media"
        try:
            if name == "list_observation_markers":
                response = await self.host.client.get(
                    "/harness/observation/markers",
                    params=typed.model_dump(mode="json", exclude_none=True),
                )
            else:
                response = await self.host.client.post(
                    "/harness/observation/" + endpoint, json=typed.model_dump(mode="json")
                )
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ValueError("invalid observation response")
            if name == "list_observation_markers":
                from .recording_time import annotate_markers

                value = await annotate_markers(self.host, event["principal"], value)
            elif name == "query_observation":
                value["evidence_limits"] = {
                    "recovery_tests_provided": False,
                    "restart_initiator_provided": False,
                    "meaning": "本查询不提供恢复测试或重启发起者证据；不能凭运行中/代次递增/旧模型总结宣称自动恢复已验收。",
                    "coverage": "missing_media_ms=0只表示账本没有登记媒体缺失，不证明墙钟时间上从未停录；gap_count不是缺口持续时间。",
                }
                for entry in value.get("tasks", []):
                    ledger = entry["task"].get("ledger", {})
                    ledger["coverage_scope"] = "all_recording_generations_not_current_run_only"
                    received = ledger.get("latest_ingested")
                    seen = ledger.get("latest_seen")
                    same_segment = bool(received and seen and
                                        received["segment_id"] == seen["segment_id"])
                    entry["latest_progress"] = {
                        "same_segment": same_segment,
                        "head_lag_ms": max(0, received["end_ms"] - seen["end_ms"])
                        if same_segment else None,
                        "meaning": "latest-position lag only; not proof that all earlier footage was judged",
                    }
            return value
        except httpx.HTTPStatusError as exc:
            return {
                "reachable": True,
                "available": False,
                "side": "edge",
                "checked_at": self.store.clock(),
                "error": "observation_service_rejected_query",
                "http_status": exc.response.status_code,
                "task_state_unknown": True,
            }
        except ValueError:
            return {
                "reachable": True,
                "available": False,
                "side": "edge",
                "checked_at": self.store.clock(),
                "error": "invalid_observation_response",
                "task_state_unknown": True,
            }
        except httpx.HTTPError:
            return {
                "reachable": False,
                "side": "edge",
                "checked_at": self.store.clock(),
                "error": "observation_query_unavailable",
                "task_state_unknown": True,
            }

    async def deliver_once(self):
        if self.host.side != "cloud":
            return False
        async with self.delivery_lock:
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("""UPDATE observation_outbox SET status='withdraw_pending',next_try=0
                    WHERE status IN ('pending','delivered') AND (source_event IS NULL OR EXISTS
                    (SELECT 1 FROM harness_events e WHERE e.id=source_event AND e.status='superseded'))""")
                row = db.execute(
                    """SELECT * FROM observation_outbox o WHERE status IN ('pending','withdraw_pending') AND next_try<=?
                    AND NOT EXISTS (SELECT 1 FROM observation_outbox earlier WHERE earlier.task_id=o.task_id
                    AND earlier.status IN ('pending','withdraw_pending') AND earlier.rowid<o.rowid) ORDER BY rowid LIMIT 1""",
                    (self.store.clock(),),
                ).fetchone()
                if row and row["status"] == "pending":
                    db.execute(
                        "UPDATE observation_outbox SET send_attempted=1 WHERE id=?", (row["id"],)
                    )
                if row and row["status"] == "pending" and row["depends_on"]:
                    dependency = db.execute(
                        "SELECT status FROM observation_outbox WHERE id=?", (row["depends_on"],)
                    ).fetchone()
                    if dependency is None or dependency["status"] != "delivered":
                        # A previously attempted start may have reached edge
                        # despite a lost response. Reconcile/withdraw it rather
                        # than erasing that uncertainty as "never sent".
                        status = "withdraw_pending" if row["send_attempted"] else "rejected"
                        db.execute(
                            "UPDATE observation_outbox SET status=?,send_attempted=?,next_try=0,error='configuration_not_accepted' WHERE id=?",
                            (status, row["send_attempted"], row["id"]),
                        )
                        self._notice(
                            db,
                            row,
                            "dependency:" + row["id"],
                            "观察配置未获确认，正在撤回尚未确认的启动操作。"
                            if row["send_attempted"]
                            else "观察配置未获确认，因此没有启动观察。",
                            {"command_status": status, "request_id": row["id"]},
                        )
                        return True
            if row is None:
                return False
            withdrawing = row["status"] == "withdraw_pending"
            try:
                envelope = ObservationEnvelope.model_validate_json(row["body"])
                if withdrawing and not row["send_attempted"]:
                    receipt = {"request_id": row["id"], "withdrawn": True, "never_sent": True}
                else:
                    endpoint = "withdraw" if withdrawing else "commands"
                    response = await self.host.client.post(
                        "/harness/observation/" + endpoint, json=envelope.model_dump(mode="json")
                    )
                    response.raise_for_status()
                    receipt = response.json()
                    if (
                        receipt.get("request_id") != row["id"]
                        or receipt.get("withdrawn" if withdrawing else "accepted") is not True
                    ):
                        raise ValueError("invalid observation receipt")
                status, error = ("withdrawn" if withdrawing else "delivered"), None
            except (httpx.HTTPError, ValueError) as exc:
                permanent = isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {
                    400,
                    401,
                    403,
                    404,
                    409,
                    422,
                }
                status, error = (
                    (
                        ("withdraw_failed" if permanent else "withdraw_pending")
                        if withdrawing
                        else ("rejected" if permanent else "pending")
                    ),
                    type(exc).__name__,
                )
                receipt = None
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                origin = db.execute(
                    "SELECT status FROM harness_events WHERE id=?", (row["source_event"],)
                ).fetchone()
                if not withdrawing and (origin is None or origin["status"] == "superseded"):
                    status = "withdraw_pending"
                db.execute(
                    "UPDATE observation_outbox SET status=?,receipt=?,attempts=attempts+1,next_try=?,error=? WHERE id=?",
                    (
                        status,
                        canonical(receipt) if receipt is not None else None,
                        self.store.clock()
                        + (
                            0
                            if status == "withdraw_pending" and not withdrawing
                            else min(60, 2 ** min(row["attempts"], 6))
                        ),
                        error,
                        row["id"],
                    ),
                )
                if status == "delivered" or (
                    status == "withdrawn" and receipt.get("runtime") is not None
                ):
                    db.execute(
                        """INSERT INTO observation_remote_tasks(principal,task_id,source_event,revision)
                        VALUES (?,?,?,?) ON CONFLICT(principal,task_id) DO UPDATE SET
                        source_event=excluded.source_event,revision=excluded.revision,active=1,next_poll=0""",
                        (
                            row["principal"],
                            row["task_id"],
                            row["source_event"],
                            row["id"] + ":" + status,
                        ),
                    )
                if status in {"rejected", "withdraw_failed"}:
                    self._notice(
                        db,
                        row,
                        "command:" + row["id"] + ":" + status,
                        "端侧未确认这次观察操作，请查询当前状态后处理。",
                        {"command_status": status, "request_id": row["id"]},
                    )
            return True

    def _notice(self, db, origin, signature, text, state, *, phase=None):
        self.store._audit(db, None, "observation.lifecycle.status", {
            "observation_task_id": origin["task_id"], "signature": signature,
            "text": text, "observed_state": state, "model_invoked": False,
        })
        event = db.execute(
            "SELECT * FROM harness_events WHERE id=? AND principal=?",
            (origin["source_event"], origin["principal"]),
        ).fetchone()
        if event is None:
            return  # Shared service transitions have no waiting human command.
        command_id = None
        if not signature.startswith("command:"):
            # A user's explicit start/stop still needs a real outcome. Later
            # disconnections/recoveries are not repeated answers to that command.
            revision = dict(origin).get("revision", "")
            command_id = revision.rsplit(":", 1)[0]
            command = db.execute(
                "SELECT body FROM observation_outbox WHERE id=? AND principal=? AND source_event=?",
                (command_id, origin["principal"], event["id"]),
            ).fetchone()
            if event["kind"] not in {"user", "voice"} or not command:
                return
            completed = any(marker_completion_text(r) for r in state.get("tasks", []))
            prior = db.execute(
                "SELECT body FROM harness_events WHERE kind='task.notification' AND principal=? "
                "AND json_extract(body,'$.command_request_id')=? ORDER BY rowid DESC LIMIT 1",
                (origin["principal"], command_id),
            ).fetchone()
            old_phase = json.loads(prior["body"]).get("command_phase") if prior else None
            if phase in {"disconnected", "unavailable"} or (
                prior and not completed and not (old_phase == "stopping" and phase == "stopped")
            ):
                return
            if completed:
                signature = "completion:" + command_id
            else:
                signature = "command-outcome:" + command_id + ":" + str(phase)
        session = (
            event["session_key"] or self.store.conversations.main(origin["principal"], db=db)["id"]
        )
        ident = (
            "observation-notice-" + digest([origin["principal"], origin["task_id"], signature])[:32]
        )
        body = {
            "text": text,
            "origin_event_id": event["id"],
            "conversation_id": session,
            "observation_task_id": origin["task_id"],
            "source": "observation_lifecycle",
            "command_request_id": command_id, "command_phase": phase,
        }
        previous = db.execute("SELECT 1 FROM harness_events WHERE id=?", (ident,)).fetchone()
        if previous:
            return
        self.store.submit(
            ident,
            origin["principal"],
            event["channel"],
            "task.notification",
            body,
            read_only=True,
            _db=db,
        )
        reply = {
            "text": text,
            "event_id": ident,
            "principal": origin["principal"],
            "session_key": session,
            "model_invoked": False,
            "source": "observation_lifecycle",
            "observation_task_id": origin["task_id"],
            "observed_state": state,
            "audio_generated": False,
            "simulated": event["channel"] == "voice",
        }
        for runtime in state["tasks"] if isinstance(state.get("tasks"), list) else []:
            if isinstance(runtime, dict) and (
                runtime.get("source_simulated") is True
                or runtime.get("inference_simulated") is True
            ):
                reply["simulated"] = True
        db.execute(
            "UPDATE harness_events SET status='done',session_key=?,reply=?,finished=? WHERE id=?",
            (session, canonical(reply), self.store.clock(), ident),
        )
        self.host.voice.notifications.enqueue(db, event, reply)
        self.store._audit(
            db,
            None,
            "observation.lifecycle.notice",
            {
                "event_id": ident,
                "origin_event_id": event["id"],
                "observation_task_id": origin["task_id"],
                "model_invoked": False,
                "signature": signature,
            },
        )

    async def poll_health_once(self):
        if self.host.side != "cloud":
            return False
        if self.store.clock() >= self.next_health_poll:
            self.next_health_poll = self.store.clock() + self.host.settings.health_poll_seconds
            try:
                response = await self.host.client.post(
                    "/harness/observation/health",
                    json={"health_after": self.health.cursor("local-user")},
                )
                response.raise_for_status()
                health_state = response.json()
            except (httpx.HTTPError, ValueError):
                return False
            if health_state.get("reachable"):
                journal = health_state.get("health_events", {})
                # Fault transitions remain deliverable even after recovery or
                # disconnection. Backlog uses current sustained state instead.
                records = journal.get("events", [])
                self.health.enqueue_cloud(
                    "local-user",
                    [
                        r
                        for r in records
                        if r.get("code") != "analysis_backlog"
                        or r.get("details", {}).get("sustained") is True
                    ],
                    enabled=self.host.settings.health_cloud_handling,
                    max_actions_per_hour=self.host.settings.health_max_actions_per_hour,
                )
                self.health.enqueue_cloud(
                    "local-user",
                    [
                        r
                        for r in health_state.get("health", [])
                        if r.get("code") == "analysis_backlog"
                        and r.get("details", {}).get("sustained") is not True
                    ],
                    enabled=self.host.settings.health_cloud_handling,
                    max_actions_per_hour=self.host.settings.health_max_actions_per_hour,
                    sustain_seconds=self.host.settings.health_backlog_sustain_seconds,
                )
                self.health.cursor(
                    "local-user", journal.get("cursor", self.health.cursor("local-user"))
                )

    async def poll_once(self):
        """Bounded node status polling; no media upload, no model wakeup."""
        if self.host.side != "cloud":
            return False
        async with self.monitor_lock:
            with self.store.connect() as db:
                row = db.execute(
                    """SELECT * FROM observation_remote_tasks r WHERE active=1 AND next_poll<=?
                    AND NOT EXISTS (SELECT 1 FROM observation_outbox o WHERE o.principal=r.principal AND o.task_id=r.task_id
                                    AND o.status IN ('pending','withdraw_pending'))
                    ORDER BY next_poll,task_id LIMIT 1""",
                    (self.store.clock(),),
                ).fetchone()
            if row is None:
                return False
            state = await self.invoke(
                {"principal": row["principal"]}, "query_observation", {"task_id": row["task_id"]}
            )
            phase = "unavailable"
            text = "端侧可连接，但暂时无法确认观察状态。"
            terminal = False
            identity = None
            if state.get("reachable") is False:
                phase = "disconnected"
                text = "暂时无法连接端侧，不能据此判断观察任务已停止。"
            elif (
                isinstance(state.get("tasks"), list)
                and len(state["tasks"]) == 1
                and isinstance(state["tasks"][0], dict)
                and isinstance(state["tasks"][0].get("task"), dict)
                and state["tasks"][0]["task"].get("task_id") == row["task_id"]
                and state["tasks"][0]["task"].get("principal") == row["principal"]
            ):
                runtime = state["tasks"][0]
                task = runtime.get("task") or {}
                identity = [task.get("version"), task.get("generation")]
                status = task.get("status")
                shutdown = runtime.get("shutdown") or {}
                if status == "running" and runtime.get("worker_active") is True:
                    phase = "running"
                    text = "端侧观察已启动。"
                    if runtime.get("inference_simulated") is True:
                        text = "端侧模拟观察已启动，未运行真实识别模型。"
                    elif runtime.get("source_simulated") is True:
                        text = "端侧已开始观察回放输入，未接入真实摄像头。"
                elif status == "paused":
                    planned_pause = task.get("reason") in {
                        None,
                        "default_disabled",
                        "default_schedule_closed",
                        "service_shutdown",
                        "paired_input_disconnected",
                        "host restarted; resource recovery required before resume",
                    }
                    phase = "paused" if planned_pause else "fault"
                    text = (
                        "端侧观察已暂停。"
                        if planned_pause
                        else "端侧观察因异常暂停，请检查输入或模型服务。"
                    )
                    if str(task.get("reason") or "").startswith("storage:"):
                        text = "端侧观察因保存异常暂停，请检查存储空间或素材完整性。"
                    if task.get("reason") == "PairedInputDisconnected":
                        text = "设备输入连接中断，观察已暂停；设备重新连接后可按默认任务恢复。"
                elif status == "stopped":
                    terminal = shutdown.get("confirmed") is True or task.get("generation") == 0
                    phase = "stopped" if terminal else "stopping"
                    text = (
                        "端侧观察已停止。" if terminal else "已禁止新的观察动作，仍在等待停止确认。"
                    )
                    if task.get("generation") == 0:
                        text = "观察任务已停止；没有启动过采集资源。"
                    elif terminal:
                        text = marker_completion_text(runtime) or text
                elif status in {"accepted", "starting"}:
                    phase = status
                    text = None
                if task.get("reason"):
                    identity.append(task["reason"])
            signature = digest([phase, identity])
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT revision,signature,transition FROM observation_remote_tasks WHERE principal=? AND task_id=?",
                    (row["principal"], row["task_id"]),
                ).fetchone()
                # An accepted newer command supersedes an older in-flight query.
                if current is None or current["revision"] != row["revision"]:
                    return False
                if db.execute(
                    "SELECT 1 FROM observation_outbox WHERE principal=? AND task_id=? AND status IN ('pending','withdraw_pending') LIMIT 1",
                    (row["principal"], row["task_id"]),
                ).fetchone():
                    return False
                changed = current["signature"] != signature
                transition = current["transition"] + int(changed)
                if changed and text:
                    self._notice(db, row, signature + ":" + str(transition), text, state, phase=phase)
                db.execute(
                    """UPDATE observation_remote_tasks SET snapshot=?,signature=?,transition=?,active=?,next_poll=?
                    WHERE principal=? AND task_id=?""",
                    (
                        canonical(state),
                        signature,
                        transition,
                        int(not terminal),
                        self.store.clock() + self.host.settings.observation_status_fallback_seconds,
                        row["principal"],
                        row["task_id"],
                    ),
                )
            if phase == "running" and task.get("ledger", {}).get("batch_count", 0):
                self.store.scheduler.observe_ready(row["principal"], row["task_id"])
            return True

    async def close(self):
        if self.installation:
            gateway = await self.installation.gateway.close()
            parts = {"gateway": gateway}
            if self.installation.retention is not None:
                parts["retention"] = await self.installation.retention.close()
            if self.installation.close_resources is not None:
                parts["installation"] = await self.installation.close_resources()
            if len(parts) == 1:
                return gateway
            return {
                "confirmed": all(p["confirmed"] for p in parts.values()),
                "lock_released": all(p["lock_released"] for p in parts.values()),
                "pending_callbacks": sum(p["pending_callbacks"] for p in parts.values()),
                "recovery_committing": gateway["recovery_committing"],
                **parts,
            }
        return {"confirmed": True, "not_installed": True}

    async def retention_once(self):
        if self.installation is None or self.installation.retention is None:
            return False
        return await self.installation.retention.tick()

    async def _retention_failure(self, result):
        installed = self._installed()
        task = installed.gateway.status(result["principal"], result["task_id"])["task"]
        if task["version"] != result["version"] or task["status"] not in {"starting", "running"}:
            return
        self.health.record(
            result["principal"],
            "observation_task",
            "task_failed",
            task_id=result["task_id"],
            version=task["version"],
            generation=task["generation"],
            details={"error_type": result["error_type"], "component": "retention"},
        )
        await installed.gateway.control(
            ObservationCommand(
                principal=result["principal"],
                task_id=result["task_id"],
                version=task["version"],
                generation=task["generation"],
                action="pause",
                reason="storage:" + result["error_type"],
                request_id="storage-pause-"
                + digest(
                    [result["event_id"], task["version"], task["generation"], result["error_type"]]
                )[:32],
            )
        )

    async def recover(self):
        if self.installation is None:
            return
        with self.store.connect() as db:
            tasks = [
                dict(row) for row in db.execute("SELECT principal,task_id FROM observation_tasks")
            ]
        for task in tasks:
            await self.installation.gateway.recover(task["principal"], task["task_id"])
