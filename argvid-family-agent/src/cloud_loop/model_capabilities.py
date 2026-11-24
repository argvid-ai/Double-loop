"""Resolve installed route metadata without a network probe on every turn."""


def model_accepts_images(settings):
    return "image" in resolve_model_capabilities(settings)["input_modalities"]


def resolve_model_capabilities(settings, fallback=False):
    route = settings.model_fallback if fallback else settings
    capabilities = route.capabilities if fallback else settings.model_capabilities
    output_tokens = (
        (route.output_tokens or settings.output_tokens) if fallback else settings.output_tokens
    )
    if capabilities:
        value = capabilities.model_dump(mode="json")
        if value.get("model_id") is not None and value["model_id"] != route.model:
            raise ValueError("model capability metadata belongs to a different route model")
    else:
        value = {
            "context_window": settings.context_tokens,
            "max_output_tokens": output_tokens,
            "input_modalities": ["text", "image"] if settings.model_image_input else ["text"],
            "reasoning": False,
            "source": "legacy_profile_unverified",
        }
    if output_tokens > value["max_output_tokens"] or output_tokens >= value["context_window"]:
        raise ValueError("request output budget exceeds installed model capabilities")
    return {**value, "model": route.model, "output_tokens": output_tokens}
