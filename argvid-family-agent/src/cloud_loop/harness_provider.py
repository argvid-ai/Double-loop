"""Record actual provider calls without manufacturing task-fact messages."""

import time
import uuid
from copy import deepcopy


def native_estimate(messages, tools=None):
    from nanobot.utils.helpers import _estimate_prompt_tokens_with_source

    # Same SDK estimator for native consolidation and final admission. DeepSeek
    # usage is still the measured authority, not this cl100k estimate. Reserve
    # 25% plus template overhead until provider-specific counting is available.
    count, source = _estimate_prompt_tokens_with_source(messages, tools)
    return int(count * 1.25) + 512, "native_" + source + "_with_25pct_margin_not_provider_tokens"


class NativeRequestEstimate:
    measurement = "native_tiktoken_or_byte_fallback_with_25pct_margin_not_provider_tokens"

    async def __call__(self, request):
        count, self.measurement = native_estimate(request["messages"], request.get("tools"))
        return count


class RecordedProvider:
    supports_progress_deltas = False

    def __init__(self, delegate, audit, request_guard):
        self.delegate, self.audit, self.request_guard = delegate, audit, request_guard

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def estimate_prompt_tokens(self, messages, tools=None, model=None):
        return native_estimate(messages, tools)

    async def chat_with_retry(self, **kwargs):
        return await self._chat("chat_with_retry", kwargs)

    async def chat_stream_with_retry(self, **kwargs):
        return await self._chat("chat_stream_with_retry", kwargs)

    async def _chat(self, method, kwargs):
        request = dict(kwargs)
        request["messages"] = deepcopy(kwargs["messages"])
        boundary = next(
            (
                i
                for i in range(len(request["messages"]) - 1, -1, -1)
                if request["messages"][i]["role"] == "user"
            ),
            0,
        )
        request = await self.request_guard(request, boundary)
        request_id = uuid.uuid4().hex
        self.audit(
            "model.request",
            {
                "request_id": request_id,
                "model": kwargs.get("model"),
                "capture_boundary": "nanobot_provider_call_not_http_wire",
                "streaming": method == "chat_stream_with_retry",
                "facts_delivery": "none_on_demand_tools",
                "messages": [
                    {
                        k: deepcopy(v)
                        for k, v in msg.items()
                        if k not in {"reasoning_content", "reasoning", "thinking"}
                    }
                    for msg in request["messages"]
                ],
                "excluded_message_fields": ["reasoning_content", "reasoning", "thinking"],
                "tool_definitions": deepcopy(request.get("tools", [])),
                "tool_choice": request.get("tool_choice"),
                "tools": [t["function"]["name"] for t in request.get("tools") or []],
            },
        )
        started = time.monotonic()
        first_delta = last_delta = None
        content_chars = 0
        retry_notifications = 0
        stream_recoveries = 0
        original_delta = request.get("on_content_delta")
        original_retry = request.get("on_retry_wait")
        original_recover = request.get("on_stream_recover")

        async def delta(text):
            nonlocal first_delta, last_delta, content_chars
            if text:
                elapsed = time.monotonic() - started
                if first_delta is None:
                    first_delta = elapsed
                last_delta = elapsed
                content_chars += len(text)
            if original_delta:
                await original_delta(text)

        async def retry(message):
            nonlocal retry_notifications
            retry_notifications += 1
            # Do not persist provider error strings, which can contain URLs or headers.
            self.audit(
                "model.retry_wait",
                {
                    "request_id": request_id,
                    "notification": retry_notifications,
                    "elapsed_seconds": time.monotonic() - started,
                },
            )
            if original_retry:
                await original_retry("模型服务暂时不可用，正在进行有限重试。")

        async def recover():
            nonlocal first_delta, last_delta, content_chars, stream_recoveries
            stream_recoveries += 1
            self.audit(
                "model.stream_recovered",
                {"request_id": request_id, "elapsed_seconds": time.monotonic() - started},
            )
            first_delta = last_delta = None
            content_chars = 0
            if original_recover:
                await original_recover()

        if original_delta:
            request["on_content_delta"] = delta
        request["on_retry_wait"] = retry
        if original_recover:
            request["on_stream_recover"] = recover
        try:
            result = await getattr(self.delegate, method)(**request)
        except BaseException as exc:
            self.audit(
                "model.failed",
                {
                    "request_id": request_id,
                    "elapsed_seconds": time.monotonic() - started,
                    "error_type": type(exc).__name__,
                    "sdk_retry_notifications": retry_notifications,
                },
            )
            raise
        self.audit(
            "model.response",
            {
                "request_id": request_id,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "content": "[provider error omitted]"
                if getattr(result, "finish_reason", None) == "error"
                else getattr(result, "content", None),
                "usage": getattr(result, "usage", None),
                "first_content_delta_seconds": first_delta,
                "last_content_delta_seconds": last_delta,
                "content_delta_chars": content_chars,
                "sdk_retry_notifications": retry_notifications,
                "stream_recoveries": stream_recoveries,
                "timing_boundary": "SDK provider roundtrip including service, network and SDK retries; not pure decode time",
                "finish_reason": getattr(result, "finish_reason", None),
                "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in (getattr(result, "tool_calls", None) or [])
                ],
            },
        )
        return result
