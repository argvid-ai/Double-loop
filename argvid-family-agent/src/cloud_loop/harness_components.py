"""Node-owned component configuration; MCP uses the SDK, NanoBot owns the loop."""

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from nanobot.agent.tools.base import Tool
from pydantic import Field, field_validator

from .contracts import Contract, canonical, digest
from .store import Conflict

NAME = r"^[a-z][a-z0-9_-]{0,39}$"
REQUIRED = {
    "get_task_context",
    "save_task",
    "task_control",
    "peer_request",
    "get_capabilities",
    "read_file",
}


class SourceSpec(Contract):
    id: str = Field(pattern=NAME)
    title: str = Field(min_length=1, max_length=100)
    transport: str = Field(default="streamable_http", pattern="^(streamable_http|sse)$")
    url: str = Field(max_length=1000)
    headers_file: str | None = Field(default=None, max_length=1000)

    @field_validator("url")
    @classmethod
    def endpoint(cls, v):
        u = urlparse(v)
        if (
            u.scheme not in {"http", "https"}
            or not u.hostname
            or u.username
            or u.password
            or u.query
            or u.fragment
        ):
            raise ValueError("use HTTP/SSE URL without embedded credentials or query secrets")
        return v


class ApplyComponents(Contract):
    expected_revision: int = Field(ge=0)
    disabled_tools: list[str] = Field(default_factory=list, max_length=100)
    enabled_skills: list[str] = Field(default_factory=list, max_length=50)
    external_tools: dict[str, dict[str, str]] = Field(default_factory=dict)


class SkillImport(Contract):
    path: str | None = Field(default=None, max_length=1000)
    zip_base64: str | None = Field(default=None, max_length=1_900_000)


class RemoveComponent(Contract):
    kind: str = Field(pattern="^(skill|source)$")
    id: str = Field(pattern=NAME)


def headers_for(spec):
    if not spec.get("headers_file"):
        return {}
    value = json.loads(Path(spec["headers_file"]).expanduser().read_text())
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) or "\n" in v or "\r" in v
        for k, v in value.items()
    ):
        raise ValueError("invalid header file")
    return value


def secret_values(spec):
    values = set(headers_for(spec).values())
    for value in list(values):
        if value.lower().startswith(("bearer ", "basic ")):
            values.add(value.split(" ", 1)[1])
    return sorted((v for v in values if v), key=len, reverse=True)


def skill_metadata(raw):
    parts = raw.split("---", 2)
    if not raw.startswith("---") or len(parts) != 3:
        raise ValueError("Skill requires YAML frontmatter")
    meta = yaml.safe_load(parts[1])
    if (
        not isinstance(meta, dict)
        or not isinstance(meta.get("name"), str)
        or not re.fullmatch(NAME, meta["name"])
        or not isinstance(meta.get("description"), str)
        or not meta["description"].strip()
    ):
        raise ValueError("Skill name/description invalid")
    metadata = meta.get("metadata") or {}
    if isinstance(metadata, str):
        metadata = json.loads(metadata)
    if not isinstance(metadata, dict):
        raise ValueError("Skill metadata must be an object")
    provider = metadata.get("nanobot", metadata.get("openclaw", {}))
    if isinstance(provider, str):
        provider = json.loads(provider)
    if not isinstance(provider, dict):
        raise ValueError("Skill dependency metadata invalid")
    req = provider.get("requires", {})
    if not isinstance(req, dict):
        raise ValueError("Skill requires must be an object")
    for key in ["bins", "anyBins", "env", "tools"]:
        if not isinstance(req.get(key, []), list) or any(
            not isinstance(v, str) for v in req.get(key, [])
        ):
            raise ValueError("Skill dependency list invalid")
    return meta, req


@asynccontextmanager
async def session_for(spec):
    headers = headers_for(spec)
    async with asyncio.timeout(25):
        if spec["transport"] == "sse":
            async with sse_client(
                spec["url"], headers=headers, timeout=10, sse_read_timeout=20
            ) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    yield session
        else:
            async with httpx.AsyncClient(
                headers=headers, timeout=20, follow_redirects=False, trust_env=False
            ) as client:
                async with streamable_http_client(spec["url"], http_client=client) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        yield session


def public_schema(tool):
    schema = tool.inputSchema
    from jsonschema import Draft202012Validator

    Draft202012Validator.check_schema(schema)
    raw = canonical(schema)

    def local_refs(value):
        if isinstance(value, dict):
            return all(
                (k not in {"$ref", "$dynamicRef"} or isinstance(v, str) and v.startswith("#"))
                and local_refs(v)
                for k, v in value.items()
            )
        return all(local_refs(x) for x in value) if isinstance(value, list) else True

    if len(raw) > 24000 or not local_refs(schema):
        raise ValueError("tool schema too large or external reference")
    return {
        "name": tool.name,
        "description": (tool.description or "")[:2000],
        "schema": schema,
        "read_only_hint": bool(tool.annotations and tool.annotations.readOnlyHint),
    }


async def list_source_tools(session):
    tools = []
    cursor = None
    seen = set()
    while True:
        page = await session.list_tools(cursor=cursor)
        tools.extend(page.tools)
        if len(tools) > 64:
            raise ValueError("MCP source has too many tools")
        cursor = page.nextCursor
        if not cursor:
            return tools
        if cursor in seen:
            raise ValueError("MCP source repeated pagination cursor")
        seen.add(cursor)


class ExternalTool(Tool):
    def __init__(self, definition, executor):
        self.definition = definition
        self.executor = executor
        self.call_id = None

    @property
    def name(self):
        return self.definition["registered_name"]

    @property
    def description(self):
        return self.definition["description"]

    @property
    def parameters(self):
        return self.definition["schema"]

    @property
    def exclusive(self):
        return True

    async def execute(self, **kwargs):
        return canonical(await self.executor(self.name, kwargs, self.call_id))


class Components:
    def __init__(self, host):
        self.host = host
        self.store = host.store
        self.root = self.store.root / "components"
        self.root.mkdir(exist_ok=True)
        with self.store.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS component_sources (id TEXT PRIMARY KEY,spec TEXT NOT NULL,tools TEXT NOT NULL,checked REAL NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS component_calls (id TEXT PRIMARY KEY,status TEXT NOT NULL,result TEXT)"
            )
        if self.store.meta("component_configuration") is None:
            self.store.set_meta(
                "component_configuration",
                {
                    "revision": 0,
                    "disabled_tools": [],
                    "enabled_skills": list(host.profile.harness.enabled_skills),
                    "external": [],
                },
            )
        self.active = self.store.meta("component_configuration")

    def definitions(self):
        return {d["registered_name"]: d for src in self.active["external"] for d in src["tools"]}

    def effective_tools(self, config):
        from .harness_scripts import available_scripts

        names = set(self.host.base_binding["tools"]) - set(config["disabled_tools"])
        if not available_scripts(config["enabled_skills"]):
            names.discard("run_skill_script")
        names.update(d["registered_name"] for s in config["external"] for d in s["tools"])
        return names

    def validate_dependencies(self, config):
        skills = {s["id"]: s for s in self.skills()}
        available = self.effective_tools(config)
        for name in config["enabled_skills"]:
            if name not in skills or skills[name]["missing"]:
                raise ValueError("Skill is missing or has unmet dependencies")
            if set(skills[name]["required_tools"]) - available:
                raise ValueError("Skill requires tools that are not callable")

    def skills(self):
        roots = [(Path(__file__).parent / "skills", "bundled"), (self.root / "skills", "imported")]
        result = []
        for root, origin in roots:
            if not root.exists():
                continue
            for folder in sorted(root.iterdir()):
                if not folder.is_dir() or not (folder / "SKILL.md").is_file():
                    continue
                raw = (folder / "SKILL.md").read_text()
                meta, req = skill_metadata(raw)
                missing = ["command:" + x for x in req.get("bins", []) if not shutil.which(x)]
                if req.get("anyBins") and not any(shutil.which(x) for x in req["anyBins"]):
                    missing.append("any command:" + ",".join(req["anyBins"]))
                missing += ["environment:" + x for x in req.get("env", []) if not os.environ.get(x)]
                required_tools = req.get("tools", [])
                scripts = (
                    [
                        str(p.relative_to(folder))
                        for p in (folder / "scripts").rglob("*")
                        if p.is_file()
                    ]
                    if (folder / "scripts").exists()
                    else []
                )
                result.append(
                    {
                        "id": folder.name,
                        "title": meta.get("name", folder.name),
                        "description": meta.get("description", ""),
                        "origin": origin,
                        "enabled": folder.name in self.active["enabled_skills"],
                        "missing": missing,
                        "scripts": scripts,
                        "required_tools": required_tools,
                        "script_execution": "built-in reviewed registry only; imported scripts require an explicit tool adapter",
                        "digest": digest(
                            {
                                str(p.relative_to(folder)): hashlib.sha256(
                                    p.read_bytes()
                                ).hexdigest()
                                for p in folder.rglob("*")
                                if p.is_file()
                            }
                        ),
                    }
                )
        return result

    def import_skill(self, args):
        if bool(args.path) == bool(args.zip_base64):
            raise ValueError("choose directory or zip")
        with tempfile.TemporaryDirectory(dir=self.root) as tmp:
            staging = Path(tmp)
            if args.path:
                source = Path(args.path).expanduser().resolve()
                if not source.is_dir():
                    raise ValueError("Skill directory missing on selected node")
                files = list(source.rglob("*"))
                if any(p.is_symlink() for p in files):
                    raise ValueError("Skill symlinks are not accepted")
                if (
                    len(files) > 300
                    or sum(p.stat().st_size for p in files if p.is_file()) > 20_000_000
                ):
                    raise ValueError("Skill package too large")
                shutil.copytree(source, staging / "package")
                folder = staging / "package"
            else:
                with zipfile.ZipFile(
                    io.BytesIO(base64.b64decode(args.zip_base64, validate=True))
                ) as z:
                    if (
                        len(z.infolist()) > 300
                        or sum(x.file_size for x in z.infolist()) > 20_000_000
                    ):
                        raise ValueError("Skill archive too large")
                    for info in z.infolist():
                        target = (staging / info.filename).resolve()
                        if (
                            not target.is_relative_to(staging)
                            or (info.external_attr >> 16) & 0o170000 == 0o120000
                        ):
                            raise ValueError("invalid Skill archive path")
                    z.extractall(staging)
                matches = list(staging.rglob("SKILL.md"))
                if len(matches) != 1:
                    raise ValueError("zip must contain exactly one Skill")
                folder = matches[0].parent
            main = folder / "SKILL.md"
            if not main.is_file() or main.stat().st_size > 100_000:
                raise ValueError("SKILL.md missing or too large")
            meta, _ = skill_metadata(main.read_text())
            name = meta["name"]
            if any(s["id"] == name for s in self.skills()):
                raise Conflict(
                    "Skill already installed; remove/update explicitly rather than overwrite"
                )
            if any(
                p.name in {".env", "node-token", "model-key", "id_rsa", "id_ed25519"}
                or p.suffix in {".pem", ".key"}
                for p in folder.rglob("*")
            ):
                raise ValueError("remove credential files from Skill package")
            dest = self.root / "skills" / name
            dest.parent.mkdir(exist_ok=True)
            folder.rename(dest)
        self.store.audit(None, "component.skill.imported", {"skill": name, "enabled": False})
        return next(s for s in self.skills() if s["id"] == name)

    async def probe(self, spec):
        value = spec.model_dump()
        tools = []
        if spec.headers_file and Path(spec.headers_file).expanduser().resolve().is_relative_to(
            (self.store.root / "harness/workspace").resolve()
        ):
            raise ValueError("credential files must stay outside the model workspace")
        async with session_for(value) as session:
            for t in await list_source_tools(session):
                d = public_schema(t)
                d["registered_name"] = (
                    "ext_"
                    + spec.id[:15]
                    + "_"
                    + re.sub("[^a-zA-Z0-9_]", "_", t.name)[:25]
                    + "_"
                    + hashlib.sha256((spec.id + ":" + t.name).encode()).hexdigest()[:10]
                )
                tools.append(d)
        if len({d["name"] for d in tools}) != len(tools):
            raise ValueError("duplicate tool names")
        if any(secret in canonical(tools) for secret in secret_values(value)):
            raise ValueError("source returned credential values in tool metadata")
        with self.store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO component_sources VALUES (?,?,?,?)",
                (spec.id, canonical(value), canonical(tools), self.store.clock()),
            )
        return {"id": spec.id, "tools": tools, "checked_at": self.store.clock(), "applied": False}

    def catalog(self):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM component_sources").fetchall()
        sources = [
            {**json.loads(r["spec"]), "tools": json.loads(r["tools"]), "checked_at": r["checked"]}
            for r in rows
        ]
        base = self.host.base_binding["tools"]
        from .contracts import TOOL_CONTRACTS
        from .harness_contracts import TOOLS

        contracts = {**TOOL_CONTRACTS, **TOOLS}

        def tool_info(name):
            native = (
                self.host.runtime.tools.get(name) if hasattr(self.host.runtime, "tools") else None
            )
            return {
                "name": name,
                "enabled": name in self.host.binding["tools"],
                "required": name in REQUIRED,
                "description": contracts[name][1]
                if name in contracts
                else native.description
                if native
                else "原生文件工具；启用后查看实际调用说明。",
                "parameters": contracts[name][0].model_json_schema()
                if name in contracts
                else native.parameters
                if native
                else None,
            }

        return {
            "side": self.host.side,
            "revision": self.active["revision"],
            "configuration_digest": digest(self.active),
            "builtins": [tool_info(n) for n in base],
            "skills": self.skills(),
            "sources": sources,
            "active": self.active,
            "note": "Save imports first, then explicitly apply. Imported scripts are not automatically executed.",
        }

    def remove(self, args):
        if self.host.configuration_lock.locked():
            raise Conflict("wait for the current run or configuration change")
        if args.kind == "source":
            if any(x["spec"]["id"] == args.id for x in self.active["external"]):
                raise Conflict("disable source tools and apply before removing")
            with self.store.connect() as db:
                db.execute("DELETE FROM component_sources WHERE id=?", (args.id,))
        else:
            if args.id in self.active["enabled_skills"]:
                raise Conflict("disable Skill and apply before removing")
            folder = self.root / "skills" / args.id
            if not folder.is_dir():
                raise ValueError("only imported Skills can be removed")
            import uuid

            archive = self.root / "removed"
            archive.mkdir(exist_ok=True)
            folder.rename(archive / (args.id + "-" + uuid.uuid4().hex))
            workspace = self.store.root / "harness/workspace/skills" / args.id
            if workspace.is_dir():
                shutil.rmtree(workspace)
        self.store.audit(None, "component.removed", args.model_dump())
        return {"removed": True}

    async def apply(self, args):
        if self.host.configuration_lock.locked():
            raise Conflict("model is running; apply after this turn finishes")
        async with self.host.configuration_lock:
            if args.expected_revision != self.active["revision"]:
                raise Conflict("configuration changed; refresh before applying")
            with self.store.connect() as db:
                if (
                    db.execute(
                        "SELECT 1 FROM harness_events WHERE status IN ('queued','running') LIMIT 1"
                    ).fetchone()
                    or db.execute(
                        "SELECT 1 FROM harness_jobs WHERE status IN ('queued','running') LIMIT 1"
                    ).fetchone()
                ):
                    raise Conflict("work is running or queued; apply after it finishes")
            disabled = set(args.disabled_tools)
            if disabled & REQUIRED or disabled - set(self.host.base_binding["tools"]):
                raise ValueError("required or unknown tool cannot be disabled")
            skills = {s["id"]: s for s in self.skills()}
            if any(n not in skills or skills[n]["missing"] for n in args.enabled_skills):
                raise ValueError("Skill is missing or has unmet dependencies")
            external = []
            for source_id, modes in args.external_tools.items():
                if not modes:
                    continue
                with self.store.connect() as db:
                    row = db.execute(
                        "SELECT * FROM component_sources WHERE id=?", (source_id,)
                    ).fetchone()
                if row is None:
                    raise ValueError("probe source before enabling tools")
                definitions = {x["name"]: x for x in json.loads(row["tools"])}
                chosen = []
                for name, mode in modes.items():
                    if name not in definitions or mode not in {"read_only", "task_action"}:
                        raise ValueError("unknown tool or permission")
                    chosen.append({**definitions[name], "mode": mode})
                external.append({"spec": json.loads(row["spec"]), "tools": chosen})
            if sum(len(s["tools"]) for s in external) > 24:
                raise ValueError("enable at most 24 external tools per node")
            names = [d["registered_name"] for s in external for d in s["tools"]]
            if len(names) != len(set(names)) or set(names) & set(self.host.base_binding["tools"]):
                raise ValueError("registered tool name collision; recheck sources")
            proposed = {
                "revision": self.active["revision"] + 1,
                "disabled_tools": sorted(disabled),
                "enabled_skills": sorted(set(args.enabled_skills)),
                "external": external,
            }
            self.validate_dependencies(proposed)
            old = self.active
            self.active = proposed
            try:
                await self.host.reload_components()
            except BaseException:
                self.active = old
                await self.host.reload_components()
                raise
            self.store.set_meta("component_configuration", proposed)
            self.store.audit(
                None,
                "component.configuration.applied",
                {
                    "revision": proposed["revision"],
                    "configuration_digest": digest(proposed),
                    "tools": self.host.binding["tools"],
                },
            )
            return self.catalog()

    def prepare_runtime(self):
        self.validate_dependencies(self.active)
        workspace = self.store.root / "harness/workspace/skills"
        workspace.mkdir(parents=True, exist_ok=True)
        for name in self.active["enabled_skills"]:
            src = self.root / "skills" / name
            if src.is_dir():
                shutil.copytree(src, workspace / name, dirs_exist_ok=True)
        settings = self.host.profile.harness.model_copy(
            update={"enabled_skills": self.active["enabled_skills"]}
        )
        return self.host.profile.model_copy(update={"harness": settings})

    async def invoke(self, event, name, args):
        try:
            return await self._invoke(event, name, args)
        except Conflict:
            raise
        except Exception:
            raise Conflict(
                "external tool connection or execution failed; inspect source, do not assume completion"
            ) from None

    async def _invoke(self, event, name, args):
        source = next(
            x
            for x in self.active["external"]
            if any(d["registered_name"] == name for d in x["tools"])
        )
        definition = next(d for d in source["tools"] if d["registered_name"] == name)
        action = definition["mode"] == "task_action"
        if self.store.event(event["id"], event["principal"])["status"] != "running":
            raise Conflict("input no longer active")
        if action:
            if event["read_only"] or not event.get("task_id"):
                raise Conflict("external action requires a bound authorized task")
            task = self.store.business_task(event["task_id"], event["principal"])
            expected = self.store.event_versions(event["id"]).get(task["id"]) or event.get(
                "_task_versions", {}
            ).get(task["id"])
            if (
                task["status"] in {"paused", "cancelled", "completed", "expired"}
                or expected != task["version"]
            ):
                raise Conflict("task changed or ended")
            self.store.guard_job({"task_id": task["id"], "version": expected})
        key = digest({"event": event["id"], "tool": name, "args": args})
        async with session_for(source["spec"]) as session:
            tools = await list_source_tools(session)
            actual = next((public_schema(t) for t in tools if t.name == definition["name"]), None)
            if not actual or any(
                actual[k] != definition[k] for k in ["name", "description", "schema"]
            ):
                raise Conflict("MCP tool changed; probe and apply its new definition")
            if self.store.event(event["id"], event["principal"])["status"] != "running":
                raise Conflict("input ended while connecting")
            if action:
                current = self.store.business_task(event["task_id"], event["principal"])
                if current["version"] != expected or current["status"] in {
                    "paused",
                    "cancelled",
                    "completed",
                    "expired",
                }:
                    raise Conflict("task ended while connecting")
                self.store.guard_job({"task_id": current["id"], "version": expected})
                with self.store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    previous = db.execute(
                        "SELECT * FROM component_calls WHERE id=?", (key,)
                    ).fetchone()
                    if previous:
                        if previous["status"] == "done":
                            return json.loads(previous["result"])
                        raise Conflict(
                            "external action outcome unknown; inspect source before retrying"
                        )
                    db.execute("INSERT INTO component_calls VALUES (?,'started',NULL)", (key,))
            result = await session.call_tool(definition["name"], args)
            value = result.model_dump(mode="json")
            raw = canonical(value)
            for secret in secret_values(source["spec"]):
                raw = raw.replace(json.dumps(secret)[1:-1], "[redacted]").replace(
                    secret, "[redacted]"
                )
            value = json.loads(raw)
            if result.isError:
                raise Conflict("external tool reported failure; inspect its source")
            if len(raw) > 32000:
                value = {
                    "truncated": True,
                    "reason": "external result exceeded 32000 characters; request a smaller result",
                    "original_characters": len(raw),
                }
        result = {
            "source": source["spec"]["id"],
            "tool": definition["name"],
            "result": value,
            "authority": "external_tool_output_not_media_completion",
        }
        if action:
            with self.store.connect() as db:
                db.execute(
                    "UPDATE component_calls SET status='done',result=? WHERE id=?",
                    (canonical(result), key),
                )
        return result
