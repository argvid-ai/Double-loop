"""Task-scoped HTTP connection to an independently deployed model service.

No process adoption, launch or termination. In-flight calls drain on cancellation;
a lost response remains explicitly unconfirmed rather than claiming that the
service stopped computing. Deployment owns recovery of that uncertain state.
"""

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, field_validator, model_validator

from .contracts import Contract
from .store import Conflict


class ModelServicePreset(Contract):
    backend: Literal["model_service_http"]
    title: str = Field(min_length=1, max_length=100)
    family: Literal["clip", "blip_vqa", "qwen"]
    endpoint: str
    key_file: str | None = Field(default=None, min_length=1)
    model_alias: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    revision: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")
    context_tokens: int = Field(default=8192, strict=True, ge=2048, le=32768)
    output_tokens: int = Field(default=1, strict=True, ge=1, le=12)
    request_timeout: float = Field(default=120, gt=0, le=600, allow_inf_nan=False)

    @field_validator("endpoint")
    @classmethod
    def service_endpoint(cls, value):
        url = urlsplit(value)
        _ = url.port
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("model service URL must not contain credentials, query or fragment")
        return value.rstrip("/")

    @model_validator(mode="after")
    def contract(self):
        if self.family != "qwen" and self.revision is None:
            raise ValueError("native visual service requires an expected model revision")
        return self

    @property
    def result_type(self):
        return "cosine_similarities" if self.family == "clip" else "visual_answer"


class ModelServiceConnection:
    def __init__(self, spec, root, *, client=None):
        self.spec, self.endpoint = spec, spec.endpoint
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "external-requests.json"
        self.pending = {}
        self.uncertain = set(json.loads(self.path.read_text())) if self.path.exists() else set()
        self.cleanup = None
        self.ready = self.released = False
        self.last_contact = None
        headers = {}
        if spec.key_file:
            key = Path(spec.key_file).expanduser().read_text().strip()
            if not key or any(c.isspace() or not c.isascii() for c in key):
                raise ValueError("model service API key must be a nonempty ASCII token")
            headers["Authorization"] = "Bearer " + key
        self.client = client or httpx.AsyncClient(
            base_url=self.endpoint,
            timeout=spec.request_timeout,
            headers=headers,
            trust_env=False,
            follow_redirects=False,
        )
        if client is not None:
            self.client.headers.update(headers)
        self.owns_client = client is None

    def _save(self):
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(sorted(self.uncertain | set(self.pending))))
        temp.replace(self.path)

    async def reset(self):
        if self.pending or (self.uncertain and not await self._reconcile_readonly()):
            raise Conflict(
                "external model requests remain unconfirmed; deployment recovery required"
            )
        response = await self.client.get("/health")
        response.raise_for_status()
        props = await self.client.get("/props")
        props.raise_for_status()
        value = props.json()
        if (
            response.json().get("status") != "ok"
            or value.get("model_alias") != self.spec.model_alias
        ):
            raise Conflict("external model service identity differs from configured capability")
        if self.spec.family != "qwen" and (value.get("family"), value.get("revision")) != (
            self.spec.family,
            self.spec.revision,
        ):
            raise Conflict("external model family/revision mismatch")
        self.last_contact = time.time()
        self.ready = True
        self.released = False
        return True

    async def _reconcile_readonly(self):
        """A verified, idle, pure visual service can safely recompute a lost reply.

        No such inference is made for chat/action services or old servers lacking
        the explicit idle/read-only contract. The old task receipt is not invented.
        """
        if self.pending or self.spec.family not in {"clip", "blip_vqa"}:
            return False
        try:
            response = await self.client.get("/props")
            response.raise_for_status()
            value = response.json()
            if not (
                value.get("read_only") is True
                and value.get("inflight") == 0
                and isinstance(value.get("instance_id"), str)
                and value["instance_id"]
                and (value.get("model_alias"), value.get("family"), value.get("revision"))
                == (self.spec.model_alias, self.spec.family, self.spec.revision)
            ):
                return False
        except (httpx.HTTPError, ValueError):
            return False
        self.uncertain.clear()
        self._save()
        return True

    def _read(self):
        return {"instance": self.spec.model_alias}

    async def request(self, operation):
        ident = uuid.uuid4().hex

        async def execute():
            try:
                response = await operation
                self.last_contact = time.time()
                return response
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                # No connection was established/acquired, so this individual
                # request never reached inference. Do not fence recovery for it.
                # Read/write/protocol failures below remain ambiguous.
                self.ready = False
                raise
            except httpx.TransportError:
                self.ready = False
                self.uncertain.add(ident)
                raise
            finally:
                self.pending.pop(ident, None)
                self._save()

        task = asyncio.create_task(execute())
        self.pending[ident] = task
        self._save()
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return await asyncio.shield(task)

    async def status(self):
        return {
            "state": "recovery_pending"
            if self.uncertain
            else "server_ready"
            if self.ready
            else "not_started",
            "server_reachable": True if self.ready else None,
            "externally_managed": True,
            "reachability_source": "last_successful_contact_not_live_probe",
            "last_successful_contact": self.last_contact,
            "service_terminated": False,
            "pending_requests": len(self.pending),
            "unconfirmed_requests": len(self.uncertain),
            "proof": "configured_HTTP_alias_not_OS_process_ownership",
        }

    async def abort(self):
        if not self.pending and self.uncertain:
            await self._reconcile_readonly()
        return not self.pending and not self.uncertain

    async def close(self):
        if self.cleanup is None or (
            self.cleanup.done()
            and (
                self.cleanup.cancelled()
                or self.cleanup.exception() is not None
                or self.cleanup.result() is not True
            )
        ):
            self.cleanup = asyncio.create_task(self._close())
        return await asyncio.shield(self.cleanup)

    async def _close(self):
        if self.pending:
            await asyncio.gather(
                *(asyncio.shield(t) for t in tuple(self.pending.values())), return_exceptions=True
            )
        self.released = await self.abort()
        if self.released:
            self.ready = False
            if self.owns_client:
                await self.client.aclose()
        return self.released


class ModelServiceBackend:
    required_modalities = ("image",)
    inference_simulated = False
    environment_sound = False

    def __init__(self, model):
        self.model = model
        self.native_visual = model.family != "qwen"
        self.score_mode = (
            "native_visual_then_task_rule"
            if self.native_visual
            else "native_option_probabilities_with_one_generated_token"
        )

    def process(self, root):
        return ModelServiceConnection(self.model, root)

    def scorer(self, process, payloads, principal, task_id, audit, guard):
        from .native_visual_provider import NativeVisualScorer
        from .qwen_observation_provider import QwenOptionScorer

        if self.native_visual:
            return NativeVisualScorer(
                self.model,
                process,
                payloads,
                principal,
                task_id,
                audit,
                guard,
                client=process.client,
            )
        return QwenOptionScorer(
            process.endpoint,
            payloads,
            principal,
            task_id,
            self.model.model_alias,
            audit,
            client=process.client,
            asset_guard=guard,
            request_runner=process.request,
        )
