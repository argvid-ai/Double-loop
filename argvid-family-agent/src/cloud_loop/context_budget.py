"""Bound the final model request without changing authoritative task state."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlparse

import httpx

from .contracts import FACT_SNAPSHOT_PREFIX


class ContextBudgetExceeded(RuntimeError):
    def __init__(self, tokens: int, budget: int):
        super().__init__(f"protected context requires {tokens} tokens; input budget is {budget}")
        self.tokens, self.budget = tokens, budget


@dataclass(frozen=True)
class ContextBudget:
    capacity: int
    output: int
    margin: int = 512

    @property
    def input_limit(self):
        value = self.capacity - self.output - self.margin
        if self.output < 1 or self.margin < 0 or value < 1:
            raise ValueError("invalid context budget")
        return value


class LlamaRequestCounter:
    """Use the loaded model's own chat template and tokenizer, not char estimates.

    Text/tool requests only. Counts must be calibrated against actual provider
    usage for each selected model before claiming an exact final budget.
    """

    def __init__(self, client: httpx.AsyncClient, model: str):
        if urlparse(str(client.base_url)).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("local token counter requires a loopback model service")
        self.client, self.model = client, model

    async def __call__(self, request: dict) -> int:
        if request.get("model") != self.model:
            raise ValueError("request model does not match token counter")
        models = await self.client.get("/v1/models")
        models.raise_for_status()
        if self.model not in {m["id"] for m in models.json()["data"]}:
            raise ValueError("loaded model does not match token counter")
        body = {
            k: deepcopy(request[k])
            for k in ("model", "messages", "tools", "tool_choice", "chat_template_kwargs")
            if k in request
        }
        for msg in body["messages"]:
            if not isinstance(msg.get("content"), (str, type(None))):
                raise ValueError("multimodal input needs a modality-aware token counter")
        response = await self.client.post("/apply-template", json=body)
        response.raise_for_status()
        tokens = await self.client.post(
            "/tokenize",
            json={
                "content": response.json()["prompt"],
                "add_special": False,
                "parse_special": True,
            },
        )
        tokens.raise_for_status()
        return len(tokens.json()["tokens"])


async def fit_request(
    request: dict,
    counter: Callable[[dict], Awaitable[int]],
    budget: ContextBudget,
    *,
    protected_from: int,
    allow_drop_history: bool = True,
    target_tokens: int | None = None,
) -> tuple[dict, dict]:
    """Remove only whole older user turns; keep the current turn and fresh facts.

    protected_from is captured BEFORE fresh facts are appended. Using the final
    user-role message instead would allow dropping the actual current user goal.
    Removed messages remain in the caller's persistent history; this is a view.
    """
    # Streaming callbacks may contain live client/lock state. Only copy model
    # messages; fitting never mutates the other request fields.
    result = dict(request)
    result["messages"] = deepcopy(request["messages"])
    original = result["messages"]
    if not 0 <= protected_from < len(original):
        raise ValueError("protected turn boundary is outside the message list")
    if allow_drop_history and original[protected_from]["role"] != "user":
        raise ValueError("protected boundary must start at a user message")
    limit = budget.input_limit
    desired = limit if target_tokens is None else target_tokens
    if desired < 1 or desired > limit:
        raise ValueError("history target must fit inside the real input budget")
    measured = await counter(result)
    before = measured
    removed = []
    # Conversation turns start with a user message. System/developer policies
    # never become removable, even if they occur inside an older turn.
    starts = [
        i
        for i, msg in enumerate(original[:protected_from])
        if msg["role"] == "user"
        and not (
            isinstance(msg.get("content"), str) and msg["content"].startswith(FACT_SNAPSHOT_PREFIX)
        )
    ]
    spans = list(zip(starts, starts[1:] + [protected_from]))
    for start, end in spans:
        if measured <= desired or not allow_drop_history:
            break
        if any(msg["role"] in {"system", "developer"} for msg in original[start:end]):
            continue
        removed.extend(range(start, end))
        dropped = set(removed)
        result["messages"] = [m for i, m in enumerate(original) if i not in dropped]
        measured = await counter(result)
    if measured > limit:
        raise ContextBudgetExceeded(measured, limit)
    return result, {
        "tokens_before": before,
        "tokens_after": measured,
        "input_limit": limit,
        "target_tokens": desired,
        "target_met": measured <= desired,
        "output_reserved": budget.output,
        "margin": budget.margin,
        "removed_message_indices": removed,
        "persistent_history_modified": False,
    }
