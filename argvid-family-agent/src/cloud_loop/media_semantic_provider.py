"""Bounded semantic-media Provider contract, with explicit simulation and cancel.

Installing this adapter does not install a sound/vision model. An operator must
bind a loopback service which declares its real model, modalities and cancellation
capability. Request journals contain identifiers only, never credentials.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import Field, field_validator

from .contracts import Contract, canonical
from .observation_contracts import Reference
from .observation_evidence import EvidenceDescription
from .store import Conflict


class MediaProviderSpec(Contract):
    endpoint: str = Field(max_length=400)
    model: str = Field(min_length=1, max_length=160)
    modalities: tuple[str, ...] = ("audio",)
    capabilities: tuple[str, ...] = ("semantic_choices",)
    simulated: bool = Field(default=False, strict=True)
    key_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,100}$")
    timeout_seconds: int = Field(default=30, strict=True, ge=1, le=180)
    max_request_bytes: int = Field(default=16 * 1024**2, strict=True, ge=1024, le=32 * 1024**2)

    @field_validator("endpoint")
    @classmethod
    def local_endpoint(cls, value):
        parsed = urlsplit(value)
        _ = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("use a credential-free loopback Provider endpoint")
        try:
            local = (
                parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
            )
        except ValueError:
            local = False
        if not local:
            raise ValueError(
                "this local demo requires a loopback Provider or an explicit SSH tunnel"
            )
        return value.rstrip("/")

    @field_validator("modalities")
    @classmethod
    def supported_modalities(cls, value):
        if not value or len(set(value)) != len(value) or set(value) - {"image", "audio"}:
            raise ValueError("declare distinct image/audio input requirements")
        return tuple(v for v in ("image", "audio") if v in value)

    @field_validator("capabilities")
    @classmethod
    def supported_capabilities(cls, value):
        if not value or set(value) - {
            "semantic_choices",
            "transcription",
            "environment_sound",
            "scene_descriptions",
        }:
            raise ValueError("unsupported semantic-media capability declaration")
        return value


class MediaChoiceResult(Contract):
    request_id: Reference
    model: str = Field(min_length=1, max_length=160)
    choice_id: Reference
    simulated: bool = Field(strict=True)
    observations: tuple[EvidenceDescription, ...] = Field(default=(), max_length=8)
    transcript: str | None = Field(default=None, max_length=8000)
    usage: dict[str, int] = Field(default_factory=dict, max_length=16)


class MediaProviderConnection:
    def __init__(self, spec, root, *, client=None):
        self.spec = MediaProviderSpec.model_validate(spec.model_dump())
        self.root = Path(root).absolute()
        if self.root.resolve() != self.root:
            raise Conflict("Provider journal directory changed")
        self.root.mkdir(parents=True, exist_ok=True)
        self.journal = self.root / "requests.json"
        if self.journal.is_symlink():
            raise Conflict("Provider journal must not be a symlink")
        self.active = (
            set(json.loads(self.journal.read_text()).get("requests", []))
            if self.journal.exists()
            else set()
        )
        self.owns_client = client is None
        self.client = client or httpx.AsyncClient(
            base_url=self.spec.endpoint,
            timeout=self.spec.timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        self.ready = False
        self.released = False

    def _headers(self):
        if self.spec.key_env is None:
            return {}
        key = os.environ.get(self.spec.key_env)
        if not key:
            raise Conflict("configured Provider credential environment variable is unavailable")
        return {"Authorization": "Bearer " + key}

    def _save(self):
        if self.root.resolve() != self.root or self.journal.is_symlink():
            raise Conflict("Provider journal location changed")
        temporary = self.root / (uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("x") as output:
                temporary.chmod(0o600)
                output.write(canonical({"requests": sorted(self.active)}))
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(self.journal)
        finally:
            temporary.unlink(missing_ok=True)

    async def _json(self, method, path, body=None, *, timeout=None):
        async with self.client.stream(
            method,
            path,
            json=body,
            headers=self._headers(),
            timeout=timeout or self.spec.timeout_seconds,
        ) as response:
            response.raise_for_status()
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > 65536:
                    raise Conflict("Provider response exceeds bound")
        return json.loads(chunks)

    async def reset(self):
        if self.active and not await self.abort():
            raise Conflict("old Provider requests have not confirmed cancellation")
        health = await self._json("GET", "/health")
        if (
            health.get("model") != self.spec.model
            or health.get("ready") is not True
            or type(health.get("simulated")) is not bool
            or health["simulated"] != self.spec.simulated
            or not set(self.spec.modalities) <= set(health.get("modalities", []))
            or not set(self.spec.capabilities) <= set(health.get("capabilities", []))
            or health.get("cancellation_supported") is not True
        ):
            raise Conflict(
                "Provider model, modalities, simulation or cancellation contract is unavailable"
            )
        self.ready = True
        return True

    def _read(self):
        return {"instance": "external-" + self.spec.model}

    async def status(self):
        return {
            "state": "server_ready" if self.ready and not self.released else "stopped",
            "server_reachable": self.ready and not self.released,
            "externally_managed": True,
            "pending_requests": len(self.active),
            "inference_simulated": self.spec.simulated,
        }

    async def abort(self):
        for ident in tuple(self.active):
            try:
                reply = await self._json("POST", "/cancel", {"request_id": ident}, timeout=1)
            except (httpx.HTTPError, ValueError, Conflict):
                continue
            if reply.get("request_id") == ident and reply.get("state") in {
                "cancelled",
                "completed",
                "not_found",
            }:
                self.active.remove(ident)
        self._save()
        return not self.active

    async def close(self):
        closed = await self.abort()
        if closed and self.owns_client:
            await self.client.aclose()
        self.released = closed
        self.ready = False
        return closed


class MediaSemanticScorer:
    score_mode = "structured_media_provider"

    def __init__(
        self, connection, payloads, principal, task_id, audit, *, asset_guard=lambda: None
    ):
        self.connection, self.payloads = connection, payloads
        self.principal, self.task_id, self.audit = principal, task_id, audit
        self.asset_guard = asset_guard
        self.inference_simulated = connection.spec.simulated

    async def initialize(self):
        if "semantic_choices" not in self.connection.spec.capabilities:
            raise Conflict("Provider has no semantic choice capability")
        if not self.connection.ready:
            await self.connection.reset()

    def estimate(self, messages):
        return len(canonical(messages)) // 4 + len(self.payloads.references(messages)) * 1024

    async def score(self, messages, decision):
        self.asset_guard()
        refs = self.payloads.references([messages[-1]])
        if len(refs) != 1:
            raise Conflict("semantic media Provider requires one actual current payload")
        media = self.payloads.resolve(self.principal, self.task_id, refs[0])
        manifest, root = media["manifest"], Path(media["directory"])
        inputs = {}
        if "image" in self.connection.spec.modalities:
            if not manifest["frames"]:
                raise Conflict("configured Provider requires actual images")
            inputs["images"] = [
                {
                    **frame,
                    "jpeg_base64": base64.b64encode((root / frame["file"]).read_bytes()).decode(),
                }
                for frame in manifest["frames"]
            ]
        if "audio" in self.connection.spec.modalities:
            if not manifest.get("audio"):
                raise Conflict("configured Provider requires actual audio")
            sound = manifest["audio"]
            inputs["audio_wav"] = {
                **sound,
                "data_base64": base64.b64encode((root / sound["file"]).read_bytes()).decode(),
            }
        ident = "media-score-" + uuid.uuid4().hex
        body = {
            "schema": "harness.semantic-media/1",
            "request_id": ident,
            "model": self.connection.spec.model,
            "source": "observation_data_not_user_command",
            "question": decision.question,
            "choices": [c.model_dump() for c in decision.choices],
            "range_ms": [manifest["start_ms"], manifest["end_ms"]],
            "inputs": inputs,
            "recent_context": [m for m in messages[:-1] if m.get("role") != "system"],
        }
        if len(canonical(body).encode()) > self.connection.spec.max_request_bytes:
            raise Conflict("semantic media request exceeds configured bound")
        self.connection.active.add(ident)
        self.connection._save()
        self.audit(
            "model.request",
            {
                "request_id": ident,
                "body": body,
                "tools": [],
                "model_purpose": "semantic_media_provider",
                "inference_simulated": self.inference_simulated,
            },
        )
        result = MediaChoiceResult.model_validate(
            await self.connection._json("POST", "/score", body)
        )
        if (
            result.request_id != ident
            or result.model != self.connection.spec.model
            or result.simulated != self.inference_simulated
        ):
            raise Conflict("semantic media response differs from request provenance")
        choice = next((c for c in decision.choices if c.id == result.choice_id), None)
        if choice is None:
            raise Conflict("Provider selected an unregistered choice")
        self.audit(
            "model.response",
            {
                "request_id": ident,
                "response": result.model_dump(mode="json"),
                "inference_simulated": self.inference_simulated,
            },
        )
        self.connection.active.remove(ident)
        self.connection._save()
        label = next(label for label, item in decision.labelled() if item.id == choice.id)
        return {
            "selected_id": choice.id,
            "selected_label": label,
            "branch": choice.branch,
            "evidence_ref": ident,
            "generated_token": canonical(result),
            "generated_tokens": result.usage.get("completion_tokens"),
            "observations": [item.model_dump() for item in result.observations],
            "transcript": result.transcript,
            "inference_simulated": self.inference_simulated,
            "score_mode": self.score_mode,
            "selection_source": "validated_provider_choice_id",
        }

    async def close(self):
        # Connection lifetime belongs to the runner/process adapter.
        return None
