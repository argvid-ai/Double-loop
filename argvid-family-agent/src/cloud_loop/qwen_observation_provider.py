"""Qwen CPU visual option scoring, with honest one-token backend accounting.

This is not MiniCPM's native audiovisual protocol. Actual option probabilities
are read from llama-server; generated labels and self-reported scores are never
used as the semantic decision. The provider does not execute business tools.
"""

from __future__ import annotations

import asyncio
import base64
import math
import uuid
from copy import deepcopy
from pathlib import Path

import httpx

from .observation_payloads import MEDIA_TYPE
from .observation_process import NativeModelProcess, identity
from .store import Conflict


class QwenModelProcess(NativeModelProcess):
    """Reuse exclusive ownership, CLI identity and cancellation, not omni health."""

    async def _health(self, instance):
        record = self._read()
        pid = record.get("pid") if record else None
        if not pid and self.child is not None:
            pid = self.child.pid
        if not pid or await asyncio.to_thread(identity, pid, instance) is None:
            return None
        try:
            response = await self.client.get("/health")
            response.raise_for_status()
            if response.json().get("status") != "ok":
                return None
            props = await self.client.get("/props")
            props.raise_for_status()
            value = props.json()
            if value.get("model_alias") != instance:
                return None
            return {
                "pid": pid,
                "instance_id": instance,
                "engine": "llama.cpp-qwen-vision",
                "proof": "OS_owned_PID_and_model_alias_not_omni_receipt",
            }
        except (httpx.HTTPError, ValueError, AttributeError):
            return None


class QwenOptionScorer:
    inference_simulated = False
    supported_modalities = ("image", "text")
    score_mode = "native_option_probabilities_with_one_generated_token"

    def __init__(
        self,
        endpoint,
        payloads,
        principal,
        task_id,
        model_alias,
        audit,
        *,
        client=None,
        asset_guard=lambda: None,
        inference_simulated=False,
        request_runner=None,
    ):
        if type(inference_simulated) is not bool:
            raise ValueError("explicit inference provenance is required")
        self.inference_simulated = inference_simulated
        self.payloads, self.principal, self.task_id = payloads, principal, task_id
        self.model_alias, self.audit, self.asset_guard = model_alias, audit, asset_guard
        self.client = client or httpx.AsyncClient(base_url=endpoint, timeout=120, trust_env=False)
        self.owns_client = client is None
        self.tokens = {}
        self.request_runner = request_runner

    async def initialize(self):
        for label in "ABC":
            response = await self.client.post(
                "/tokenize", json={"content": label, "add_special": False, "with_pieces": True}
            )
            response.raise_for_status()
            tokens = response.json().get("tokens")
            if not isinstance(tokens, list) or len(tokens) != 1:
                raise Conflict("semantic alternatives must each tokenize to one token")
            value = tokens[0]
            ident = value.get("id") if isinstance(value, dict) else value
            if type(ident) is not int or ident < 0:
                raise Conflict("invalid semantic token identity")
            self.tokens[label] = ident
        if len(set(self.tokens.values())) != 3:
            raise Conflict("semantic alternatives share a token identity")
        self.audit(
            "semantic.tokenizer",
            {
                "labels": self.tokens,
                "score_mode": self.score_mode,
                "zero_generation": False,
                "audio_understanding": False,
            },
        )

    def expand(self, messages):
        """Rehydrate immutable logical media into actual OpenAI image bytes."""
        references = self.payloads.validate_messages(self.principal, self.task_id, messages)
        wire, submitted = [], []
        for original in messages:
            message = {
                key: deepcopy(value)
                for key, value in original.items()
                if key in {"role", "content"}
            }
            content = message.get("content")
            if isinstance(content, list):
                expanded = []
                for block in content:
                    if block.get("type") == "text":
                        expanded.append(deepcopy(block))
                    elif block.get("type") == MEDIA_TYPE:
                        value = references[block["id"]]
                        manifest, directory = value["manifest"], Path(value["directory"])
                        for frame in manifest["frames"]:
                            raw = (directory / frame["file"]).read_bytes()
                            expanded.extend(
                                [
                                    {
                                        "type": "text",
                                        "text": "媒体采样时间："
                                        + str(frame["sample_at_ms"])
                                        + "毫秒",
                                    },
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": "data:image/jpeg;base64,"
                                            + base64.b64encode(raw).decode()
                                        },
                                    },
                                ]
                            )
                            submitted.append(
                                {
                                    "payload_id": block["id"],
                                    "sha256": frame["sha256"],
                                    "sample_at_ms": frame["sample_at_ms"],
                                    **{
                                        key: frame[key]
                                        for key in (
                                            "image_region",
                                            "region_pixels",
                                            "original_display_size",
                                        )
                                        if key in frame
                                    },
                                }
                            )
                    else:
                        raise Conflict("unsupported visual scoring block")
                message["content"] = expanded
            wire.append(message)
        return wire, submitted

    def estimate(self, messages):
        # Budgeting needs frame counts, not another read/hash of every JPEG.
        # expand() still verifies actual bytes immediately before submission.
        references = {
            ident: self.payloads.describe(self.principal, self.task_id, ident)
            for ident in dict.fromkeys(self.payloads.references(messages))
        }
        # An explicit conservative bound, not a claim about backend cache reuse.
        count = 512
        for message in messages:
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if block.get("type") == MEDIA_TYPE:
                    count += 256 * len(references[block["id"]]["manifest"]["frames"])
                elif block.get("type") == "text":
                    count += len(block["text"].encode())
            if not isinstance(content, list):
                count += len(str(content).encode())
        return count

    def parse(self, response, decision):
        """Read all alternatives; do not infer absent or nonfinite scores."""
        try:
            choice = response["choices"][0]
            records = choice["logprobs"]["content"]
            usage = response["usage"]
            if (
                len(records) != 1
                or type(usage["completion_tokens"]) is not int
                or usage["completion_tokens"] != 1
            ):
                raise ValueError("expected exactly one backend-generated token")
            rows = records[0].get("top_probs") or records[0].get("top_logprobs")
            if not isinstance(rows, list):
                raise ValueError("native option scores unavailable")
            values = {}
            for label, ident in self.tokens.items():
                matches = [
                    row for row in rows if row.get("id") == ident and row.get("token") == label
                ]
                if len(matches) != 1:
                    raise ValueError("missing or ambiguous candidate score")
                row = matches[0]
                if "prob" in row:
                    value = row["prob"]
                    if (
                        type(value) not in {int, float}
                        or not math.isfinite(value)
                        or not 0 <= value <= 1
                    ):
                        raise ValueError("invalid native probability")
                else:
                    score = row["logprob"]
                    if type(score) not in {int, float} or not math.isfinite(score) or score > 0:
                        raise ValueError("invalid native log probability")
                    value = math.exp(score)
                values[label] = value
            denominator = sum(values.values())
            if not math.isfinite(denominator) or denominator <= 0:
                raise ValueError("empty native probability mass")
            probabilities = {label: value / denominator for label, value in values.items()}
            label = max(probabilities, key=probabilities.get)
            options = dict(decision.labelled())
            selected = options[label]
            emitted = choice["message"].get("content")
            if emitted not in self.tokens:
                raise ValueError("backend did not obey single-token candidate grammar")
            return {
                "selected_label": label,
                "selected_id": selected.id,
                "branch": selected.branch,
                "option_probabilities": {
                    options[key].id: value for key, value in probabilities.items()
                },
                "generated_token": emitted,
                "generated_tokens": usage["completion_tokens"],
                "selection_source": "argmax_actual_native_option_scores_not_generated_label",
                "score_basis": "post_sampling" if records[0].get("top_probs") else "pre_sampling",
                "usage": usage,
                "score_mode": self.score_mode,
                "calibrated": False,
            }
        except (KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
            raise Conflict("invalid or unavailable native semantic scores: " + str(exc)) from None

    async def score(self, messages, decision):
        if set(self.tokens) != set("ABC"):
            raise Conflict("semantic scorer not initialized")
        self.asset_guard()
        wire, images = self.expand(messages)
        if not images:
            raise Conflict("visual semantic decision requires actual images")
        body = {
            "model": self.model_alias,
            "messages": wire,
            "max_tokens": 1,
            "temperature": 1,
            "seed": 1234,
            "stream": False,
            "logprobs": True,
            "top_logprobs": 20,
            "post_sampling_probs": True,
            "top_k": 0,
            "top_p": 1,
            "min_p": 0,
            "typical_p": 1,
            "repeat_penalty": 1,
            "presence_penalty": 0,
            "frequency_penalty": 0,
            "grammar": 'root ::= "A" | "B" | "C"',
            "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        request_id = "qwen-score-" + uuid.uuid4().hex
        self.audit(
            "model.request",
            {
                "request_id": request_id,
                "capture_boundary": "actual_http_request",
                "body": body,
                "messages": wire,
                "tools": [],
                "submitted_images": images,
                "audio_samples_submitted": 0,
                "score_mode": self.score_mode,
            },
        )
        request = self.client.post("/v1/chat/completions", json=body)
        response = await self.request_runner(request) if self.request_runner else await request
        response.raise_for_status()
        value = response.json()
        self.audit(
            "model.response",
            {
                "request_id": request_id,
                "response": value,
                "inference_simulated": self.inference_simulated,
            },
        )
        self.asset_guard()
        if self.request_runner and value.get("model") != self.model_alias:
            raise Conflict("external Qwen response model identity differs from configured service")
        selected = self.parse(value, decision)
        selected.update(
            evidence_ref=request_id,
            submitted_image_count=len(images),
            audio_samples_submitted=0,
            inference_simulated=self.inference_simulated,
        )
        self.audit("semantic.decision", selected)
        return selected

    async def close(self):
        if self.owns_client:
            await self.client.aclose()
