"""Trusted adapter factories. Configuration selects IDs, never Python imports.

Adding a platform implementation means registering a factory here (or in
trusted bootstrap code), not adding board checks to the observation loop.
"""

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Adapter:
    kind: str
    at: str
    factory: Callable | None
    contract: str
    family: str | None = None
    recover: Callable | None = None


ADAPTERS: dict[str, Adapter] = {}


def register(name, adapter):
    if name in ADAPTERS:
        raise ValueError("duplicate installed adapter")
    ADAPTERS[name] = adapter


def resolve(binding, kind):
    adapter = ADAPTERS.get(binding.adapter)
    if adapter is None or adapter.kind != kind:
        raise ValueError(f"adapter not installed for {kind}: {binding.adapter}")
    if binding.at != adapter.at:
        raise ValueError(f"adapter placement unavailable: {binding.adapter} at {binding.at}")
    return adapter


def video_reader(*args, **kwargs):
    from .observation_video_cursor import ContinuousVideoReader

    return ContinuousVideoReader(*args, **kwargs)


def frame_preparer(*args, **kwargs):
    from .observation_media import FileReplayWindow

    return FileReplayWindow(*args, **kwargs)


def native_scorer(model, process, payloads, principal, task_id, audit, guard):
    from .native_visual_provider import NativeVisualScorer

    return NativeVisualScorer(
        model, process, payloads, principal, task_id, audit, guard, client=process.client
    )


def qwen_scorer(model, process, payloads, principal, task_id, audit, guard):
    from .qwen_observation_provider import QwenOptionScorer

    return QwenOptionScorer(
        process.endpoint,
        payloads,
        principal,
        task_id,
        model.model_alias,
        audit,
        client=process.client,
        asset_guard=guard,
        request_runner=process.request,
    )


def local_connection(model, root):
    from .model_service_connection import ModelServiceConnection

    return ModelServiceConnection(model, root)


async def recover_input(registry, cfg, run):
    return await registry._recover_source(cfg, run)


register(
    "file-replay.v1",
    Adapter(
        "input",
        "edge.harness.cpu",
        lambda registry, cfg: registry._replay_source(cfg),
        "replay-media-source/1",
        recover=recover_input,
    ),
)
register(
    "registered-input.v1",
    Adapter(
        "input",
        "edge.harness.cpu",
        lambda registry, cfg: registry._managed_source(cfg),
        "managed-media-source/1",
        recover=recover_input,
    ),
)
register(
    "pyav-continuous.v1",
    Adapter("video_reader", "edge.harness.cpu", video_reader, "timestamped-decoded-frames/1"),
)
register(
    "sampled-media.v1",
    Adapter("frame_preparer", "edge.harness.cpu", frame_preparer, "cix.observation-media/1"),
)
register(
    "loopback-http.v1",
    Adapter("transport", "edge.harness.cpu", local_connection, "model-service-lifecycle/1"),
)
register(
    "http-model-service.v1",
    Adapter("transport", "edge.harness.cpu", local_connection, "model-service-lifecycle/1"),
)
register(
    "clip-native.v1",
    Adapter("request", "edge.harness.cpu", native_scorer, "harness.native-visual/1", "clip"),
)
register(
    "blip-vqa.v1",
    Adapter("request", "edge.harness.cpu", native_scorer, "harness.native-visual/1", "blip_vqa"),
)
register(
    "qwen-options.v1",
    Adapter("request", "edge.harness.cpu", qwen_scorer, "qwen-option-scores/1", "qwen"),
)
register(
    "external-cpu-service.v1",
    Adapter("compute", "edge.model_service.cpu", None, "externally-deployed-service/1"),
)
register(
    "remote-model-service.v1",
    Adapter("compute", "remote.model_service.external", None, "externally-deployed-service/1"),
)
