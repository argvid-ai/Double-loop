"""Installed capability catalog and dependency resolver; no agent loop lives here."""

from .contracts import PluginSpec, Profile


def spec(id, side, slot, name, provides, requires=(), tools=(), **kw):
    return PluginSpec(
        id=id,
        side=side,
        slot=slot,
        name=name,
        provides=list(provides),
        requires=list(requires),
        tools=list(tools),
        **kw,
    )


CATALOG = [
    spec("nanobot", "both", "runtime", "NanoBot 0.3.0", ["harness"]),
    spec("bridge-chat", "both", "model", "traex-bridge 生成模型", ["generate"], ["harness"]),
    spec("sqlite-state", "both", "state", "任务状态与事实", ["state"], tools=["get_task_context"]),
    spec(
        "fresh-context",
        "both",
        "context",
        "每步事实装配",
        ["context"],
        ["state", "generate"],
        events=["before_model_request"],
        context="fresh task-scoped projection; never copies the other agent's transcript",
    ),
    spec(
        "native-context",
        "both",
        "context",
        "原生会话、压缩与真实请求记录",
        ["context"],
        ["state", "generate"],
        events=["before_model_request"],
        context="native history and summary; business facts only through tools",
    ),
    spec(
        "trace-hooks",
        "both",
        "hooks",
        "工具生命周期观测",
        ["trace"],
        ["harness"],
        events=["before_execute_tool", "after_execute_tool", "on_execute_tool_error"],
        effects=["append_audit"],
    ),
    spec(
        "story-skill",
        "cloud",
        "skills",
        "素材补齐与成片策略",
        ["strategy.story"],
        ["media", "policy"],
    ),
    spec(
        "photo-skill",
        "edge",
        "skills",
        "观察与回放拍摄策略",
        ["strategy.photo"],
        ["scene", "policy"],
    ),
    spec(
        "gateway-http",
        "both",
        "bridge",
        "端云任务桥",
        ["bridge"],
        ["state"],
        events=[
            "EDGE_HELLO",
            "CLOUD_WELCOME",
            "KEEPALIVE",
            "capabilities_changed",
            "EDGE_TOOL_INVOKE",
            "EDGE_TASK_COMPLETED",
        ],
    ),
    spec(
        "task-policy",
        "both",
        "policy",
        "期限与任务策略",
        ["policy"],
        ["state"],
        tools=["publish_result"],
        events=["user", "timer", "scene.changed", "edge.completed"],
    ),
    spec(
        "remote-capture",
        "cloud",
        "tools",
        "远程补采",
        ["remote.capture"],
        ["bridge", "policy"],
        tools=["edge_capture_task"],
        permissions=["edge.task.submit"],
        effects=["remote_task"],
    ),
    spec(
        "ffmpeg-media",
        "cloud",
        "media",
        "素材检查与成片",
        ["media"],
        ["state"],
        tools=["media_list", "media_render", "media_inspect"],
        permissions=["cloud.assets"],
        effects=["write_deliverable"],
    ),
    spec(
        "scene-replay",
        "edge",
        "perception",
        "现场事实回放",
        ["scene", "input.scene"],
        ["state"],
        tools=["get_scene_state", "get_robot_state"],
        events=["scene.changed"],
    ),
    spec(
        "mock-inputs",
        "edge",
        "inputs",
        "视频 / 音频 / 设备共同输入接口",
        ["inputs"],
        ["scene", "state"],
        events=["input.scene", "input.robot", "input.transcript", "task.paused", "task.resumed"],
    ),
    spec("safety-guard", "edge", "safety", "本地执行准入", ["safety"], ["scene", "policy"]),
    spec(
        "replay-device",
        "edge",
        "action",
        "回放拍摄设备",
        ["capture"],
        ["safety", "scene", "state"],
        tools=["move_to_viewpoint", "camera_capture", "capture_qc", "capture_workflow"],
        permissions=["edge.assets"],
        effects=["simulated_motion", "replay_capture"],
    ),
    spec(
        "video-file-camera",
        "edge",
        "action",
        "原始视频文件相机 / 模拟运动",
        ["capture"],
        ["safety", "scene", "state"],
        tools=["move_to_viewpoint", "camera_capture", "capture_qc", "capture_workflow"],
        permissions=["edge.assets"],
        effects=["simulated_motion", "replay_capture"],
    ),
    spec(
        "semif",
        "edge",
        "scoring",
        "SemIf / Qwen 14B（待接入）",
        ["score"],
        ["local-model"],
        tools=["semantic_score"],
        implemented=False,
    ),
    spec(
        "llamacpp-qwen",
        "edge",
        "model",
        "llama.cpp / Qwen（本地提供者）",
        ["generate", "local-model"],
        ["harness"],
    ),
    spec(
        "budgeted-context",
        "edge",
        "context",
        "最终请求预算与会话上下文",
        ["context.budget"],
        ["context", "generate"],
        events=["before_model_request"],
    ),
    spec(
        "conversation-policy",
        "edge",
        "policy",
        "端侧用户目标与控制",
        ["policy", "conversation"],
        ["state"],
        tools=["set_request", "control_request", "publish_status", "read_history"],
        events=["user", "capture.completed", "cloud.completed"],
    ),
    spec(
        "dialogue-skill",
        "edge",
        "skills",
        "端侧对话与异步任务说明",
        ["strategy.dialogue"],
        ["conversation", "context"],
    ),
    spec(
        "interactive-scene",
        "edge",
        "perception",
        "当前状态表中的现场输入",
        ["scene", "input.scene"],
        ["state"],
    ),
    spec(
        "interactive-video",
        "edge",
        "action",
        "异步视频回放与模拟运动",
        ["capture"],
        ["safety", "scene", "conversation"],
        tools=["start_capture"],
        permissions=["edge.assets"],
        effects=["simulated_motion", "replay_capture"],
    ),
    spec(
        "cloud-delegate",
        "edge",
        "tools",
        "委托云端成片",
        ["delegate.cloud"],
        ["bridge", "conversation", "capture"],
        tools=["delegate_cloud"],
        permissions=["cloud.task.submit"],
    ),
    spec("codex", "both", "runtime", "Codex（待接入）", ["harness"], implemented=False),
    spec("pi", "both", "runtime", "Pi（待接入）", ["harness"], implemented=False),
    spec(
        "harness-policy",
        "both",
        "policy",
        "双端持久事项与固定会话",
        ["policy", "functional"],
        ["state"],
        tools=[
            "save_task",
            "begin_media_output",
            "task_control",
            "set_timer",
            "set_wake_rule",
            "list_wake_rules",
            "control_wake_rule",
            "read_report_period",
            "publish_report",
            "get_delivery_policy",
            "configure_delivery_policy",
            "request_user_attention",
        ],
    ),
    spec(
        "harness-basic",
        "both",
        "tools",
        "基础时间、文件与会话查询",
        ["files", "utilities"],
        ["harness", "state"],
        tools=[
            "list_media_sources",
            "lookup_sampling_plan",
            "get_source_frame",
            "get_capabilities",
            "get_time",
            "reply_with_voice",
            "list_conversations",
            "read_conversation",
            "read_file",
            "write_file",
            "edit_file",
            "list_dir",
            "find_files",
            "grep",
        ],
    ),
    spec(
        "harness-memory",
        "both",
        "memory",
        "跨会话历史与记忆",
        ["records"],
        ["state"],
        tools=["search_records", "read_record", "remember"],
    ),
    spec(
        "harness-bridge",
        "both",
        "bridge",
        "双向问题与任务通信",
        ["bridge"],
        ["state"],
        tools=["peer_request", "peer_records", "peer_update_record"],
        events=["peer.query", "peer.analysis", "peer.task", "peer.result"],
    ),
    spec(
        "harness-skill",
        "both",
        "skills",
        "独立入口与持久事实工作方法",
        ["strategy.harness"],
        ["functional", "records", "context"],
        tools=["run_skill_script"],
    ),
    spec(
        "harness-camera",
        "edge",
        "action",
        "受约束的异步文件拍摄",
        ["capture"],
        ["safety", "scene", "functional"],
        tools=["start_job"],
        effects=["replay_capture"],
    ),
    spec(
        "harness-media",
        "both",
        "media",
        "异步素材处理与检查",
        ["media"],
        ["state", "functional"],
        tools=[
            "media_list",
            "media_inspect",
            "inspect_media_frames",
            "review_rendered_media",
            "start_job",
        ],
        effects=["write_deliverable"],
    ),
    spec(
        "observation-control",
        "cloud",
        "tools",
        "持续观察配置、控制与按需查询",
        ["observation.control"],
        ["functional", "bridge", "state"],
        tools=[
            "list_input_devices",
            "read_recording_policy",
            "configure_recording_policy",
            "read_media_storage",
            "configure_media_storage",
            "collect_media_storage",
            "protect_media_file",
            "review_observation_candidates",
            "read_default_observation",
            "configure_default_observation",
            "start_observation",
            "list_observation_executors",
            "control_observation_task",
            "configure_observation",
            "control_observation",
            "query_observation",
            "list_observation_markers",
            "list_observation_media",
            "attach_observation_media",
            "search_observation_evidence",
            "review_observation_period",
        ],
    ),
    spec("dsh", "both", "runtime", "DeepSeek Harness（待接入）", ["harness"], implemented=False),
]
BY_ID = {s.id: s for s in CATALOG}
REQUIRED = {
    "cloud": {"harness", "generate", "state", "context", "policy", "bridge", "media"},
    "edge": {"harness", "generate", "state", "context", "policy", "bridge", "scene", "safety"},
}
SINGLE_SLOTS = {"runtime", "model", "state", "policy", "bridge", "safety", "action"}


def resolve(profile: Profile, side: str) -> dict:
    selected = getattr(profile, side)
    errors, specs = [], []
    if len(set(selected.plugins)) != len(selected.plugins):
        errors.append("重复插件")
    for name in selected.plugins:
        item = BY_ID.get(name)
        if item is None:
            errors.append(f"未知插件: {name}")
        elif not item.implemented:
            errors.append(f"尚未实现: {name}")
        elif item.side not in {side, "both"}:
            errors.append(f"插件不属于 {side}: {name}")
        else:
            specs.append(item)
    provides = {cap for s in specs for cap in s.provides}
    bridge_modelhub = selected.model in {
        "gpt-6.1-sol",
        "ModelHub-GPT-6-Sol",
        "ModelHub-GPT-6.1-Sol",
        "ModelHub-GPT-6-Astra",
    }
    if "bridge-chat" in selected.plugins and selected.model_capabilities is not None:
        from .model_capabilities import resolve_model_capabilities

        try:
            resolve_model_capabilities(selected)
            if selected.model_fallback:
                resolve_model_capabilities(selected, fallback=True)
        except ValueError as exc:
            errors.append(str(exc))
    elif "bridge-chat" in selected.plugins and bridge_modelhub:
        if selected.model_api_type != "responses":
            errors.append("ModelHub 模型需要 Responses 接口配置")
    elif "bridge-chat" in selected.plugins and selected.model not in {
        "DeepSeek-V4-Pro",
        "DeepSeek-V4-Flash",
        "GPT-6.1-Sol",
    }:
        errors.append("bridge-chat 未登记此模型；当前支持 DeepSeek Pro / Flash、TraeX GPT-6.1-Sol 和 ModelHub GPT6")
    if "llamacpp-qwen" in selected.plugins:
        from urllib.parse import urlparse

        if not selected.model.startswith("Qwen") or not selected.model_base_url:
            errors.append("本地 Qwen 需要显式模型名与本地服务地址")
        elif urlparse(selected.model_base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
            errors.append("本地 Qwen 服务必须监听 loopback")
    for slot in SINGLE_SLOTS:
        if sum(s.slot == slot for s in specs) > 1:
            errors.append(f"核心插槽重复: {slot}")
    for item in specs:
        missing = set(item.requires) - provides
        if missing:
            errors.append(f"{item.id} 缺少依赖: {', '.join(sorted(missing))}")
    missing = REQUIRED[side] - provides
    if missing:
        errors.append(f"缺少必要能力: {', '.join(sorted(missing))}")
    # Topological load order, not the order supplied by a frontend.
    ordered, pending, ready = [], list(specs), set()
    while pending:
        next_items = [s for s in pending if set(s.requires) <= ready]
        if not next_items:
            if not errors:
                errors.append("插件依赖成环")
            break
        for item in next_items:
            ordered.append(item.id)
            ready.update(item.provides)
            pending.remove(item)
    tools = [t for s in specs for t in s.tools]
    if profile.harness:
        from .harness_scripts import available_scripts

        if not available_scripts(profile.harness.enabled_skills):
            tools = [t for t in tools if t != "run_skill_script"]
    if "functional" in provides and tools.count("start_job") > 1:
        tools = [t for t in tools if t != "start_job"] + ["start_job"]
    if len(tools) != len(set(tools)):
        errors.append("工具名冲突")
    return {
        "side": side,
        "valid": not errors,
        "errors": errors,
        "load_order": ordered,
        "tools": sorted(tools),
        "capabilities": sorted(provides),
        "model": selected.model,
        "profile_digest": profile.fingerprint,
    }


def validate_profile(profile: Profile) -> dict:
    bindings = {side: resolve(profile, side) for side in ("cloud", "edge")}
    return {
        "valid": all(x["valid"] for x in bindings.values()),
        "bindings": bindings,
        "profile_digest": profile.fingerprint,
    }


def require_profile(profile: Profile):
    result = validate_profile(profile)
    if not result["valid"]:
        raise ValueError("; ".join(e for b in result["bindings"].values() for e in b["errors"]))
    return result["bindings"]
