"""Native visual model output stays separate from task rules and tool receipts."""

from __future__ import annotations

import base64
import uuid
from pathlib import Path

import httpx

from .contracts import canonical
from .qwen_observation_provider import QwenModelProcess
from .store import Conflict
from .visual_contracts import ContrastiveRule, VisualRequest, VisualResult, evaluate_rule


class NativeVisualProcess(QwenModelProcess):
    async def _health(self, instance):
        value = await super()._health(instance)
        if value:
            value["engine"] = "pytorch-native-visual"
        return value


class NativeVisualScorer:
    score_mode = "native_visual_then_task_rule"

    def __init__(
        self,
        model,
        process,
        payloads,
        principal,
        task_id,
        audit,
        guard,
        *,
        client=None,
        inference_simulated=False,
    ):
        self.model, self.process, self.payloads = model, process, payloads
        self.principal, self.task_id, self.audit, self.guard = principal, task_id, audit, guard
        self.client = client or httpx.AsyncClient(
            base_url=process.endpoint, timeout=120, trust_env=False
        )
        self.owns_client = client is None
        self.inference_simulated = inference_simulated

    async def initialize(self):
        response = await self.client.get("/props")
        response.raise_for_status()
        props = response.json()
        self.alias = self.process._read()["instance"]
        if (props.get("model_alias"), props.get("family"), props.get("revision")) != (
            self.alias,
            self.model.family,
            self.model.revision,
        ):
            raise Conflict("native visual runtime differs from installed capability")

    def estimate(self, messages):
        return 0  # Non-chat models consume only current frames, not conversation history.

    async def score(self, messages, rule):
        self.guard()
        refs = self.payloads.references(messages)
        if len(refs) != 1:
            raise Conflict("native visual model requires one current media batch")
        media = self.payloads.resolve(self.principal, self.task_id, refs[0])
        manifest, root = media["manifest"], Path(media["directory"])
        frames = [
            {
                "sha256": frame["sha256"],
                "sample_at_ms": frame["sample_at_ms"],
                "jpeg_base64": base64.b64encode(
                    self.payloads.image_bytes(media, frame)
                ).decode(),
            }
            for frame in manifest["frames"]
        ]
        request = VisualRequest(
            request_id="native-visual-" + uuid.uuid4().hex,
            model_alias=self.alias,
            family=self.model.family,
            revision=self.model.revision,
            frames=frames,
            texts=(rule.positive_text, rule.negative_text)
            if isinstance(rule, ContrastiveRule)
            else None,
            question=None if isinstance(rule, ContrastiveRule) else rule.question,
        )
        wire = canonical(request).encode()
        if len(wire) > 16 * 1024**2:
            raise Conflict("native visual batch exceeds request budget")
        self.audit(
            "model.request",
            {
                "request_id": request.request_id,
                "body": request.model_dump(
                    mode="json", exclude={"frames": {"__all__": {"jpeg_base64"}}}
                ),
                "image_bytes_persisted": False,
                "tools": [],
                "messages": None,
                "submitted_images": manifest["frames"],
                "native_result_type": self.model.result_type,
            },
        )

        async def invoke():
            async with self.client.stream(
                "POST", "/observe", content=wire, headers={"Content-Type": "application/json"}
            ) as response:
                response.raise_for_status()
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 65536:
                        raise Conflict("native visual response exceeds budget")
            return raw

        run_request = getattr(self.process, "request", None)
        try:
            raw = await run_request(invoke()) if run_request else await invoke()
        except httpx.HTTPError as exc:
            message = str(exc)
            secret = self.client.headers.get("authorization", "").removeprefix("Bearer ")
            if secret:
                message = message.replace(secret, "[redacted]")
            self.audit("model.response", {
                "request_id": request.request_id, "error": True,
                "error_type": type(exc).__name__, "error_message": message[:1000],
                "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else None,
                "result_received": False,
            })
            raise
        result = VisualResult.model_validate_json(raw)
        if (
            result.request_id,
            result.model_alias,
            result.family,
            result.revision,
            result.simulated,
        ) != (
            request.request_id,
            request.model_alias,
            request.family,
            request.revision,
            self.inference_simulated,
        ) or [(f.sha256, f.sample_at_ms) for f in result.frames] != [
            (f.sha256, f.sample_at_ms) for f in request.frames
        ]:
            raise Conflict("native visual response differs from submitted frame/model provenance")
        self.audit(
            "model.response",
            {
                "request_id": request.request_id,
                "response": result.model_dump(mode="json"),
            },
        )
        self.guard()
        branch, per_frame = evaluate_rule(rule, result)
        # Record the observed native answer/scores, not the user's desired target as fact.
        text = canonical(
            {
                "first_sample": result.frames[0].model_dump(mode="json", exclude_none=True),
                "sample_count": len(result.frames),
                "sampled_only": True,
                "full_result_ref": request.request_id,
            }
        )
        selected = {
            "evidence_ref": request.request_id,
            "selected_id": branch,
            "selected_label": branch,
            "branch": branch,
            "generated_token": None,
            "inference_simulated": self.inference_simulated,
            "observations": [],
            "description": {"text": text},
            "native_result": result.model_dump(mode="json"),
            "rule": rule.model_dump(mode="json"),
            "per_frame_routes": per_frame,
            "selection_source": "versioned_task_rule",
            "score_mode": self.score_mode,
        }
        self.audit("perception.rule", selected)
        return selected

    async def close(self):
        if self.owns_client:
            await self.client.aclose()


class NativeVisualBackend:
    required_modalities = ("image",)
    inference_simulated = False
    environment_sound = False
    native_visual = True
    score_mode = NativeVisualScorer.score_mode

    def __init__(self, model):
        self.model = model

    def process(self, root):
        return NativeVisualProcess(
            root, self.model.command(), self.model.port, startup_timeout=180, stop_timeout=2
        )

    def scorer(self, process, payloads, principal, task_id, audit, guard):
        return NativeVisualScorer(self.model, process, payloads, principal, task_id, audit, guard)
