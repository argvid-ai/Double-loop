"""Provider-specific configuration without coupling the harness to a tokenizer server."""

from __future__ import annotations

import os
from pathlib import Path

from .contracts import canonical


def model_access(profile, side):
    settings = getattr(profile, side)
    local = "llamacpp-qwen" in settings.plugins
    endpoint = settings.model_base_url or profile.model_base_url
    key = os.environ.get(f"CIX_{side.upper()}_MODEL_API_KEY")
    if not key and settings.model_key_file:
        key = Path(settings.model_key_file).expanduser().read_text().strip()
    if not key:
        key = (
            "local-no-key"
            if local
            else (
                os.environ.get("CLOUD_MODEL_API_KEY")
                or Path(profile.model_key_file).expanduser().read_text().strip()
            )
        )
    if not key:
        raise ValueError("model credentials unavailable")
    return endpoint, key, local


class RemoteRequestEstimate:
    """Conservative byte-based admission estimate, NOT a provider token measurement.

    Includes tool schemas and all message fields. Provider usage remains the
    measured source after a call; unexposed chat-template overhead is unknown.
    No request text or credentials are sent to a third-party tokenizer.
    """

    measurement = "utf8_byte_estimate_not_provider_tokens"

    async def __call__(self, request):
        body = {k: request[k] for k in ("messages", "tools", "tool_choice") if k in request}
        return len(canonical(body).encode("utf-8")) + 256
