"""Pi SDK adapter. Pi owns model/tool iteration; Python owns existing business tools."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from jsonschema import validate

from .contracts import TOOL_CONTRACTS, canonical, tool_schema
from .harness_contracts import TOOLS
from .harness_runtime import allowed_tools
from .harness_visual_review import VisualToolResult
from .model_access import model_access
from .media_audit import without_image_bytes
from .model_capabilities import resolve_model_capabilities
from .harness_workspace import sync_managed_files
from .harness_prompt import instructions, facts_message
from .plugins import require_profile
from .store import Conflict


@dataclass
class SchemaTool:
    name: str
    description: str
    parameters: dict

    def to_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class PiTools:
    def __init__(self):
        self.entries = {}

    @property
    def tool_names(self):
        return list(self.entries)

    def register(self, tool):
        self.entries[tool.name] = tool

    def unregister(self, name):
        self.entries.pop(name, None)

    def get(self, name):
        return self.entries.get(name)

    def get_definitions(self):
        return [self.entries[name].to_schema() for name in sorted(self.entries)]


@dataclass
class PiRun:
    event: dict
    process: object = None
    closed: bool = False
    pending_receipt: dict | None = None
    pending_receipts: list = field(default_factory=list)
    receipts: dict = field(default_factory=dict)
    requests: dict = field(default_factory=dict)
    redactions: tuple = ()
    cancel_requested: bool = False
    usage: dict = field(
        default_factory=lambda: {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    )
    models_used: list = field(default_factory=list)
    cache_reports: dict = field(default_factory=dict)
    request_count: int = 0
    completed_tools: list = field(default_factory=list)
    failed_tools: list = field(default_factory=list)
    delivered_visual_calls: set = field(default_factory=set)
    first_delta: float | None = None
    ready_seconds: float | None = None
    started: float = field(default_factory=time.monotonic)
    tool_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class PiRuntime:
    name = "pi"
    concurrent = True

    def __init__(self, store, profile, execute, context, **_):
        self.store, self.profile, self.execute_tool, self.context = store, profile, execute, context
        self.settings = getattr(profile, store.side)
        resolve_model_capabilities(self.settings)
        if self.settings.model_fallback:
            resolve_model_capabilities(self.settings, fallback=True)
        self.binding = require_profile(profile)[store.side]
        self.root = store.root / "pi"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.tools = PiTools()
        self.native = json.loads((Path(__file__).parent / "pi/native-tools.json").read_text())
        for name in self.binding["tools"] + [
            "bash",
            "read_autonomy",
            "configure_autonomy",
            "message_work",
        ]:
            if name in self.native:
                self.tools.register(SchemaTool(**self.native[name]))
            elif name in {**TOOL_CONTRACTS, **TOOLS}:
                args, description = {**TOOL_CONTRACTS, **TOOLS}[name]
                self.tools.register(SchemaTool(name, description, tool_schema(args)))
        self.runs = {}
        self.session_locks = {}
        self.external_read_tools = set()
        self.instructions = self.workspace / "MUSE.md"
        if not self.instructions.exists():
            shutil.copyfile(Path(__file__).parent / "prompts/muse-main.md", self.instructions)
        skill_source = Path(__file__).parent / "skills"
        managed = {"muse_tools.py": Path(__file__).parent / "pi/tool_client.py"}
        for name in profile.harness.enabled_skills:
            imported = self.store.root / "components" / "skills" / name
            source = imported if imported.is_dir() else skill_source / name
            if source.is_dir():
                for file in source.rglob("*"):
                    if file.is_file():
                        managed[str(Path("skills") / name / file.relative_to(source))] = file
        self.workspace_status = sync_managed_files(self.workspace, managed)

    def audit(self, run, kind, value):
        event = run.event
        wire = canonical(without_image_bytes(value))
        for secret in run.redactions:
            wire = wire.replace(secret, "[redacted]")
        value = json.loads(wire)
        self.store.audit(
            event.get("task_id"),
            kind,
            {
                **value,
                "event_id": event["id"],
                "principal": event["principal"],
                "session_key": event["session_key"],
                "runtime": "pi",
            },
        )

    def definitions(self, event):
        names = allowed_tools(self.binding, event, self.external_read_tools)
        return [
            t["function"] for t in self.tools.get_definitions() if t["function"]["name"] in names
        ]

    def catalogue_definitions(self):
        # set_timer is the old interval-only alias of set_wake_rule. Existing
        # scripts retain access through call_tool; new model turns use the full API.
        return [
            item["function"]
            for item in self.tools.get_definitions()
            if item["function"]["name"] != "set_timer"
        ]

    async def execute(self, name, args, call_id):
        raise RuntimeError("Pi business tools require the exact run context")

    async def call_tool(self, run, name, args, call_id):
        async with run.tool_lock:
            event = run.event
            if run.closed:
                return {"value": {"status": "turn_closed"}, "terminate": True}
            if name not in {d["name"] for d in self.definitions(event)} or name in self.native:
                raise Conflict("tool not available in this run")
            if self.store.event(event["id"], event["principal"])["status"] != "running":
                raise Conflict("input no longer running")
            schema = self.tools.get(name).parameters
            validate(args, schema)
            if name in {**TOOL_CONTRACTS, **TOOLS}:
                args = (
                    {**TOOL_CONTRACTS, **TOOLS}[name][0]
                    .model_validate(args)
                    .model_dump(mode="json")
                )
            self.audit(run, "tool.started", {"call_id": call_id, "name": name, "arguments": args})
            try:
                value = await self.execute_tool(event, name, args, call_id)
            except Exception as exc:
                run.failed_tools.append(name)
                self.audit(
                    run,
                    "tool.failed",
                    {
                        "call_id": call_id,
                        "name": name,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
                raise
            images = []
            if isinstance(value, VisualToolResult):
                for c in value.image_content:
                    if c.get("type") == "image_url":
                        url = c["image_url"]["url"]
                        mime, data = url.removeprefix("data:").split(";base64,", 1)
                        images.append({"type": "image", "data": data, "mimeType": mime})
                run.receipts[call_id] = {"receipt": dict(value), "images": images}
            if name in {"start_job", "peer_request"} and value.get("status") == "accepted":
                run.pending_receipt = {"tool": name, **value}
                run.pending_receipts.append(run.pending_receipt)
            if name == "publish_report" and value.get("status") == "pending_delivery":
                run.pending_receipt = {"tool": name, **value}
                run.pending_receipts.append(run.pending_receipt)
            self.audit(
                run,
                "tool.completed",
                {"call_id": call_id, "name": name, "arguments": args, "result": canonical(value)},
            )
            run.completed_tools.append(name)
            return {"value": dict(value), "images": images, "terminate": run.closed}

    async def dispatch(self, run, message):
        kind = message["type"]
        if kind == "context_delivered":
            self.store.context_mail.acknowledge(
                run.event["principal"],
                run.event["session_key"],
                [
                    ident
                    for ident in message["ids"]
                    if ident in run.event.get("_context_mail_ids", [])
                ],
            )
            return {"ok": True}
        if kind == "tool":
            return await self.call_tool(run, message["name"], message["args"], message["call_id"])
        if kind == "model_route":
            self.audit(
                run,
                "model.route",
                {k: message.get(k) for k in ("from_model", "to_model", "reason")},
            )
            return {"ok": True}
        if kind == "native_started":
            if run.closed or message["name"] not in {
                d["name"] for d in self.definitions(run.event)
            }:
                raise Conflict("tool not available in this run")
            if self.store.event(run.event["id"], run.event["principal"])["status"] != "running":
                raise Conflict("input no longer running")
            # Only the configured low-level command boundary; no semantic task judging.
            if message["name"] == "bash":
                import re

                command = message["args"]["command"]
                if re.search(r"(?:^|[\s;|&])(sudo|doas|systemctl|launchctl)(?:\s|$)", command):
                    raise Conflict("privilege and system-service commands are not enabled")
            self.audit(
                run,
                "tool.started",
                {
                    "call_id": message["call_id"],
                    "name": message["name"],
                    "arguments": message["args"],
                    "native_pi": True,
                },
            )
        elif kind in {"native_completed", "native_failed"}:
            if kind == "native_completed":
                run.completed_tools.append(message["name"])
                images = [
                    part for part in (message.get("result") or {}).get("content", [])
                    if part.get("type") == "image"
                ]
                if images:
                    run.receipts[message["call_id"]] = {
                        "receipt": {"origin": "pi_native_tool", "tool": message["name"]},
                        "images": images,
                    }
            else:
                run.failed_tools.append(message["name"])
            self.audit(
                run,
                "tool.completed" if kind.endswith("completed") else "tool.failed",
                {
                    "call_id": message["call_id"],
                    "name": message["name"],
                    "result": canonical(without_image_bytes(message.get("result"))),
                    "error": message.get("error"),
                    "native_pi": True,
                },
            )
        elif kind == "model_request":
            limit = self.profile.harness.model_request_limit
            if limit is not None and run.request_count >= limit:
                raise Conflict("model request limit reached")
            from .model_request_budget import reserve_request

            reserve_request(
                self.store, self.profile.harness.model_request_budget,
                run.event["id"], message["request_id"],
            )
            if message["model"] not in run.models_used:
                run.models_used.append(message["model"])
            run.request_count += 1
            ctx = message["context"]
            messages = ctx.get("messages", [])
            selected = {}
            for m in messages:
                if m.get("role") != "toolResult":
                    continue
                call_id = m.get("toolCallId")
                receipt = run.receipts.get(call_id)
                if (
                    receipt
                    and receipt["images"]
                    and all(i in m.get("content", []) for i in receipt["images"])
                ):
                    selected[call_id] = receipt["receipt"]
            run.requests[message["request_id"]] = selected
            self.audit(
                run,
                "model.request",
                {
                    "request_id": message["request_id"],
                    "model": message["model"],
                    "provider": message.get("provider", "muse"),
                    "capture_boundary": "pi_sdk_model_runtime_context",
                    "messages": [
                        {
                            **{
                                k: v
                                for k, v in m.items()
                                if k not in {"reasoning", "reasoning_content"}
                            },
                            "content": [c for c in m["content"] if c.get("type") != "thinking"],
                        }
                        if isinstance(m.get("content"), list)
                        else {
                            k: v
                            for k, v in m.items()
                            if k not in {"reasoning", "reasoning_content"}
                        }
                        for m in messages
                    ],
                    "excluded_message_fields": [
                        "thinking content",
                        "reasoning",
                        "reasoning_content",
                    ],
                    "system_prompt": ctx.get("systemPrompt"),
                    "tool_definitions": ctx.get("tools", []),
                    "message_format": "pi_native",
                    "route_capabilities": message.get("capabilities"),
                },
            )
        elif kind == "wire_usage":
            usage = message.get("usage") or {}
            details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
            run.cache_reports[message["request_id"]] = (
                "cached_tokens" in details
                or "prompt_cache_hit_tokens" in usage
                or "cached_tokens" in usage
            )
            self.audit(
                run,
                "model.wire_usage",
                {
                    key: message.get(key)
                    for key in (
                        "request_id",
                        "model",
                        "provider",
                        "usage",
                        "provider_usage_present",
                    )
                },
            )
        elif kind == "wire_request":
            self.audit(
                run,
                "model.wire_request",
                {key: value for key, value in message.items() if key not in {"id", "type"}},
            )
        elif kind in {"model_response", "model_failed"}:
            request_id = message["request_id"]
            receipts = run.requests.pop(request_id, {})
            if kind == "model_response" and not message.get("error") and receipts:
                from .harness_media_review import record_delivered

                record_delivered(self.store, run.event, request_id, receipts)
                run.delivered_visual_calls.update(receipts)
            reply = message.get("message") or {}
            for metric in run.usage:
                run.usage[metric] += (reply.get("usage") or {}).get(metric, 0)
            self.audit(
                run,
                "model.response" if kind == "model_response" else "model.failed",
                {
                    "request_id": request_id,
                    "finish_reason": "error" if message.get("error") else reply.get("stopReason"),
                    "content": [c for c in reply.get("content", []) if c.get("type") != "thinking"],
                    "usage": reply.get("usage"),
                    "error": message.get("error"),
                    "error_message": reply.get("errorMessage"),
                },
            )
        return {"ok": True}

    async def run(self, event):
        lock = self.session_locks.setdefault(event["session_key"], asyncio.Lock())
        async with lock:
            async with asyncio.timeout(self.settings.run_timeout):
                return await self._run(event)

    async def compact(self, event, instructions):
        """Explicit owner maintenance using Pi's checkpoint operation."""
        lock = self.session_locks.setdefault(event["session_key"], asyncio.Lock())
        async with lock:
            async with asyncio.timeout(self.settings.run_timeout):
                return await self._run(event, compact_instructions=instructions)

    def workspace_for(self, event):
        if ":main:" in event["session_key"]:
            return self.workspace
        sid = hashlib.sha256(event["session_key"].encode()).hexdigest()[:24]
        return self.root / "workspaces" / sid

    async def _run(self, event, *, compact_instructions=None):
        run = PiRun(event)
        context = self.context(event)
        system = instructions(
            self.store, event, context, presentation=self.instructions.read_text()
        )
        current_facts = facts_message(self.store, event, context)
        context_mail = self.store.context_mail.pending(event["principal"], event["session_key"])
        event["_context_mail_ids"] = [item["id"] for item in context_mail]
        endpoint, key, local = model_access(self.profile, self.store.side)
        fallback = self.settings.model_fallback
        fallback_key = Path(fallback.key_file).expanduser().read_text().strip() if fallback else ""
        sid = hashlib.sha256(event["session_key"].encode()).hexdigest()[:24]
        folder = self.root / "sessions" / sid
        folder.mkdir(parents=True, exist_ok=True)
        native_workspace = self.workspace_for(event)
        native_workspace.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        node_token_file = self.store.root.parent / "node-token"
        node_token = node_token_file.read_text().strip() if node_token_file.is_file() else ""
        run.redactions = tuple(x for x in (key, fallback_key, token, node_token) if x)
        transport = Path(tempfile.mkdtemp(prefix="muse-pi-"))
        socket = transport / "tool.sock"

        async def code_call(reader, writer):
            try:
                message = json.loads(await reader.readline())
                if not secrets.compare_digest(str(message.pop("token", "")), token):
                    raise Conflict("invalid run credential")
                reply = await self.call_tool(
                    run,
                    message["name"],
                    message.get("arguments", {}),
                    "code-" + secrets.token_hex(8),
                )
                result = {"ok": True, "result": reply["value"]}
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            writer.write((canonical(result) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_unix_server(
            code_call, path=str(socket), limit=32 * 1024 * 1024
        )
        skills = []
        for name in self.profile.harness.enabled_skills:
            file = self.workspace / "skills" / name / "SKILL.md"
            if file.is_file():
                skills.append(f"{name}: {file}")
        system += "\n已装配技能（需要时用read_file读取）：\n" + "\n".join(skills)
        system += "\n代码执行环境已将Muse工具客户端加入Python路径，可用from muse_tools import call并调用call(name, arguments)访问本轮同一业务工具；无需寻找或读取凭据。read_file/write_file/edit_file/list_dir/find_files/grep/bash由Pi原生工具执行。禁止提权及系统服务管理。"
        prompt = (
            event["body"].get("text", "")
            if event["kind"] in {"user", "voice"}
            else "[程序事件，不是用户新指令]\n"
            + canonical(
                {
                    "event_id": event["id"],
                    "kind": event["kind"],
                    "task_id": event.get("task_id"),
                    "input": event["body"],
                }
            )
            + "\n按需调用get_task_context读取当前事实。"
        )
        if event["kind"] == "autonomy":
            file = Path(event["body"]["duties_file"])
            prompt += "\n[本轮读取的自主工作文件 " + str(file) + "]\n" + file.read_text()
        # Stable schemas preserve the session prefix. Invocation permissions are
        # checked against the current event in call_tool/native_started.
        definitions = self.catalogue_definitions()
        if event["read_only"]:
            prompt += "\n[本轮为只读查询，工具执行权限仅允许读取。]"
        payload = {
            "type": "run",
            "event_id": event["id"],
            "pi_dir": self.profile.harness.pi_runtime_dir,
            "workspace": str(native_workspace),
            "tool_module_dir": str(self.workspace),
            "redactions": list(run.redactions),
            "agent_dir": str(self.root / "agent"),
            "session_dir": str(folder),
            "endpoint": endpoint,
            "key": key,
            "fallback": {
                "endpoint": fallback.base_url,
                "key": fallback_key,
                "model": fallback.model,
                "capabilities": resolve_model_capabilities(self.settings, fallback=True),
                "request_timeout_ms": fallback.request_timeout * 1000
                if fallback.request_timeout is not None
                else None,
                "api": "openai-responses"
                if fallback.api_type == "responses"
                else "openai-completions",
            }
            if fallback
            else None,
            "api": "openai-responses"
            if self.settings.model_api_type == "responses"
            else "openai-completions",
            "model": self.settings.model,
            "model_capabilities": resolve_model_capabilities(self.settings),
            "thinking_level": self.settings.thinking_level,
            "compaction": {
                key: value
                for key, value in {
                    "reserveTokens": self.profile.harness.compaction_reserve_tokens,
                    "keepRecentTokens": self.profile.harness.compaction_keep_recent_tokens,
                }.items()
                if value is not None
            },
            "context_tokens": self.settings.context_tokens,
            "output_tokens": self.settings.output_tokens,
            "request_timeout_ms": self.settings.request_timeout * 1000
            if self.settings.request_timeout is not None
            else None,
            "request_retries": self.settings.request_retries,
            "system_prompt": system,
            "tools": definitions,
            "tool_socket": str(socket),
            "tool_token": token,
            "prompt": prompt,
            "current_facts": current_facts,
            "context_mail": context_mail,
            "retired_context_mail_ids": self.store.context_mail.archived_ids(
                event["principal"], event["session_key"]
            ),
            "compact_instructions": compact_instructions,
            "program_event": event["kind"] not in {"user", "voice"},
        }
        env = {
            k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"}
        }
        outcome = None
        self.runs[event["id"]] = run
        try:
            process = run.process = await asyncio.create_subprocess_exec(
                "node",
                str(Path(__file__).parent / "pi/bridge.mjs"),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                limit=32 * 1024 * 1024,
            )

            async def errors():
                return (
                    (await process.stderr.read())
                    .decode(errors="replace")
                    .replace(key, "[redacted]")
                    .replace(token, "[redacted]")
                )

            stderr = asyncio.create_task(errors())
            process.stdin.write((canonical(payload) + "\n").encode())
            await process.stdin.drain()
            self.audit(
                run,
                "harness.run.started",
                {"model": self.settings.model, "runtime_version": "pi@1.5.0"},
            )
            while line := await process.stdout.readline():
                message = json.loads(line)
                kind = message["type"]
                if kind == "text_delta":
                    if run.first_delta is None:
                        run.first_delta = time.monotonic() - run.started
                    self.audit(run, "model.text.delta", {"text": message["text"]})
                elif kind == "done":
                    outcome = message
                elif kind == "failed":
                    raise RuntimeError(message["error"])
                elif kind == "ready":
                    run.ready_seconds = time.monotonic() - run.started
                    self.audit(
                        run,
                        "pi.ready",
                        {
                            "seconds": run.ready_seconds,
                            "workspace": str(native_workspace),
                            "process_id": process.pid,
                            "session_file": message["session_file"],
                            "model_capabilities": message.get("model_capabilities"),
                            "compaction": message.get("compaction"),
                            "thinking_level": message.get("thinking_level"),
                        },
                    )
                    if set(message["active_tools"]) != {d["name"] for d in definitions}:
                        raise RuntimeError("Pi tool catalogue differs from configured tools")
                elif kind == "compacted":
                    self.audit(run, "pi.compacted", message["result"])
                else:
                    try:
                        value = await self.dispatch(run, message)
                        response = {"type": "reply", "id": message["id"], "value": value}
                    except Exception as exc:
                        response = {
                            "type": "reply",
                            "id": message["id"],
                            "error": str(exc)
                            .replace(key, "[redacted]")
                            .replace(token, "[redacted]"),
                        }
                    process.stdin.write((canonical(response) + "\n").encode())
                    await process.stdin.drain()
            await process.wait()
            err = await stderr
            if not outcome and run.cancel_requested:
                outcome = {"text": "", "messages": [], "stats": {}}
            if not outcome:
                raise RuntimeError("Pi run produced no completion: " + err[-1000:])
            text = outcome.get("text") or (
                "已安排，完成后会告诉你。" if run.pending_receipt else ""
            )
            # The native bridge tracks this run's events. Session messages also
            # contain prior provider failures and can be replaced by compaction.
            error = outcome.get("error")
            if run.cancel_requested:
                text, error = "已停止这一轮。", None
            self.audit(
                run,
                "harness.run.finished",
                {
                    "runtime_version": "pi@1.5.0",
                    "seconds": time.monotonic() - run.started,
                    "requests": run.request_count,
                    "stats": outcome.get("stats"),
                    "run_usage": run.usage,
                    "cost_status": "provider_price_not_configured",
                    "error": error,
                },
            )
            return {
                "text": text,
                "model_invoked": run.request_count > 0,
                "pending_receipt": run.pending_receipt,
                "pending_receipts": run.pending_receipts,
                "error": "PiModelFailed" if error else None,
                "runtime": "pi@1.5.0",
                "model": run.models_used[-1] if run.models_used else self.settings.model,
                "models_used": run.models_used,
                "run_seconds": time.monotonic() - run.started,
                "first_text_delta_seconds": run.first_delta,
                "runtime_ready_seconds": run.ready_seconds,
                "usage": {**run.usage, "total": sum(run.usage.values())},
                "cache_usage_status": (
                    "reported"
                    if run.cache_reports and all(run.cache_reports.values())
                    else "partial"
                    if any(run.cache_reports.values())
                    else "not_reported"
                ),
                "cancelled": run.cancel_requested,
                "request_count": run.request_count,
                "review_activity": {
                    "completed_tools": run.completed_tools,
                    "failed_tools": run.failed_tools,
                    "visual_calls_delivered": sorted(run.delivered_visual_calls),
                    "evidence_status": (
                        "images_received"
                        if run.delivered_visual_calls
                        else "tools_only"
                        if run.completed_tools
                        else "no_tool_evidence"
                    ),
                    "meaning": "execution_evidence_not_a_claim_of_complete_review",
                },
            }
        finally:
            if run.process and run.process.returncode is None:
                run.process.terminate()
                try:
                    await asyncio.wait_for(run.process.wait(), 5)
                except asyncio.TimeoutError:
                    run.process.kill()
                    await run.process.wait()
            server.close()
            await server.wait_closed()
            shutil.rmtree(transport, ignore_errors=True)
            self.runs.pop(event["id"], None)

    async def cancel_event(self, event_id):
        run = self.runs.get(event_id)
        if run and run.process and run.process.returncode is None:
            run.cancel_requested = True
            run.process.stdin.write(b'{"type":"cancel"}\n')
            await run.process.stdin.drain()

    async def close(self):
        for event_id in list(self.runs):
            await self.cancel_event(event_id)

    async def history(self, conversation_id):
        sid = hashlib.sha256(conversation_id.encode()).hexdigest()[:24]
        folder = self.root / "sessions" / sid
        files = list(folder.glob("*.jsonl"))
        if not files:
            return {
                "conversation_id": conversation_id,
                "model": self.settings.model,
                "messages": [],
                "boundary": "Pi native active branch",
            }
        source = max(files, key=lambda p: p.stat().st_mtime_ns)
        proc = await asyncio.create_subprocess_exec(
            "node",
            str(Path(__file__).parent / "pi/history.mjs"),
            self.profile.harness.pi_runtime_dir,
            str(source),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        output, error = await proc.communicate()
        if proc.returncode:
            raise RuntimeError("Pi history read failed")
        return {
            "conversation_id": conversation_id,
            "model": self.settings.model,
            **json.loads(output),
            "boundary": "Pi native active branch; business event history remains separately available",
        }

    async def clear_test_sessions(self, principal):
        if not self.profile.harness.test_controls:
            raise Conflict("test controls disabled")
        with self.store.connect() as db:
            if (
                self.runs
                or db.execute(
                    "SELECT 1 FROM harness_events WHERE status IN ('queued','running') LIMIT 1"
                ).fetchone()
            ):
                raise Conflict("wait for active inputs before clearing test sessions")
        removed = 0
        for conversation in self.store.conversations.list(principal):
            sid = hashlib.sha256(conversation["id"].encode()).hexdigest()[:24]
            folder = self.root / "sessions" / sid
            if folder.exists():
                shutil.rmtree(folder)
                removed += 1
        self.store.audit(
            None,
            "test.sessions.cleared",
            {"principal": principal, "runtime": "pi", "removed_files": removed},
        )
        return {
            "removed_files": removed,
            "business_records_preserved": True,
            "provider_cache_cleared": False,
        }
