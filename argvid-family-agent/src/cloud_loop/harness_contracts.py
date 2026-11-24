"""Shared edge/cloud functional contracts, separate from legacy video-only RPC."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator, model_validator

from .contracts import ID, CapturePackage, Contract, Invocation
from .autonomy_contracts import ReadAutonomy, ConfigureAutonomy
from .harness_media_contracts import MediaProject, MediaRecipe, FrameRead, SourceQuery
from .harness_visual_review import InspectMediaFrames
from .harness_media_review import ReviewRenderedMedia
from .sampling_lookup import SamplingLookupQuery
from .voice_contracts import VoiceUtterance, validate_streaming_utterance
from .voice_reply import ReplyWithVoice
from .harness_delivery import DeliveryQuery, DeliveryUpdate, AttentionRequest


class UserInput(Contract):
    event_id: str = Field(pattern=ID)
    text: str = Field(min_length=1)
    task_id: str | None = Field(default=None, pattern=ID)
    interaction_id: str | None = Field(
        default=None,
        pattern=ID,
        description="Input grouping only; does not rotate the main conversation.",
    )
    conversation_id: str | None = Field(default=None, pattern=ID)
    mode: Literal["query", "interact"] = "interact"
    new_session: Literal[False] = False


class VoiceInput(UserInput):
    observed_at: datetime
    valid_for_ms: int = Field(default=60000, ge=1, le=60000)
    source_ref: str = Field(min_length=1, max_length=400)
    simulated: bool = True
    purpose: Literal["user_command", "media_content"] = "user_command"
    _aware = field_validator("observed_at")(Invocation.aware.__func__)


class PeerMessage(Contract):
    protocol: Literal["cix.harness/1"] = "cix.harness/1"
    id: str = Field(pattern=ID)
    correlation_id: str = Field(pattern=ID)
    sender: Literal["edge", "cloud"]
    recipient: Literal["edge", "cloud"]
    principal: str = Field(pattern=ID)
    kind: Literal["query", "analysis", "task", "result", "control"]
    text: str = Field(max_length=8000)
    task_id: str | None = Field(default=None, pattern=ID)
    task_version: int | None = Field(default=None, ge=1)
    sent_at: datetime
    expires_at: datetime
    hops: int = Field(default=0, ge=0, le=4)
    payload: dict = Field(default_factory=dict)
    phase: Literal["request", "accepted", "completed", "failed", "cancelled", "paused"] = "request"
    _aware = field_validator("sent_at", "expires_at")(Invocation.aware.__func__)

    @model_validator(mode="after")
    def direction_and_expiry(self):
        if self.sender == self.recipient or self.expires_at <= self.sent_at:
            raise ValueError("invalid peer direction or expiry")
        return self


class AssetTransfer(Contract):
    request_id: str = Field(pattern=ID)
    principal: str = Field(pattern=ID)
    package: CapturePackage
    content_base64: str = Field(min_length=1, max_length=40_000_000)


class EmptyArgs(Contract):
    pass


class CapabilityQuery(Contract):
    target: Literal["local", "peer"] = "local"


class WorkMessage(Contract):
    task_id: str = Field(pattern=ID)
    text: str = Field(min_length=1, max_length=4000)
    request_kind: Literal["instruction", "question"] = Field(
        default="instruction",
        description="转交当前用户的明确提问时用question：答复即时回原主对话，不受普通汇报周期影响；普通交办、后台进度用instruction。",
    )


class TimeArgs(Contract):
    timezone: str = Field(default="Asia/Shanghai", max_length=80)


class ContextQuery(Contract):
    task_id: str | None = Field(default=None, pattern=ID)


class ConversationQuery(Contract):
    conversation_id: str = Field(pattern=ID)
    before_sequence: int | None = Field(default=None, ge=1)
    limit: int = Field(default=100, ge=1, le=100)


class SkillScriptArgs(Contract):
    script: Literal["camera-workflow/candidate-summary"]
    input_file: str = Field(
        min_length=1,
        max_length=300,
        description="工作区内JSON文件路径，含candidates列表，每项id/source_ref/duration_ms。",
    )


class SearchArgs(Contract):
    match_mode: Literal["any", "all"] = Field(
        default="any",
        description="默认任一关键词命中并按匹配词数排序，适合中英文同义词；需要每个词都出现时选all。偏好名称优先查memory。",
    )
    query: str = Field(default="", max_length=160)
    collection: Literal["tasks", "history", "memory", "all"] = "all"
    limit: int = Field(default=5, ge=1, le=10)
    include_generated: bool = Field(
        default=False, description="仅在用户询问模型之前说过什么时设true；生成回复不是执行事实。"
    )


class ReadRecordArgs(Contract):
    collection: Literal["history", "memory"]
    record_id: str = Field(min_length=1, max_length=200)
    offset: int = Field(default=0, ge=0)
    max_chars: int = Field(default=2000, ge=1, le=3000)


class RememberArgs(Contract):
    key: str = Field(min_length=1, max_length=100)
    text: str = Field(min_length=1, max_length=1000)
    memory_id: str | None = Field(default=None, pattern=ID)
    expected_version: int | None = Field(default=None, ge=1)
    supersedes: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="用户要求统一/覆盖同一事项时，被此条替代的重复记忆ID。原历史保留。",
    )


class SaveTaskArgs(Contract):
    creation_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="同一输入可创建多个事项。新建不同事项使用不同creation_key，重试同一事项复用该值。更新已有事项使用task_id和expected_version。",
    )
    task_id: str | None = Field(default=None, pattern=ID)
    expected_version: int | None = Field(default=None, ge=1)
    kind: Literal["goal", "capture", "compose", "report", "edit"] = Field(
        description="edit=模型自主制作，成片后完成；compose=已有明确配方的制作；goal=持续/周期事项；report=文字报告。持续录像使用start_observation。capture为兼容的回放素材作业，已有录像取段不属于新拍摄。"
    )
    goal: str = Field(min_length=1, max_length=2000)
    duration_ms: int | None = Field(
        default=None,
        ge=100,
        description="用户明确指定的成片时长；未指定时留空，由模型选段或项目时间轴决定。",
    )
    media_recipe: MediaRecipe | None = None
    no_motion: bool = True
    requires_capture: bool = Field(
        default=False,
        description="仅用户明确要求新采集/补采再制作时为true；单纯裁剪已有视频或明确禁止补拍时必须为false；合成必须实际使用当前事项核验过的采集素材，不能直接绕过采集读取源片。",
    )
    start_paused: bool = Field(
        default=False,
        description="仅用于新任务：用户明确要求先保存但不执行时为true。现场条件不满足会自动等待，不等于用户要求暂停。",
    )

    @model_validator(mode="after")
    def output_spec(self):
        if self.media_recipe:
            if self.kind == "report":
                raise ValueError("report cannot contain a media recipe")
            if self.duration_ms is None:
                object.__setattr__(self, "duration_ms", self.media_recipe.duration_ms)
            elif self.duration_ms != self.media_recipe.duration_ms:
                raise ValueError("duration must equal recipe segment sum")
            return self
        if self.kind == "capture" and self.duration_ms != 6000:
            raise ValueError("capture delivers six seconds; a twelve-second film must be compose")
        if self.kind == "compose" and self.duration_ms != 12000:
            raise ValueError("compose delivers twelve seconds; a six-second clip must be capture")
        if self.kind == "report" and self.duration_ms is not None:
            raise ValueError("report has no media duration")
        return self


class TaskRefArgs(Contract):
    task_id: str = Field(pattern=ID)


class BeginMediaOutput(TaskRefArgs):
    expected_version: int = Field(ge=1)
    output_key: str = Field(min_length=1, max_length=120)
    brief: str = Field(min_length=1, max_length=2000)


class StartJobArgs(TaskRefArgs):
    operation: Literal["capture", "compose"] | None = None
    media_project: MediaProject | None = Field(
        default=None,
        description="Remotion project in the agent workspace. The worker snapshots its source and renders the named composition. Registered assets are available as staticFile('<asset_id>.mp4').",
    )
    media_recipe: MediaRecipe | None = Field(
        default=None,
        description="可选的实际作业输入。合成时可把计划中的源选段替换为media_list返回的已核验采集asset_id，必须保持原选段、顺序和输出规格；采集可以选计划的有序子集。",
    )

    @model_validator(mode="after")
    def one_render_input(self):
        if self.media_project and self.media_recipe:
            raise ValueError("provide either a project or a recipe")
        return self


class TaskControlArgs(TaskRefArgs):
    action: Literal["pause", "resume", "cancel"]
    expected_version: int = Field(ge=1)


class PeerRequestArgs(Contract):
    operation: Literal["capture", "compose"] | None = Field(
        default=None,
        description="本次委托的具体操作，不一定是最终目标。先采集再制作的复合事项必须明确：capture仅委托采集子步骤，compose委托完整制作。",
    )
    media_recipe: MediaRecipe | None = None
    transfer_result: bool = Field(
        default=False,
        description="只有本侧后续处理需要真实文件时为true；最终结果默认返回引用，不自动把成片传回端侧。",
    )
    kind: Literal["query", "analysis", "task"]
    question: str = Field(min_length=1, max_length=4000)
    task_id: str | None = Field(default=None, pattern=ID)


class TimerArgs(TaskRefArgs):
    interval_seconds: int = Field(ge=60, le=31536000)
    enabled: bool = True


class WakeRuleQuery(Contract):
    task_id: str | None = Field(default=None, pattern=ID)
    include_deleted: bool = False


class RuleArgs(TaskRefArgs):
    start_on_observation: bool = Field(
        default=False,
        description="先等待绑定观察任务收到实际媒体，再从该时刻开始周期计时，适用于尚待接入的手机。",
    )
    observation_task_id: str | None = Field(
        default=None,
        pattern=ID,
        description="绑定已登记的观察任务；有截止时间时计算末期，持续观察可按周期继续。",
    )
    finalize_grace_seconds: int = Field(default=15, strict=True, ge=0, le=300)
    output_kind: Literal["text", "video", "review"] = Field(
        default="text", description="video允许每期从已有素材建立一个剪辑子任务；不授权新增拍摄。"
    )
    next_wake_in_seconds: int | None = Field(
        default=None, ge=60, le=31536000,
        description="只覆盖下一次间隔唤醒；后续默认间隔不变。review只查看并决定行动，不强制创建影片。",
    )
    end_at: datetime | None = Field(
        default=None, description="周期任务截止时间，带时区；到期触发最后一期后停止重复唤醒。"
    )
    schedule: Literal["daily", "interval", "once"]
    timezone: str = Field(default="Asia/Shanghai", max_length=80)
    local_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    interval_seconds: int | None = Field(default=None, ge=60, le=31536000)
    run_at: datetime | None = None
    enabled: bool = True
    expected_revision: int | None = Field(default=None, ge=1)
    delivery_side: Literal["edge", "cloud"]
    delivery_channel: Literal["web", "voice"] = "web"

    @model_validator(mode="after")
    def schedule_fields(self):
        from zoneinfo import ZoneInfo

        if self.next_wake_in_seconds is not None and (
            self.schedule != "interval" or self.start_on_observation
        ):
            raise ValueError("next wake override requires an already-started interval")

        if self.start_on_observation and not self.observation_task_id:
            raise ValueError("observation start requires a bound observation task")

        try:
            ZoneInfo(self.timezone)
        except Exception:
            raise ValueError("unknown timezone") from None
        if self.observation_task_id and (
            self.schedule != "interval" or self.output_kind != "video"
        ):
            raise ValueError("observation binding requires interval video delivery")
        if self.end_at is not None and (
            self.end_at.tzinfo is None
            or self.end_at.utcoffset() is None
            or self.schedule != "interval"
        ):
            raise ValueError("bounded end_at requires an aware interval schedule")
        if self.schedule == "daily" and (
            self.local_time is None or self.interval_seconds is not None or self.run_at is not None
        ):
            raise ValueError("daily requires only local_time")
        if self.schedule == "interval" and (
            self.interval_seconds is None or self.local_time is not None or self.run_at is not None
        ):
            raise ValueError("interval requires only interval_seconds")
        if self.schedule == "once" and (
            self.run_at is None
            or self.run_at.tzinfo is None
            or self.local_time is not None
            or self.interval_seconds is not None
        ):
            raise ValueError("once requires aware run_at")
        return self


class RuleControlArgs(TaskRefArgs):
    action: Literal["enable", "disable", "delete"]
    expected_revision: int = Field(ge=1)


class PublishReportArgs(TaskRefArgs):
    text: str = Field(
        min_length=1,
        description="面向用户的报告正文，长度与内容根据任务需要决定。定期报告的日期和来源说明由已读取的周期证据提供，引用放source_ids。",
    )
    source_ids: list[str] = Field(
        default_factory=list,
        description="实际读到的本侧记录ID；没有记录时明确报告无可核验进展。",
    )


class ReportWindowQuery(Contract):
    principal: str = Field(pattern=ID)
    start: datetime
    end: datetime
    _aware = field_validator("start", "end")(Invocation.aware.__func__)


class ReportDelivery(Contract):
    id: str = Field(pattern=ID)
    task_id: str = Field(pattern=ID)
    task_version: int = Field(ge=1)
    text: str = Field(min_length=1)
    sources: list[dict]
    source_event: str = Field(pattern=ID)
    occurrence_id: str | None = Field(default=None, pattern=ID)
    sender: Literal["edge", "cloud"]
    recipient: Literal["edge", "cloud"]
    channel: Literal["web", "voice"]
    principal: str = Field(pattern=ID)
    created: float
    period: dict | None = None
    evidence_records: list[dict] = Field(default_factory=list)
    evidence_coverage: dict = Field(default_factory=dict)
    content_origin: Literal["model_generated_report"]
    audio_generated: Literal[False] = False
    simulated: bool

    @model_validator(mode="after")
    def provenance(self):
        if self.simulated != (self.channel == "voice"):
            raise ValueError("voice delivery must be marked simulated")
        return self


class PeerRecordUpdate(Contract):
    resource: Literal["memory", "task"]
    record_id: str = Field(pattern=ID)
    expected_version: int = Field(ge=1)
    action: Literal["replace", "pause", "resume", "cancel"]
    value: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def shape(self):
        if self.resource == "memory" and (self.action != "replace" or self.value is None):
            raise ValueError("memory update requires replace and value")
        if self.resource == "task" and (self.action == "replace" or self.value is not None):
            raise ValueError("task control requires pause/resume/cancel without value")
        return self


class PeerRecordQuery(Contract):
    principal: str = Field(pattern=ID)
    query: SearchArgs


class PeerRecordMutation(Contract):
    id: str = Field(pattern=ID)
    principal: str = Field(pattern=ID)
    update: PeerRecordUpdate
    expires_at: datetime
    _aware = field_validator("expires_at")(Invocation.aware.__func__)


TOOLS = {
    "get_delivery_policy": (
        DeliveryQuery,
        "读取用户汇报配置与待通知成果数。task_id为空读通用偏好，有值读任务覆盖及继承结果。汇报周期不控制任务唤醒、录像或成片入库。",
    ),
    "configure_delivery_policy": (
        DeliveryUpdate,
        "按用户要求保存通用或任务汇报配置；先读取revision。on_result=有正式成果即通知，interval=按用户指定秒数合并，on_request=仅供主动查询；policy=null恢复继承。allow_agent_changes仅用户可授予，后台只能在获授权后修改自己的任务覆盖。不要自行把举例的日/周设为用户要求。",
    ),
    "request_user_attention": (
        AttentionRequest,
        "只有任务确实缺少用户的信息、选择或授权且无法自行继续时使用：交回一个具体question及必要reason。普通错误重试、看图和进度留本任务；同一任务的相同问题不会反复通知。此工具不暂停任务或关闭录像。",
    ),
    "reply_with_voice": (
        ReplyWithVoice,
        "用语音回答当前主对话的语音提问。把完整、简短的口头答复交给本工具；本轮结束后复用已配置合成与板端播放。同一次输入只交付一份，重复调用不会重复播放。requested只表示已登记，不代表已生成声音或用户听到；调用后结束本轮。不用于后台播报或其他对话。",
    ),
    "message_work": (
        WorkMessage,
        "把补充要求或问题交给一个已有任务的工作会话，持久排队后在当前轮结束时继续；不取消当前作业。返回queued只表示消息已保存，不代表工作完成。需要立即暂停/取消仍用task_control。返回target_status为实际状态：已结束事项的新制作要求可由工作会话另建任务执行，不能改写旧任务；不要把已完成作业描述为仍在排队。",
    ),
    "read_autonomy": (
        ReadAutonomy,
        "读取Muse当前自主开关、周期、下次时间、工作文件、人设和最近自主活动。",
    ),
    "configure_autonomy": (
        ConfigureAutonomy,
        "修改Muse自主配置；只改提供字段。用户与获授权模型均可操作。next_wake_in_seconds只安排下一次，interval_seconds修改后续默认周期；enabled启停未来自动触发，不取消独立已提交任务。文件内容可更新，下轮实际读取。",
    ),
    "review_rendered_media": (
        ReviewRenderedMedia,
        "复看成片后批准交付，或在目标不变的前提下请求修正。approve先用inspect_media_frames实际查看当前成片；按candidate_image_review.sampling_policy选择复看方式，model模式由模型决定取样，coverage模式遵守返回的覆盖要求。revise使用新任务版本，原片和旧回执保留，随后重新绑定/复看候选并start_job。不能批准失败作业、旧版本或其他任务的影片。",
    ),
    "lookup_sampling_plan": (
        SamplingLookupQuery,
        "只读查询已保存的概率采样表：窗口5/10/15秒，目标发现概率、有效画面概率h、条件识别概率p各为0.7/0.8/0.9。p是采到有效目标后的正确识别概率，不是总体准确率；须明确独立机会假设。返回最少图数、平均频率及三个CPU模型的估算余量。假设未校准、误留未知，结果不是质量保证或执行准入；Qwen多图联合模式不能直接套用逐图独立结论。",
    ),
    "inspect_media_frames": (
        InspectMediaFrames,
        "读取本人可访问的视频画面，真实图片随工具回执进入模型。纯查看只填写asset_id和positions_ms，可用于只读问答和已完成作品。制作复核时同时填写task_id和expected_version，记录本次版本证据。时间从文件0开始，每次最多12张，可继续读取。",
    ),
    "read_report_period": (
        TaskRefArgs,
        "读取本次播报应覆盖的日期区间、端云作业完成/观察片段保存回执及本期是否已发布；留意总数、截断和离线范围。日期来自当前到期事件，不是主机今天；旧期报告不能替代本期。返回可供publish_report引用的证据ID。",
    ),
    "list_media_sources": (
        SourceQuery,
        "列出本侧或对侧已登记视频来源、真实规格、来源声明和回放位置；target=peer可直接查对侧来源，不需要唤醒对侧模型。可以按source_id读取。返回元数据不等于视觉识别。",
    ),
    "get_source_frame": (
        FrameRead,
        "读取已登记来源或原录像的单张真实画面。source_id与media_ref二选一，端侧原录像可用target=peer。position_ms相对该来源或登记片段的起点，结果另给原时间。支持图像输入时直接附图，纯查看无需创建编辑任务。",
    ),
    "get_capabilities": (
        CapabilityQuery,
        "读取本侧或对侧已装配能力，不调用对侧模型。available_adapters表示已配置能力，unknown_adapters表示待探测；摄像头和麦克风可通过list_input_devices查询。模型图像输入与实体设备状态分别记录。实际任务启动验证服务，simulated不表示可用。需要对侧Agent分析时用peer_request。",
    ),
    "run_skill_script": (
        SkillScriptArgs,
        "执行已审阅、已启用Skill附带的固定脚本；当前可统计候选片段数量/来源/总时长。只读工作区JSON，不允许任意Shell，不作视觉评分。",
    ),
    "set_wake_rule": (
        RuleArgs,
        "为已授权事项登记每天、间隔或一次到期唤醒。规则只唤醒该事项原工作会话，不唤醒主对话或另建目标。review只查看和决定是否行动，不强制成片；interval_seconds是默认周期，next_wake_in_seconds仅覆盖下一次。新建不填revision，修改必须读取当前revision。",
    ),
    "list_wake_rules": (
        WakeRuleQuery,
        "查询本用户唤醒规则及最近执行。可用task_id只查一个事项，include_deleted=true包含已取消的安排，用于核对取消记录；不会运行任务。",
    ),
    "control_wake_rule": (
        RuleControlArgs,
        "停用、启用或删除已授权事项的唤醒规则。删除保留执行历史；不等于取消事项。",
    ),
    "publish_report": (
        PublishReportArgs,
        "提交当前事项文字报告并由此工具交付到指定侧和渠道。正文包含需要用户看到的结果与说明；工作会话结束后的总结保留在工作记录。内容依据实际查询，每期有独立交付记录。voice目前仅模拟播报文本。",
    ),
    "read_record": (
        ReadRecordArgs,
        "按检索返回的真实记录ID分页读取历史或记忆原文，保留来源、偏移和总长度。不读任意路径，不修改记录。",
    ),
    "get_time": (
        TimeArgs,
        "读取本机实际当前时间与指定时区。问时间只需此工具，不用查询任务或委托对侧。",
    ),
    "get_task_context": (
        ContextQuery,
        "按需读取本次事件原始输入/对侧请求及关联任务的当前状态。可指定task_id；返回来源与版本。不执行动作。",
    ),
    "list_conversations": (
        EmptyArgs,
        "列出本侧固定主对话及事项工作对话，包含真实会话ID、任务关联和当前运行状态。",
    ),
    "read_conversation": (
        ConversationQuery,
        "读取本侧指定会话的持久输入、回复和事件关联；这不是当前任务状态，进度需查询任务。",
    ),
    "search_records": (
        SearchArgs,
        "按空格分隔的关键词检索本用户任务、历史或记忆；默认任一词匹配并按命中数排序，返回matched_terms，all模式才要求全部词匹配。query可为空查看最近记录。返回来源编号，排除本轮问题自身；未命中不代表没有记录，可缩短关键词。只读，不启动动作。",
    ),
    "remember": (
        RememberArgs,
        "保存用户明确表达的偏好。创建新键前先检索已有记忆；更新用原memory_id/key及expected_version，避免另造同义键。supersedes可归并同一事项的重复条目，历史保留。不保存臆测的现场或完成状态。",
    ),
    "save_task": (
        SaveTaskArgs,
        "保存用户明确授权的长期目标或本地任务，返回持久任务编号。不会立即执行。修改必须指定最新版本，暂停保持暂停。",
    ),
    "task_control": (
        TaskControlArgs,
        "暂停、恢复或取消授权任务；取消不能恢复，完成不可改回暂停。必须用最新版本。",
    ),
    "peer_request": (
        PeerRequestArgs,
        "向另一侧提问、查询或委托任务。查询/分析没有执行权限；task 必须关联本侧已保存的任务。受理后可继续安排其他独立工作，完成结果由关联事件返回。",
    ),
    "peer_records": (
        SearchArgs,
        "直接查询另一侧本用户的任务/历史/记忆，返回真实记录ID与版本，不唤醒对方模型。不修改数据。复杂问题需对方推理时用peer_request。",
    ),
    "peer_update_record": (
        PeerRecordUpdate,
        "用户明确要求修改另一侧既有记忆或暂停/恢复/取消其任务时使用。先用peer_records取得真实ID与版本。只有收到权威方confirmed回执才能说成功；unconfirmed表示结果未知，不能擅自重试改值。",
    ),
    "start_job": (
        StartJobArgs,
        "对本侧已授权任务启动可取消的异步作业。长期goal可指定operation=compose，完成本期作品后目标继续。重试同版本同操作返回原回执。accepted表示已受理，可继续其他独立工作，完成结果由后续事件返回。",
    ),
    "begin_media_output": (
        BeginMediaOutput,
        "在当前完整任务内登记一条影片的制作轮次，不创建子任务或新对话。确定要制作后、绑定素材前调用。output_key标识这条影片，重试复用；brief写本片要表达什么。上片未完成时继续原片或revise，不换key绕过失败。返回task_version用于绑定和复看；长期任务交付后可用新key制作下一片。任务目标、周期和录像配置保持不变。",
    ),
    "set_timer": (
        TimerArgs,
        "为用户明确要求的长期目标保存周期检查规则。时钟只唤醒 Agent，不替 Agent 决定动作。",
    ),
}

from .harness_observation import TOOLS as OBSERVATION_TOOLS  # noqa: E402 — registry depends on contracts above

TOOLS.update(OBSERVATION_TOOLS)


class WakeConfiguration(Contract):
    wake_words: list[str] = Field(default_factory=lambda: ["muse"], min_length=1, max_length=5)
    followup_seconds: int = Field(default=30, ge=5, le=300)

    @field_validator("wake_words")
    @classmethod
    def words(cls, words):
        words = [w.strip().casefold() for w in words]
        if any(not w or len(w) > 32 or "\n" in w for w in words) or len(set(words)) != len(words):
            raise ValueError("wake words must be distinct short text")
        return words


class ForwardedVoice(Contract):
    """Only the authenticated edge voice ingress may create this envelope.

    This is distinct from ordinary PeerMessage and never accepts a caller's
    conversation selection, task selection, or authorization flags.
    """

    principal: str = Field(pattern=ID)
    sender: Literal["edge"] = "edge"
    recipient: Literal["cloud"] = "cloud"
    adapter: Literal["mock-transcript", "file-asr", "streaming-asr"]
    utterance: VoiceUtterance
    request_text: str = Field(min_length=1, max_length=8000)
    gate_reason: Literal["wake_word", "wake_collection", "followup_window", "push_to_talk"]
    wake_word: str | None = Field(default=None, min_length=1, max_length=32)

    @property
    def event_id(self):
        return self.utterance.event_id

    @property
    def received_at(self):
        return self.utterance.received_at

    @model_validator(mode="after")
    def user_speech_only(self):
        if self.utterance.purpose != "user_command" or not self.request_text.strip():
            raise ValueError("only admitted user speech may be forwarded")
        if self.adapter == "file-asr" and (
            not self.utterance.audio_sha256
            or not self.utterance.asr.get("model")
            or self.utterance.replayed_at is None
            or self.utterance.asr.get("recognition_simulated") is True
        ):
            raise ValueError("file ASR requires real-recognition provenance and replay metadata")
        if self.adapter == "streaming-asr":
            validate_streaming_utterance(self.utterance)
        elif self.gate_reason == "wake_collection":
            raise ValueError("wake collection belongs to the streaming voice adapter")
        if (self.gate_reason == "wake_word") != (self.wake_word is not None):
            raise ValueError("wake decision and matched word disagree")
        return self


class ForwardedText(Contract):
    """Edge-authored text envelope. Conversation selection is origin metadata.

    Cloud always binds its own main, preserves query mode, and never interprets
    an edge task identifier as a cloud task. received_at is set once by the edge.
    """

    principal: str = Field(pattern=ID)
    sender: Literal["edge"] = "edge"
    recipient: Literal["cloud"] = "cloud"
    adapter: Literal["edge-text"] = "edge-text"
    message: UserInput
    received_at: datetime
    _aware = field_validator("received_at")(Invocation.aware.__func__)

    @property
    def event_id(self):
        return self.message.event_id

    @model_validator(mode="after")
    def unscoped_text(self):
        if self.message.task_id is not None:
            raise ValueError("edge task input requires an explicit local work conversation")
        if not self.message.text.strip():
            raise ValueError("text input must not be blank")
        return self


ForwardedInput = ForwardedVoice | ForwardedText


class VoiceReceiptQuery(Contract):
    event_id: str = Field(pattern=ID)
    principal: str = Field(pattern=ID)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class VoiceReplyDelivery(Contract):
    """Acknowledgement from the local output adapter AFTER rendering/playback."""

    event_id: str = Field(pattern=ID)
    reply_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
