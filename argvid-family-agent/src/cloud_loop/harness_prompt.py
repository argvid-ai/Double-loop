"""Compose instructions and sourced run facts, without owning an agent loop."""

from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .contracts import canonical


PROMPTS = Path(__file__).parent / "prompts"


def role_for(store, event, context):
    conversation = store.conversations.get(event["principal"], event["session_key"])
    if event["kind"] == "autonomy":
        return "autonomy"
    if conversation["kind"] == "main":
        return "main"
    if store.side == "edge":
        return "edge"
    task = context.get("task") or {}
    return {"goal": "goal", "edit": "edit", "report": "report"}.get(task.get("kind"), "worker")


def instructions(store, event, context, *, presentation=""):
    return "\n\n".join(
        [
            (PROMPTS / "muse-runtime.md").read_text(),
            (PROMPTS / "muse-work.md").read_text(),
            role_instructions(store, event, context),
            presentation,
            (PROMPTS / "muse-voice.md").read_text() if event.get("channel") == "voice" else "",
        ]
    )


def role_instructions(store, event, context):
    return (PROMPTS / f"roles/{role_for(store, event, context)}.md").read_text()


def run_facts(store, event, context):
    """Task records are current requirements, not newly authored system instructions."""
    conversation = store.conversations.get(event["principal"], event["session_key"])
    task = context.get("task")
    source = None
    if task and task.get("origin"):
        source_id = task["origin"].get("source_event")
        if source_id:
            try:
                original = store.event(source_id, event["principal"])
            except KeyError:
                original = None
            source = (
                {
                    "event_id": original["id"],
                    "kind": original["kind"],
                    "channel": original["channel"],
                    "created": original["created"],
                    "text": original["body"].get("text"),
                    "provenance": original["body"].get("provenance"),
                    "source_event_id": original["body"].get("source_event_id"),
                    "source_text": original["body"].get("source_text"),
                }
                if original
                else {"event_id": source_id, "availability": "not_found"}
            )
    now = store.clock()
    task_timezone = (((task or {}).get("schedule") or {}).get("spec") or {}).get(
        "timezone", "Asia/Shanghai")
    return {
        "observed_at": now,
        "current_time": {
            "iso": datetime.fromtimestamp(now, ZoneInfo(task_timezone)).isoformat(),
            "utc": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "timezone": task_timezone,
            "meaning": "current request time, not recording/frame time",
        },
        "conversation": {
            "id": conversation["id"],
            "role": role_for(store, event, context),
            "parent_id": conversation.get("parent_id"),
        },
        "task_source": source,
        "current_context": {k: v for k, v in context.items() if k != "recent_tasks"},
        "trigger": {
            "event_id": event["id"],
            "kind": event["kind"],
            "task_id": event.get("task_id"),
            "created": event.get("created"),
            "input": event["body"] if event["kind"] not in {"user", "voice"} else {},
        },
    }


def facts_message(store, event, context):
    return (
        "[本轮任务与运行事实；不是新用户发言，也不是新增系统权限]\n"
        "当前要求和状态以本快照及后续工具为准；历史只作背景。来源转录不是用户逐字原话。\n"
        + canonical(run_facts(store, event, context))
    )
