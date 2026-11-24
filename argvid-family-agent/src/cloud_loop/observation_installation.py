"""Typed, file-backed assembly of the existing observation components.

Loading verifies registered bytes and budgets, never starts a model/task. File
references are operator configuration; model tools only receive registered IDs.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

import av
from pydantic import Field, field_validator, model_validator

from .contracts import Contract, canonical, digest
from .harness_input_store import InputMediaStore, InputSourceSpec
from .harness_observation import ObservationInstallation
from .managed_observation_source import ManagedMediaPreparer, RegisteredInputDriver
from .media_semantic_provider import MediaProviderSpec
from .observation_archive import ObservationArchive
from .observation_cache import AnalysisBudget, BudgetedReplaySource
from .observation_cleanup import ObservationCacheCleanup
from .observation_contracts import ObservationConfig, Reference
from .observation_decisions import ObservationDecisions, RetentionPolicy
from .observation_gateway import GatewayPolicy, ObservationGateway
from .observation_inputs import MediaInputSource
from .observation_media import SamplingPolicy, _fingerprint
from .observation_process import NativeModelProcess
from .observation_retention import ObservationRetentionWorker
from .observation_runtime import ManagedObservationRunner, ObservationRuntimeSettings
from .observation_semantics import StartObservation
from .observation_store import ObservationStore
from .observation_tools import MockObservationGimbal, ObservationTools
from .qwen_observation_provider import QwenModelProcess
from .semantic_backends import ExternalSemanticBackend, QwenAudioBackend
from .semantic_observation_runtime import SemanticObservationRunner
from .store import Conflict
from .native_visual_provider import NativeVisualBackend
from .model_service_connection import ModelServicePreset, ModelServiceBackend
from .node_assembly import AssemblyPlan, AssembledModelBackend, load_installation
from .node_adapters import resolve as resolve_adapter


def is_native_visual(model):
    return isinstance(model, NativeVisualPreset) or (
        isinstance(model, ModelServicePreset) and model.family != "qwen"
    )


class PinnedFile(Contract):
    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(strict=True, gt=0)

    @field_validator("path")
    @classmethod
    def absolute(cls, value):
        if not Path(value).is_absolute():
            raise ValueError("registered file path must be absolute")
        return value


class ReplayAsset(Contract):
    title: str = Field(min_length=1, max_length=100)
    device_ref: Reference
    file: PinnedFile
    start_ms: int = Field(default=0, strict=True, ge=0)
    end_ms: int | None = Field(default=None, strict=True, gt=0)
    repeat: bool = Field(default=True, strict=True)

    @model_validator(mode="after")
    def interval(self):
        if self.end_ms is not None and self.end_ms <= self.start_ms:
            raise ValueError("replay interval must be positive")
        return self


class ManagedSourceSpec(Contract):
    source_kind: Literal["managed_input"]
    input: InputSourceSpec

    @property
    def title(self):
        return self.input.title

    @property
    def device_ref(self):
        return self.input.binding.device_ref


class NativePreset(Contract):
    backend: Literal["minicpm_omni"] = "minicpm_omni"
    title: str = Field(min_length=1, max_length=100)
    binary: PinnedFile
    runtime_libraries: tuple[PinnedFile, ...]
    llm: PinnedFile
    vision: PinnedFile
    audio: PinnedFile
    port: int = Field(strict=True, ge=1024, le=65535)
    context_tokens: int = Field(default=8192, strict=True, ge=2048, le=131072)
    output_tokens: int = Field(default=96, strict=True, ge=16, le=8192)
    threads: int = Field(default=8, strict=True, ge=1, le=64)
    gpu_layers: int = Field(default=0, strict=True, ge=0, le=999)

    @model_validator(mode="after")
    def native_layout(self):
        root = Path(self.llm.path).parent
        if Path(self.audio.path) != root / "audio/MiniCPM-o-4_5-audio-F16.gguf":
            raise ValueError("audio path differs from native model layout")
        if Path(self.vision.path) != root / "vision/MiniCPM-o-4_5-vision-F16.gguf":
            raise ValueError("vision path differs from native model layout")
        if self.output_tokens + 1024 >= self.context_tokens:
            raise ValueError("output leaves no observation context budget")
        return self

    def command(self):
        return [
            self.binary.path,
            "-m",
            self.llm.path,
            "-ngl",
            str(self.gpu_layers),
            "-c",
            str(self.context_tokens),
            "-t",
            str(self.threads),
            "-n",
            str(self.output_tokens),
            "--no-warmup",
        ]


class QwenPreset(Contract):
    backend: Literal["qwen_cpu_vision"]
    title: str = Field(min_length=1, max_length=100)
    binary: PinnedFile
    runtime_libraries: tuple[PinnedFile, ...] = ()
    llm: PinnedFile
    vision: PinnedFile
    port: int = Field(strict=True, ge=1024, le=65535)
    context_tokens: int = Field(default=8192, strict=True, ge=2048, le=32768)
    threads: int = Field(default=8, strict=True, ge=1, le=64)
    gpu_layers: Literal[0] = 0
    output_tokens: Literal[1] = 1
    audio_provider: MediaProviderSpec | None = None
    fusion_policy: Literal["consensus", "any_relevant", "all_relevant"] = "consensus"

    @model_validator(mode="after")
    def pinned_visual_layout(self):
        if (
            Path(self.llm.path).name != "Qwen3.5-0.8B-Q4_K_M.gguf"
            or Path(self.vision.path) != Path(self.llm.path).parent / "mmproj-F16.gguf"
        ):
            raise ValueError("this CPU preset requires the prepared Qwen0.8B visual assets")
        return self

    def command(self):
        return [
            self.binary.path,
            "-m",
            self.llm.path,
            "--mmproj",
            self.vision.path,
            "--no-mmproj-offload",
            "-ngl",
            "0",
            "-c",
            str(self.context_tokens),
            "-t",
            str(self.threads),
            "-ub",
            "128",
            "-np",
            "1",
            "--jinja",
            "-n",
            "1",
        ]


class NativeVisualPreset(Contract):
    backend: Literal["native_visual_cpu"]
    title: str = Field(min_length=1, max_length=100)
    family: Literal["clip", "blip_vqa"]
    revision: str = Field(pattern=r"^[a-f0-9]{40}$")
    binary: PinnedFile
    model_dir: str
    files: tuple[PinnedFile, ...] = Field(min_length=3, max_length=32)
    port: int = Field(strict=True, ge=1024, le=65535)
    threads: int = Field(default=4, strict=True, ge=1, le=64)
    context_tokens: Literal[8192] = 8192
    output_tokens: Literal[12] = 12
    gpu_layers: Literal[0] = 0

    @model_validator(mode="after")
    def assets_layout(self):
        root = Path(self.model_dir)
        if not root.is_absolute() or any(Path(p.path).parent != root for p in self.files):
            raise ValueError("native model files must belong to one absolute installed directory")
        names = {Path(p.path).name for p in self.files}
        if not {"config.json", "preprocessor_config.json"} <= names or not (
            {"model.safetensors", "pytorch_model.bin"} & names
        ):
            raise ValueError("native model configuration, processor and weights must be pinned")
        return self

    @property
    def result_type(self):
        return "cosine_similarities" if self.family == "clip" else "visual_answer"

    def command(self):
        return [
            self.binary.path,
            "-m",
            "cloud_loop.native_visual_server",
            "--model-dir",
            self.model_dir,
            "--family",
            self.family,
            "--revision",
            self.revision,
            "--threads",
            str(self.threads),
        ]


class ExternalSemanticPreset(Contract):
    backend: Literal["semantic_media_http"]
    title: str = Field(min_length=1, max_length=100)
    provider: MediaProviderSpec
    context_tokens: int = Field(default=8192, strict=True, ge=2048, le=32768)
    output_tokens: int = Field(default=512, strict=True, ge=32, le=4096)
    gpu_layers: Literal[0] = 0

    @model_validator(mode="after")
    def semantic_contract(self):
        if "semantic_choices" not in self.provider.capabilities:
            raise ValueError("semantic executor requires semantic_choices")
        return self


class ExecutionPreset(Contract):
    title: str = Field(min_length=1, max_length=100)
    default_duration_seconds: int | None = Field(default=None, strict=True, ge=1, le=86400)
    source_ref: Reference
    model_preset_ref: Reference
    storage_binding_ref: Reference
    standard_batch_ref: Reference
    detailed_batch_ref: Reference | None = None
    sampling_profiles: dict[Reference, Reference] = Field(default_factory=dict, max_length=16)
    save_policy_ref: Reference
    tools: tuple[Reference, ...] = ("mark_media", "read_media", "get_edge_state")
    end_policy: Literal["stop_after_replay", "pause"] = "stop_after_replay"
    output_mode: Literal["retained_media", "markers"] = "retained_media"
    context_mode: Literal["rolling_history", "current_frame"] = "rolling_history"

    @model_validator(mode="after")
    def supported_capabilities(self):
        if (
            set(self.tools) - {"mark_media", "read_media", "get_edge_state"}
            or "mark_media" not in self.tools
        ):
            raise ValueError(
                "finite-state recorder supports only installed observation capabilities"
            )
        reserved = {"standard": self.standard_batch_ref, "detailed": self.detailed_batch_ref}
        if any(
            name in reserved and ref != reserved[name]
            for name, ref in self.sampling_profiles.items()
        ):
            raise ValueError("sampling profile conflicts with its legacy binding")
        return self

    def sampling_bindings(self):
        values = {"standard": self.standard_batch_ref}
        if self.detailed_batch_ref:
            values["detailed"] = self.detailed_batch_ref
        return {**values, **self.sampling_profiles}


class BatchPolicy(Contract):
    # None selects the staged executor whenever the installed contracts support it.
    # Zero is retained only for explicit serial comparison/legacy configurations.
    pipeline_capacity: int | None = Field(default=None, strict=True, ge=0, le=4)
    pipeline_trace: bool = Field(default=False, strict=True)
    validate_full_window: bool = True
    prefetch_batches: int = Field(default=0, strict=True, ge=0, le=1)
    window_ms: int = Field(default=5000, strict=True, ge=100, le=60000)
    frame_interval_ms: int = Field(default=500, strict=True, ge=100, le=10000)
    image_long_edge: int = Field(default=448, strict=True, ge=32, le=1024)
    image_region: tuple[float, float, float, float] | None = None

    @model_validator(mode="after")
    def sampling_schedule(self):
        if self.pipeline_capacity and self.prefetch_batches:
            raise ValueError("staged preparation replaces replay prefetch")
        SamplingPolicy(self.frame_interval_ms, self.image_long_edge, image_region=self.image_region)
        if self.window_ms < self.frame_interval_ms or self.window_ms % self.frame_interval_ms:
            raise ValueError("batch duration must be a positive multiple of the frame interval")
        return self


class SavePolicy(Contract):
    pre_roll_ms: int = Field(default=5000, strict=True, ge=0)
    post_roll_ms: int = Field(default=5000, strict=True, ge=0)
    discard_hold_ms: int = Field(default=300000, strict=True, ge=0)


class StoragePolicy(Contract):
    project_id: Reference
    quota_bytes: int = Field(strict=True, gt=0)
    min_free_bytes: int = Field(strict=True, ge=0)


class MockGimbalSpec(Contract):
    pan_limit: float = Field(default=90, gt=0, allow_inf_nan=False)
    tilt_limit: float = Field(default=45, gt=0, allow_inf_nan=False)
    max_step: float = Field(default=15, gt=0, allow_inf_nan=False)
    max_age_seconds: float = Field(default=5, gt=0, allow_inf_nan=False)


class SkillDirectory(Contract):
    path: str
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class InstallationConfig(Contract):
    node_assembly: AssemblyPlan | None = None
    schema_version: str = Field(
        default="observation-installation/1", pattern=r"^observation-installation/1$"
    )
    allow_candidate_runs: bool = Field(default=False, strict=True)
    sources: dict[Reference, ReplayAsset | ManagedSourceSpec] = Field(min_length=1, max_length=32)
    models: dict[
        Reference,
        NativePreset
        | QwenPreset
        | ExternalSemanticPreset
        | NativeVisualPreset
        | ModelServicePreset,
    ] = Field(min_length=1, max_length=8)
    batches: dict[Reference, BatchPolicy] = Field(min_length=1, max_length=16)
    saves: dict[Reference, SavePolicy] = Field(min_length=1, max_length=16)
    storage: dict[Reference, StoragePolicy] = Field(min_length=1, max_length=32)
    cache_policy_ref: Reference
    analysis_quota_bytes: int = Field(strict=True, gt=0)
    native_visual_memory_images: bool = False
    max_output_bytes: int = Field(strict=True, gt=0)
    gimbals: dict[Reference, MockGimbalSpec] = Field(default_factory=dict, max_length=16)
    skills: dict[Reference, SkillDirectory] = Field(default_factory=dict, max_length=32)
    executors: dict[Reference, ExecutionPreset] = Field(default_factory=dict, max_length=16)
    gateway_policy: GatewayPolicy = Field(default_factory=GatewayPolicy)

    @model_validator(mode="after")
    def unique_ports_and_budgets(self):
        if self.node_assembly:
            plan = self.node_assembly
            if set(plan.models) != set(self.models) or set(plan.inputs) != set(self.sources):
                raise ValueError("assembly bindings differ from installed components")
            for key, binding in plan.models.items():
                if binding.service != self.models[key]:
                    raise ValueError("assembly model specification differs from installation")
            for key, binding in plan.inputs.items():
                expected = (
                    "managed-media-source/1"
                    if isinstance(self.sources[key], ManagedSourceSpec)
                    else "replay-media-source/1"
                )
                if resolve_adapter(binding, "input").contract != expected:
                    raise ValueError("input adapter contract differs from source kind")
        ports = [
            model.port
            for model in self.models.values()
            if not isinstance(model, (ExternalSemanticPreset, ModelServicePreset))
        ]
        if len(ports) != len(set(ports)):
            raise ValueError("native presets must not share a port")
        if any(self.max_output_bytes > binding.quota_bytes for binding in self.storage.values()):
            raise ValueError("one output reservation exceeds project storage quota")
        if len({value.title for value in self.executors.values()}) != len(self.executors):
            raise ValueError("executor titles must be distinct")
        for executor in self.executors.values():
            for key, values in (
                (executor.source_ref, self.sources),
                (executor.model_preset_ref, self.models),
                (executor.storage_binding_ref, self.storage),
                (executor.standard_batch_ref, self.batches),
                (executor.save_policy_ref, self.saves),
            ):
                if key not in values:
                    raise ValueError("executor references an uninstalled component")
            if any(ref not in self.batches for ref in executor.sampling_bindings().values()):
                raise ValueError("executor sampling profile is not installed")
            if not isinstance(
                self.models[executor.model_preset_ref],
                (QwenPreset, ExternalSemanticPreset, NativeVisualPreset, ModelServicePreset),
            ):
                raise ValueError("named finite-state executor requires a semantic Provider")
            if (
                isinstance(self.sources[executor.source_ref], ManagedSourceSpec)
                and executor.output_mode != "markers"
            ):
                raise ValueError(
                    "managed capture uses explicit later export, not automatic replay retention"
                )
        for key, source in self.sources.items():
            if isinstance(source, ManagedSourceSpec) and source.input.binding.source_ref != key:
                raise ValueError("managed source identity differs from installation key")
        return self


class ObservationRegistry:
    def __init__(self, host, path):
        if host.side != "edge":
            raise ValueError("observation installation belongs on edge")
        self.host, self.store = host, host.store
        self.path = Path(path).resolve(strict=True)
        if self.path.stat().st_size > 1024**2:
            raise ValueError("observation configuration exceeds size bound")
        try:
            value, self.configuration_files = load_installation(self.path)
            self.config = InstallationConfig.model_validate(value)
            if self.config.node_assembly:
                self.config.node_assembly.check_host()
        except ValueError:
            raise ValueError("invalid observation installation configuration") from None
        self.digest = digest(self.config)
        self.config_fingerprint = _fingerprint(self.path)
        self.assets = {}
        self.asset_targets = {}
        self.skill_pins = {}
        self.analysis = self.store.root / "observation-analysis"
        self.projects = self.store.root / "observation-projects"
        self.processes = self.store.root / "observation-models"
        self._check_roots()
        self.ledger = ObservationStore(self.store)
        self.input_media = getattr(host, "input_media", None)
        if self.input_media is None:
            self.input_media = InputMediaStore(self.store)
        old = self.store.meta("observation_installation")
        same_legacy_defaults = bool(
            old
            and canonical(InstallationConfig.model_validate(old["configuration"]))
            == canonical(self.config)
        )
        if old and old["digest"] != self.digest and not same_legacy_defaults:
            with self.store.connect() as db:
                unfinished = db.execute(
                    "SELECT 1 FROM observation_tasks WHERE status!='stopped' LIMIT 1"
                ).fetchone()
                exports = db.execute(
                    "SELECT name FROM sqlite_master WHERE name='observation_retention_events'"
                ).fetchone()
                waiting = (
                    exports
                    and db.execute(
                        "SELECT 1 FROM observation_retention_events WHERE status IN ('open','exporting') LIMIT 1"
                    ).fetchone()
                )
            if unfinished or waiting:
                raise Conflict("unfinished observation work pins installation configuration")
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS observation_verified_files (
                path TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,sha256 TEXT NOT NULL,bytes INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS observation_installation_snapshots (
                digest TEXT PRIMARY KEY,configuration TEXT NOT NULL,created REAL NOT NULL)""")
        for source in self.config.sources.values():
            if isinstance(source, ManagedSourceSpec):
                self.input_media.register("local-user", source.input)
                continue
            path = self._verify_file(source.file)
            with av.open(str(path)) as media:
                if not media.streams.video:
                    raise Conflict("replay requires a real video track")
                if (
                    any(isinstance(model, NativePreset) for model in self.config.models.values())
                    and not media.streams.audio
                ):
                    raise Conflict("audiovisual presets require a real audio track")
                if source.start_ms or source.end_ms is not None:
                    stream = media.streams.video[0]
                    if stream.duration is None:
                        raise Conflict("bounded replay requires a declared video duration")
                    end = int(
                        ((stream.start_time or 0) + stream.duration) * stream.time_base * 1000
                    )
                    if source.start_ms >= end or (
                        source.end_ms is not None and source.end_ms > end
                    ):
                        raise Conflict("replay interval exceeds registered video")
        for model in self.config.models.values():
            if isinstance(model, (ExternalSemanticPreset, ModelServicePreset)):
                continue
            if isinstance(model, NativeVisualPreset):
                for asset in (model.binary, *model.files):
                    self._verify_file(asset)
                if not os.access(model.binary.path, os.X_OK):
                    raise Conflict("registered visual worker Python is not executable")
                # Transformers must not discover unregistered configuration/weight files.
                actual = {p.name for p in Path(model.model_dir).iterdir() if p.is_file()}
                if actual != {Path(p.path).name for p in model.files}:
                    raise Conflict("native model directory contains unregistered files")
                continue
            media_assets = (model.audio,) if isinstance(model, NativePreset) else ()
            for asset in (
                model.binary,
                model.llm,
                model.vision,
                *media_assets,
                *model.runtime_libraries,
            ):
                self._verify_file(asset)
            if not os.access(model.binary.path, os.X_OK):
                raise Conflict("registered native server is not executable")
        for name, skill in self.config.skills.items():
            self.skill_pins[name] = self._skill(skill)
        self.cache_budget = AnalysisBudget(
            self.store,
            self.analysis,
            self.config.analysis_quota_bytes,
            max(binding.min_free_bytes for binding in self.config.storage.values()),
        )
        self.decisions = ObservationDecisions(
            self.ledger,
            {
                key: RetentionPolicy(**value.model_dump())
                for key, value in self.config.saves.items()
            },
        )
        self.gimbal = MockObservationGimbal(self.ledger)
        for name, spec in self.config.gimbals.items():
            self.gimbal.register("local-user", name, **spec.model_dump())
        self.archive = ObservationArchive(self.ledger, self.projects, self.source_path)
        for name, spec in self.config.storage.items():
            self.archive.bind(
                name,
                "local-user",
                spec.project_id,
                quota_bytes=spec.quota_bytes,
                min_free_bytes=spec.min_free_bytes,
            )
        self.sources = {}
        self.gateway = None
        self.retention = None
        self.recovering_processes = {}

    def _verify_file(self, asset):
        path = Path(asset.path).resolve(strict=True)
        if not path.is_file() or path.stat().st_size != asset.bytes:
            raise Conflict("registered asset size differs")
        if any(
            path.is_relative_to(root.resolve())
            for root in (self.analysis, self.projects, self.processes)
        ):
            raise Conflict("registered originals must be outside writable observation directories")
        before = _fingerprint(path)
        with self.store.connect() as db:
            cached = db.execute(
                "SELECT * FROM observation_verified_files WHERE path=?", (str(path),)
            ).fetchone()
        if not cached or (cached["fingerprint"], cached["sha256"], cached["bytes"]) != (
            canonical(before),
            asset.sha256,
            asset.bytes,
        ):
            hasher = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024**2), b""):
                    hasher.update(chunk)
            if hasher.hexdigest() != asset.sha256 or _fingerprint(path) != before:
                raise Conflict("registered asset hash differs or changed during verification")
            with self.store.connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO observation_verified_files VALUES (?,?,?,?)",
                    (str(path), canonical(before), asset.sha256, asset.bytes),
                )
        self.assets[str(path)] = before
        self.asset_targets[asset.path] = str(path)
        return path

    def _skill(self, skill):
        path = Path(skill.path)
        if not path.is_absolute() or not (path / "SKILL.md").is_file():
            raise Conflict("registered community Skill is missing")
        files = {}
        fingerprints = {}
        total = 0
        for file in path.rglob("*"):
            if file.is_symlink():
                raise Conflict("Skill must not contain symlinks")
            if file.is_file():
                before = _fingerprint(file)
                size = file.stat().st_size
                total += size
                if size > 2 * 1024**2 or total > 8 * 1024**2 or len(files) >= 128:
                    raise Conflict("Skill files exceed installation bounds")
                files[str(file.relative_to(path))] = hashlib.sha256(file.read_bytes()).hexdigest()
                if _fingerprint(file) != before:
                    raise Conflict("Skill changed while verifying")
                fingerprints[str(file.resolve())] = before
        if digest(files) != skill.manifest_sha256:
            raise Conflict("Skill manifest differs from configured version")
        return fingerprints

    def _unchanged(self):
        for model in self.config.models.values():
            if isinstance(model, NativeVisualPreset) and {
                p.name for p in Path(model.model_dir).iterdir() if p.is_file()
            } != {Path(p.path).name for p in model.files}:
                raise Conflict("native model directory changed")
        self._check_roots()
        if digest(self.config) != self.digest:
            raise Conflict("loaded observation configuration was mutated")
        if _fingerprint(self.path) != self.config_fingerprint:
            raise Conflict("installation file changed; explicit service reload required")
        for path, fingerprint in self.configuration_files.items():
            if _fingerprint(Path(path)) != fingerprint:
                raise Conflict("assembly reference changed; explicit service reload required")
        for original, target in self.asset_targets.items():
            if str(Path(original).resolve(strict=True)) != target:
                raise Conflict("registered asset target changed")
        for path, expected in self.assets.items():
            if _fingerprint(Path(path)) != expected:
                raise Conflict("registered asset changed; verify and reconfigure it")

    def _check_roots(self):
        for path in (self.analysis, self.projects, self.processes):
            if not path.resolve().is_relative_to(self.store.root.resolve()):
                raise Conflict("observation writable directory escaped node state")

    def validate(self, cfg):
        self._unchanged()
        c = self.config
        if cfg.principal != "local-user":
            raise Conflict("observation principal differs from installation")
        for key, collection in (
            (cfg.source_ref, c.sources),
            (cfg.model_preset_ref, c.models),
            (cfg.batch_policy_ref, c.batches),
            (cfg.save_policy_ref, c.saves),
            (cfg.storage_binding_ref, c.storage),
        ):
            if key not in collection:
                raise Conflict("observation references an uninstalled component")
        if cfg.device_ref != c.sources[cfg.source_ref].device_ref:
            raise Conflict("source/device binding differs")
        if isinstance(c.sources[cfg.source_ref], ManagedSourceSpec):
            required = set(self.model_modalities(c.models[cfg.model_preset_ref]))
            if required - set(c.sources[cfg.source_ref].input.binding.modalities):
                raise Conflict("installed input lacks the model required modalities")
        if cfg.project_id != c.storage[cfg.storage_binding_ref].project_id:
            raise Conflict("project/storage binding differs")
        if cfg.cache_policy_ref != c.cache_policy_ref:
            raise Conflict("cache policy is not installed")
        if cfg.ptz_scope_ref and cfg.ptz_scope_ref not in c.gimbals:
            raise Conflict("gimbal is not installed")
        if set(cfg.tools) - set(ObservationTools.names) or (
            cfg.tools and "mark_media" not in cfg.tools
        ):
            raise Conflict("unsupported observation tool selection")
        if set(cfg.skills) - set(c.skills):
            raise Conflict("observation Skill is not installed")
        if isinstance(
            c.models[cfg.model_preset_ref],
            (QwenPreset, ExternalSemanticPreset, NativeVisualPreset, ModelServicePreset),
        ):
            model = c.models[cfg.model_preset_ref]
            if is_native_visual(model):
                expected = "text_similarity" if model.family == "clip" else "visual_question"
                if (
                    cfg.visual_rule is None
                    or cfg.visual_rule.kind != expected
                    or cfg.decision is not None
                ):
                    raise Conflict(
                        "task input rule is incompatible with installed native model capability"
                    )
            elif cfg.decision is None or cfg.visual_rule is not None:
                raise Conflict("semantic model requires semantic alternatives, not a native rule")
            if not cfg.executor_ref or cfg.executor_ref not in c.executors:
                raise Conflict(
                    "CPU semantic task requires a registered named executor and cloud alternatives"
                )
            executor = c.executors[cfg.executor_ref]
            if (
                cfg.source_ref != executor.source_ref
                or cfg.model_preset_ref != executor.model_preset_ref
                or cfg.storage_binding_ref != executor.storage_binding_ref
                or cfg.save_policy_ref != executor.save_policy_ref
                or cfg.batch_policy_ref not in executor.sampling_bindings().values()
                or cfg.output_mode != executor.output_mode
                or tuple(cfg.tools) != executor.tools
                or cfg.ptz_scope_ref is not None
                or cfg.skills
            ):
                raise Conflict("task differs from its installed executor configuration")
        for key in cfg.skills:
            if self._skill(c.skills[key]) != self.skill_pins[key]:
                raise Conflict("observation Skill changed")

    def validate_start(self, cfg):
        self.validate(cfg)
        if not self.config.allow_candidate_runs:
            raise Conflict("model is not admitted; candidate execution is disabled")
        source = self.config.sources[cfg.source_ref]
        if cfg.capture_until is not None and not (
            isinstance(source, ManagedSourceSpec)
            and source.input.continuous_capture
            and source.input.incremental_video
            and source.input.capture is not None
            and source.input.capture.driver != "paired_stream"
        ):
            raise Conflict("capture drain requires owned incremental recording")
        if (
            isinstance(source, ManagedSourceSpec)
            and not source.input.binding.finite
            and self.gateway is not None
        ):
            with self.store.connect() as db:
                others = [
                    dict(row)
                    for row in db.execute(
                        """SELECT * FROM observation_tasks WHERE task_id!=?
                    AND generation>0 AND json_extract(config,'$.source_ref')=?""",
                        (cfg.task_id, cfg.source_ref),
                    )
                ]
            if any(not self.gateway._released(row, capture_only=cfg.output_mode == "markers")
                   for row in others):
                raise Conflict("live input source is occupied by another observation task")
        model = self.config.models[cfg.model_preset_ref]
        independent_visual = (
            isinstance(source, ManagedSourceSpec) and source.input.continuous_capture
            and cfg.output_mode == "markers" and isinstance(model, ModelServicePreset)
            and model.family in {"clip", "blip_vqa"}
        )
        if cfg.executor_ref and self.gateway is not None and not independent_visual:
            with self.store.connect() as db:
                others = [
                    dict(row)
                    for row in db.execute(
                        """SELECT * FROM observation_tasks
                    WHERE task_id!=? AND generation>0 AND json_extract(config,'$.model_preset_ref')=?""",
                        (cfg.task_id, cfg.model_preset_ref),
                    )
                ]
            if any(not self.gateway._released(row) for row in others):
                raise Conflict("named executor inference resource is occupied by another task")

    def source_path(self, principal, ref):
        if principal != "local-user" or ref not in self.config.sources:
            raise KeyError(ref)
        if isinstance(self.config.sources[ref], ManagedSourceSpec):
            raise Conflict("managed capture has multiple files; select a captured media reference")
        self._unchanged()
        return Path(self.config.sources[ref].file.path).resolve()

    def captured_path(self, batch):
        if isinstance(self.config.sources[batch["source_ref"]], ManagedSourceSpec):
            return self.input_media.resolve(batch["principal"], batch["media_ref"])["path"]
        return self.source_path(batch["principal"], batch["source_ref"])

    def _managed_source(self, cfg):
        model = self.config.models[cfg.model_preset_ref]
        policy = self.config.batches[cfg.batch_policy_ref]
        spec = self.config.sources[cfg.source_ref]
        prefetch = 0 if self.pipeline_selection(cfg)[0] else policy.prefetch_batches
        driver = RegisteredInputDriver(
            self.input_media,
            cfg.principal,
            cfg.source_ref,
            window_ms=policy.window_ms,
            pacing="processing" if spec.input.binding.finite else "unpaced",
            prefetch_batches=prefetch,
        )
        sampling = SamplingPolicy(
            policy.frame_interval_ms,
            policy.image_long_edge,
            16000,
            include_audio="audio" in self.model_modalities(model),
            include_image="image" in self.model_modalities(model),
            image_region=policy.image_region,
            validate_full_window=policy.validate_full_window,
        )
        preparer = ManagedMediaPreparer(
            self.input_media,
            cfg.principal,
            cfg.source_ref,
            self.analysis / cfg.task_id,
            sampling=sampling,
            window_ms=policy.window_ms,
            budget=self.cache_budget,
            memory_images=self.config.native_visual_memory_images and is_native_visual(model),
            **self.preparation_factories(),
        )
        return MediaInputSource(
            self.ledger,
            driver,
            required_modalities=self.model_modalities(model),
            validate_payload=driver.validate_payload,
            preparer=preparer,
            prefetch_batches=prefetch,
            sample_stream_capacity=(
                self.pipeline_selection(cfg)[0]
                if policy.frame_interval_ms == policy.window_ms
                and not policy.validate_full_window
                and not spec.input.binding.finite
                and not sampling.include_audio
                else 0
            ),
        )

    def preparation_factories(self):
        return (
            self.config.node_assembly.preparation_factories() if self.config.node_assembly else {}
        )

    def assembly_summary(self):
        plan = self.config.node_assembly
        if plan is None:
            return None
        return {
            "id": plan.id,
            "revision": plan.revision,
            "host_os": plan.host_os,
            "host_arch": plan.host_arch,
            "inputs": {k: v.model_dump() for k, v in plan.inputs.items()},
            "video_reader": plan.video_reader.model_dump(),
            "frame_preparer": plan.frame_preparer.model_dump(),
            "models": {k: v.model_dump(exclude={"service"}) for k, v in plan.models.items()},
            "placement_basis": "validated installed adapters and declared deployment; not hardware auto-discovery",
        }

    def resolve_start(self, args: StartObservation, principal, task_id):
        self._unchanged()
        matches = [
            (key, value)
            for key, value in self.config.executors.items()
            if args.executor in {key, value.title}
        ]
        if len(matches) != 1:
            raise Conflict("named observation executor is not installed")
        key, executor = matches[0]
        source = self.config.sources[executor.source_ref]
        if (
            isinstance(source, ManagedSourceSpec)
            and source.input.continuous_capture
            and not self.input_media.retention.configuration(principal)["policy"]["enabled"]
        ):
            raise Conflict(
                "continuous recording policy is disabled; read_recording_policy and configure_recording_policy "
                "must establish the authorized bounded storage policy before starting observation"
            )
        batch = executor.sampling_bindings().get(args.sampling)
        if batch is None:
            raise Conflict("executor does not support this sampling profile")
        duration = (
            args.duration_seconds
            if args.duration_seconds is not None
            else executor.default_duration_seconds
        )
        if args.continuous:
            if not (
                isinstance(source, ManagedSourceSpec)
                and source.input.continuous_capture
                and not source.input.binding.finite
                and executor.output_mode == "markers"
            ):
                raise Conflict("continuous mode requires managed live marker-only recording")
            duration = None
        deadline = self.store.clock() + duration if duration is not None else None
        # Only newly resolved, owned marker-only captures get a separate finite
        # recording deadline. Existing expires_at contracts remain hard fences.
        drain = bool(
            deadline is not None
            and isinstance(source, ManagedSourceSpec)
            and source.input.continuous_capture
            and source.input.incremental_video
            and source.input.capture is not None
            and source.input.capture.driver != "paired_stream"
            and executor.output_mode == "markers"
            and not set(executor.tools) - {"mark_media", "read_media", "get_edge_state"}
        )
        config = ObservationConfig(
            task_id=task_id,
            principal=principal,
            version=1,
            project_id=self.config.storage[executor.storage_binding_ref].project_id,
            device_ref=self.config.sources[executor.source_ref].device_ref,
            source_ref=executor.source_ref,
            session_ref="observation:" + digest([principal, task_id])[:32],
            goal=args.goal,
            storage_binding_ref=executor.storage_binding_ref,
            model_preset_ref=executor.model_preset_ref,
            batch_policy_ref=batch,
            cache_policy_ref=self.config.cache_policy_ref,
            save_policy_ref=executor.save_policy_ref,
            tools=executor.tools,
            executor_ref=key,
            expires_at=deadline + 30 if drain else deadline,
            capture_until=deadline if drain else None,
            resume_after_restart=drain,
            decision=args.decision,
            visual_rule=args.visual_rule,
            output_mode=executor.output_mode,
        )
        self.validate_start(config)
        return config

    def _source(self, cfg):
        self.validate_start(cfg)
        self.store.audit(
            cfg.task_id,
            "observation.installation.used",
            {
                "principal": cfg.principal,
                "version": cfg.version,
                "installation_digest": self.digest,
                "model_ref": cfg.model_preset_ref,
                "source_ref": cfg.source_ref,
                "model_admitted": False,
                "candidate_execution": True,
                "model_invoked": False,
                "phase": "source_binding",
                "node_assembly": self.assembly_summary(),
            },
        )
        spec = self.config.sources[cfg.source_ref]
        if self.config.node_assembly:
            binding = self.config.node_assembly.inputs[cfg.source_ref]
            source = resolve_adapter(binding, "input").factory(self, cfg)
            self.sources[cfg.task_id] = source
            return source
        if isinstance(spec, ManagedSourceSpec):
            source = self._managed_source(cfg)
            self.sources[cfg.task_id] = source
            return source
        source = self._replay_source(cfg)
        self.sources[cfg.task_id] = source
        return source

    def _replay_source(self, cfg):
        spec = self.config.sources[cfg.source_ref]
        batch = self.config.batches[cfg.batch_policy_ref]
        source = BudgetedReplaySource(
            self.source_path(cfg.principal, cfg.source_ref),
            cfg.source_ref,
            self.analysis / cfg.task_id,
            budget=self.cache_budget,
            memory_images=self.config.native_visual_memory_images and is_native_visual(
                self.config.models[cfg.model_preset_ref]
            ),
            window_ms=batch.window_ms,
            prefetch_batches=0 if self.pipeline_selection(cfg)[0] else batch.prefetch_batches,
            **self.preparation_factories(),
            sampling=SamplingPolicy(
                batch.frame_interval_ms,
                batch.image_long_edge,
                16000,
                include_audio="audio"
                in self.model_modalities(self.config.models[cfg.model_preset_ref]),
                include_image="image"
                in self.model_modalities(self.config.models[cfg.model_preset_ref]),
                image_region=batch.image_region,
                validate_full_window=batch.validate_full_window,
            ),
            repeat=spec.repeat,
            clock=self.store.clock,
            start_ms=spec.start_ms,
            end_ms=spec.end_ms,
            pacing="processing" if cfg.executor_ref else "realtime",
            complete_task_on_end=bool(
                cfg.executor_ref
                and not spec.repeat
                and self.config.executors[cfg.executor_ref].end_policy == "stop_after_replay"
            ),
            pose_reader=(lambda at: self.gimbal.at(cfg.principal, cfg.ptz_scope_ref, at))
            if cfg.ptz_scope_ref
            else None,
        )
        return source

    def pipeline_selection(self, cfg):
        """Resolve once at assembly, not by probing during every model request."""
        return self._select_pipeline(
            cfg.model_preset_ref,
            cfg.batch_policy_ref,
            cfg.source_ref,
            cfg.output_mode,
            cfg.ptz_scope_ref,
            bool(cfg.executor_ref),
        )

    def _select_pipeline(self, model_ref, batch_ref, source_ref, output_mode, ptz_scope, named):
        """Shared by capability discovery and task assembly; no I/O or health probes."""
        model = self.config.models[model_ref]
        requested = self.config.batches[batch_ref].pipeline_capacity
        reason = None
        if not isinstance(model, (NativeVisualPreset, ModelServicePreset)) or model.family not in {
            "clip",
            "blip_vqa",
        }:
            reason = "model_requires_sequential_context_adapter"
        elif output_mode != "markers" or ptz_scope:
            reason = "stateful_output_or_camera_control"
        elif not isinstance(self.config.sources[source_ref], ManagedSourceSpec) and not named:
            reason = "legacy_realtime_replay_pacing"
        if reason:
            if requested:
                raise ValueError("staged execution unsupported: " + reason)
            return 0, reason
        if requested == 0:
            return 0, "explicit_legacy_serial_configuration"
        return requested or 1, "independent_visual_markers"

    def execution_contract(self, executor, batch_ref):
        """Describe effective scheduling, including rejected explicit configurations."""
        batch = self.config.batches[batch_ref]
        try:
            capacity, reason = self._select_pipeline(
                executor.model_preset_ref,
                batch_ref,
                executor.source_ref,
                executor.output_mode,
                None,  # Named presets do not bind task-specific camera control.
                True,
            )
        except ValueError as exc:
            return {"mode": "unsupported", "reason": str(exc), "queue_capacity": 0}
        return {
            "mode": "staged" if capacity else "sequential",
            "reason": reason,
            "queue_capacity": capacity,
            "workers": ["prepare", "infer", "commit"] if capacity else ["sequential"],
            "preparation_prefetch_batches": 0 if capacity else batch.prefetch_batches,
            "model_requests_in_flight": 1,
        }

    def sampling_execution_contract(self, executor, batch_ref):
        execution = self.execution_contract(executor, batch_ref)
        return {
            "execution": execution,
            "preparation_prefetch_batches": execution.get("preparation_prefetch_batches", 0),
            "model_requests_in_flight": 1,
        }

    def _runner(self, cfg):
        model = self.config.models[cfg.model_preset_ref]
        capacity, reason = self.pipeline_selection(cfg)
        self.store.audit(
            cfg.task_id,
            "observation.execution.selected",
            {
                "mode": "staged" if capacity else "sequential",
                "reason": reason,
                "queue_capacity": capacity,
            },
        )
        runner_type, staged_args = SemanticObservationRunner, {}
        if capacity:
            from .observation_staged import StagedObservationRunner

            source = self.sources[cfg.task_id]
            if (
                not isinstance(model, (NativeVisualPreset, ModelServicePreset))
                or model.family not in {"clip", "blip_vqa"}
                or not isinstance(source, (BudgetedReplaySource, MediaInputSource))
                or (isinstance(source, BudgetedReplaySource) and source.pacing != "processing")
                or cfg.output_mode != "markers"
                or cfg.ptz_scope_ref
            ):
                raise ValueError("staged executor requires independent visual marker input")
            runner_type, staged_args = (
                StagedObservationRunner,
                {
                    "pipeline_capacity": capacity,
                    "pipeline_trace": self.config.batches[cfg.batch_policy_ref].pipeline_trace,
                },
            )
        if isinstance(
            model, (QwenPreset, ExternalSemanticPreset, NativeVisualPreset, ModelServicePreset)
        ):
            return runner_type(
                self.ledger,
                self.sources[cfg.task_id],
                self.decisions,
                self.gimbal,
                process_root=self.processes / cfg.model_preset_ref,
                command=model.command() if isinstance(model, QwenPreset) else (),
                port=model.port if isinstance(model, QwenPreset) else None,
                context_tokens=model.context_tokens,
                asset_guard=lambda: self.validate(cfg),
                backend=self.semantic_backend(model),
                trace_timings=self.config.batches[cfg.batch_policy_ref].pipeline_trace,
                context_mode=self.config.executors[cfg.executor_ref].context_mode
                if cfg.executor_ref
                else "rolling_history",
                **staged_args,
            )
        return ManagedObservationRunner(
            self.ledger,
            self.sources[cfg.task_id],
            self.decisions,
            self.gimbal,
            process_root=self.processes / cfg.model_preset_ref,
            command=model.command(),
            port=model.port,
            settings=ObservationRuntimeSettings(
                context_tokens=model.context_tokens, output_tokens=model.output_tokens
            ),
            skill_sources={name: Path(self.config.skills[name].path) for name in cfg.skills},
            asset_guard=lambda: self.validate(cfg),
            media_writer=self.cache_budget.write_native,
        )

    def _backfill_runner(self, cfg, batch):
        original = self.ledger.instruction_config(cfg.principal, cfg.task_id, batch.version)
        self.validate_start(original)
        model = self.config.models[original.model_preset_ref]
        sampling = self.config.batches[original.batch_policy_ref]
        spec = self.config.sources[original.source_ref]
        # A historical reader never subscribes or advances the live input cursor.
        source = (
            self._managed_source(original)
            if isinstance(spec, ManagedSourceSpec)
            else BudgetedReplaySource(
                self.source_path(cfg.principal, original.source_ref),
                original.source_ref,
                self.analysis / cfg.task_id,
                budget=self.cache_budget,
                **self.preparation_factories(),
                window_ms=sampling.window_ms,
                start_ms=spec.start_ms,
                end_ms=spec.end_ms,
                sampling=SamplingPolicy(
                    sampling.frame_interval_ms,
                    sampling.image_long_edge,
                    16000,
                    include_audio="audio" in self.model_modalities(model),
                    include_image="image" in self.model_modalities(model),
                    image_region=sampling.image_region,
                ),
            )
        )
        if isinstance(
            model, (QwenPreset, ExternalSemanticPreset, NativeVisualPreset, ModelServicePreset)
        ):
            return SemanticObservationRunner(
                self.ledger,
                source,
                self.decisions,
                self.gimbal,
                process_root=self.processes / original.model_preset_ref,
                command=model.command() if isinstance(model, QwenPreset) else (),
                port=model.port if isinstance(model, QwenPreset) else None,
                context_tokens=model.context_tokens,
                asset_guard=lambda: self.validate(original),
                backend=self.semantic_backend(model),
                mode="backfill",
                observation_version=batch.version,
                owns_source=True,
                context_mode=self.config.executors[original.executor_ref].context_mode
                if original.executor_ref
                else "rolling_history",
            )
        return ManagedObservationRunner(
            self.ledger,
            source,
            self.decisions,
            self.gimbal,
            process_root=self.processes / original.model_preset_ref,
            command=model.command(),
            port=model.port,
            settings=ObservationRuntimeSettings(
                context_tokens=model.context_tokens, output_tokens=model.output_tokens
            ),
            skill_sources={name: Path(self.config.skills[name].path) for name in original.skills},
            asset_guard=lambda: self.validate(original),
            media_writer=self.cache_budget.write_native,
            mode="backfill",
            observation_version=batch.version,
            owns_source=True,
        )

    @staticmethod
    def model_modalities(model):
        if isinstance(model, ExternalSemanticPreset):
            return model.provider.modalities
        if isinstance(model, (NativeVisualPreset, ModelServicePreset)):
            return ("image",)
        if isinstance(model, QwenPreset) and model.audio_provider is None:
            return ("image",)
        return ("image", "audio")

    def semantic_backend(self, model):
        if self.config.node_assembly:
            matches = [
                binding
                for binding in self.config.node_assembly.models.values()
                if binding.service == model
            ]
            if len(matches) != 1:
                raise Conflict("model adapter binding is ambiguous or missing")
            return AssembledModelBackend(matches[0])
        if isinstance(model, ModelServicePreset):
            return ModelServiceBackend(model)
        if isinstance(model, NativeVisualPreset):
            return NativeVisualBackend(model)
        if isinstance(model, ExternalSemanticPreset):
            return ExternalSemanticBackend(model.provider)
        if isinstance(model, QwenPreset) and model.audio_provider is not None:
            return QwenAudioBackend(model, model.fusion_policy)
        return None

    async def _stop_motion(self, cfg, run):
        return self.gimbal.stop_run(cfg, run)

    async def _recover(self, cfg, run):
        model = self.config.models[cfg.model_preset_ref]
        process = self.recovering_processes.get(cfg.model_preset_ref)
        if process is None:
            backend = self.semantic_backend(model)
            if backend:
                process = backend.process(self.processes / cfg.model_preset_ref)
            else:
                process_type = (
                    QwenModelProcess if isinstance(model, QwenPreset) else NativeModelProcess
                )
                process = process_type(
                    self.processes / cfg.model_preset_ref, model.command(), model.port
                )
            self.recovering_processes[cfg.model_preset_ref] = process
        try:
            closed = await process.close()
        finally:
            if process.released:
                self.recovering_processes.pop(cfg.model_preset_ref, None)
        motion = self.gimbal.stop_run(cfg, run)
        if self.config.node_assembly:
            binding = self.config.node_assembly.inputs[cfg.source_ref]
            source_closed = await resolve_adapter(binding, "input").recover(self, cfg, run)
        else:
            source_closed = await self._recover_source(cfg, run)
        return {
            "source": source_closed,
            "model": closed,
            "motion": motion["confirmed"],
            "source_simulated": (
                self.config.sources[cfg.source_ref].input.binding.simulated
                if isinstance(self.config.sources[cfg.source_ref], ManagedSourceSpec)
                else True
            ),
            "inference_simulated": getattr(
                self.semantic_backend(model), "inference_simulated", False
            ),
            "motion_simulated": True,
        }

    async def _recover_source(self, cfg, run):
        spec = self.config.sources[cfg.source_ref]
        source_closed = True
        if (
            isinstance(spec, ManagedSourceSpec)
            and spec.input.capture is not None
            and spec.input.capture.driver != "paired_stream"
        ):
            from .input_capture import recover_capture

            source_closed = await recover_capture(self.input_media, cfg.task_id)
        elif isinstance(spec, ManagedSourceSpec) and not spec.input.binding.finite:
            source_closed = self.input_media.client_stop_confirmed(
                run.principal,
                cfg.source_ref,
                since=self.input_media.source_run_started(
                    run.principal, run.task_id, run.generation
                ),
            )
        return source_closed

    async def close_resources(self):
        for name, process in list(self.recovering_processes.items()):
            try:
                await process.close()
            finally:
                if process.released:
                    self.recovering_processes.pop(name, None)
        closed = not self.recovering_processes
        return {
            "confirmed": closed,
            "lock_released": closed,
            "pending_callbacks": len(self.recovering_processes),
        }

    @staticmethod
    def model_contract(model):
        if is_native_visual(model):
            return {
                "input": "image_and_text",
                "family": model.family,
                "revision": model.revision,
                "output": model.result_type,
                "task_field": "visual_rule",
                "rule_kind": "text_similarity" if model.family == "clip" else "visual_question",
                "language": "English question/descriptions; task goal may be Chinese",
                "chat_history": False,
                "tools_called_by_model": False,
                "unknown": "protect; invalid input/model failure is an error, not absence",
                "limitations": "sampled-frame candidates only; no object identity, tracking, audio or action guarantee",
            }
        return {
            "input": "media_and_semantic_choices",
            "output": "semantic_choice",
            "task_field": "decision",
            "rule_kind": "three_semantic_choices",
        }

    def catalogue(self):
        return {
            "node_assembly": self.assembly_summary(),
            "digest": self.digest,
            "configured": True,
            "model_admitted": False,
            "execution_mode": "candidate_probe" if self.config.allow_candidate_runs else "disabled",
            "sources": [
                (
                    {
                        "id": key,
                        "title": value.title,
                        "device_ref": value.device_ref,
                        "simulated": value.input.binding.simulated,
                        "kind": value.input.binding.kind,
                        "managed_input": True,
                        "capture_duration_ms": value.input.replay_duration_ms,
                        "modalities": list(value.input.binding.modalities),
                        "start_ms": 0,
                        "end_ms": None,
                    }
                    if isinstance(value, ManagedSourceSpec)
                    else {
                        "id": key,
                        "title": value.title,
                        "device_ref": value.device_ref,
                        "simulated": True,
                        "kind": "file_replay",
                        "start_ms": value.start_ms,
                        "end_ms": value.end_ms,
                    }
                )
                for key, value in self.config.sources.items()
            ],
            "models": [
                {
                    "id": key,
                    "title": value.title,
                    "capability": self.model_contract(value),
                    "context_tokens": None if is_native_visual(value) else value.context_tokens,
                    "output_tokens": (0 if value.family == "clip" else 12)
                    if is_native_visual(value)
                    else value.output_tokens,
                    "admitted": False,
                    "lifecycle": "external_service"
                    if isinstance(value, ModelServicePreset)
                    else "task_managed",
                }
                for key, value in self.config.models.items()
            ],
            "batches": {key: value.model_dump() for key, value in self.config.batches.items()},
            "saves": {key: value.model_dump() for key, value in self.config.saves.items()},
            "storage": {key: value.model_dump() for key, value in self.config.storage.items()},
            "cache": {
                "id": self.config.cache_policy_ref,
                "quota_bytes": self.config.analysis_quota_bytes,
            },
            "tools": list(ObservationTools.names),
            "skills": list(self.config.skills),
            "gimbals": list(self.config.gimbals),
            "executors": [
                {
                    "id": key,
                    "title": value.title,
                    "runtime": "native_visual_rules"
                    if is_native_visual(self.config.models[value.model_preset_ref])
                    else "semantic_routes",
                    "task_contract": self.model_contract(
                        self.config.models[value.model_preset_ref]
                    ),
                    "model_ref": value.model_preset_ref,
                    "source_ref": value.source_ref,
                    "project_id": self.config.storage[value.storage_binding_ref].project_id,
                    "tools": list(value.tools),
                    "context_mode": value.context_mode,
                    "configured": True,
                    "admitted": False,
                    "replay_mode": "non_realtime",
                    "start_permitted": self.config.allow_candidate_runs,
                    "scope": "semantic_media_functional",
                    "modalities": list(
                        self.model_modalities(self.config.models[value.model_preset_ref])
                    ),
                    "end_policy": value.end_policy,
                    "default_duration_seconds": value.default_duration_seconds,
                    "output_mode": value.output_mode,
                    "merges_events": value.output_mode != "markers",
                    "automatic_video_export": value.output_mode != "markers",
                    "audio": {
                        "preserve_original": True,
                        "raw_audio_input": "audio"
                        in self.model_modalities(self.config.models[value.model_preset_ref]),
                        "environment_understanding": getattr(
                            self.semantic_backend(self.config.models[value.model_preset_ref]),
                            "environment_sound",
                            False,
                        ),
                    },
                    "sampling": [
                        {
                            "id": name,
                            "frames_per_second": 1000 / self.config.batches[ref].frame_interval_ms,
                            "frame_interval_ms": self.config.batches[ref].frame_interval_ms,
                            "window_seconds": self.config.batches[ref].window_ms / 1000,
                            "new_images_per_full_batch": self.config.batches[ref].window_ms
                            // self.config.batches[ref].frame_interval_ms
                            if "image"
                            in self.model_modalities(self.config.models[value.model_preset_ref])
                            else 0,
                            "image_region": self.config.batches[ref].image_region,
                            **self.sampling_execution_contract(value, ref),
                            "image_coverage_check": "full_window"
                            if self.config.batches[ref].validate_full_window
                            else "sampled_frames_only",
                        }
                        for name, ref in value.sampling_bindings().items()
                    ],
                }
                for key, value in self.config.executors.items()
            ],
        }

    async def model_status(self, principal):
        """Inspect existing owners only: opening settings must not load weights."""
        if principal != "local-user":
            raise Conflict("observation principal mismatch")
        result = []
        for ref, model in self.config.models.items():
            folder = self.processes / ref
            owners = [
                owner
                for owner in self.gateway.owned.values()
                if owner.run.principal == principal
                and owner.runner is not None
                and getattr(owner.runner, "process_root", None) == folder
            ]
            current = [owner for owner in owners if not owner.shutdown_confirmed]
            selected = current or owners[-1:]
            state = {"state": "not_started", "server_reachable": False, "context_ready": False}
            if len(current) > 1:
                state = {"state": "unconfirmed", "server_reachable": None, "context_ready": False}
            elif selected:
                owner = selected[0]
                runner = owner.runner
                state = await runner.model_status()
                if owner.runner is not runner:
                    state = {"state": "changing", "server_reachable": None, "context_ready": False}
            elif isinstance(model, ModelServicePreset):
                path = folder / "external-requests.json"
                pending = json.loads(path.read_text()) if path.exists() else []
                state = {
                    "state": "recovery_pending" if pending else "not_started",
                    "externally_managed": True,
                    "server_reachable": None,
                    "context_ready": False,
                    "unconfirmed_requests": len(pending),
                    "service_terminated": False,
                }
            elif (folder / "requests.json").exists():
                pending = json.loads((folder / "requests.json").read_text()).get("requests", [])
                state = {
                    "state": "recovery_pending" if pending else "stopped",
                    "externally_managed": True,
                    "server_reachable": None,
                    "context_ready": False,
                    "pending_requests": len(pending),
                }
            elif (folder / "process.json").exists():
                # A previous process journal without an in-memory owner requires
                # recovery. Do not construct a controller or adopt a listener.
                try:
                    record = json.loads((folder / "process.json").read_text())
                    status = "stopped" if record.get("status") == "stopped" else "recovery_pending"
                except (OSError, ValueError):
                    status = "unconfirmed"
                state = {
                    "state": status,
                    "server_reachable": False if status == "stopped" else None,
                    "context_ready": False,
                }
            result.append(
                {
                    "id": ref,
                    "title": model.title,
                    "backend": model.backend
                    if isinstance(
                        model, (ExternalSemanticPreset, NativeVisualPreset, ModelServicePreset)
                    )
                    else "llama.cpp-qwen-vision"
                    if isinstance(model, QwenPreset)
                    else "llama.cpp-omni",
                    "configured_gpu_layers": None
                    if isinstance(model, (ExternalSemanticPreset, ModelServicePreset))
                    else model.gpu_layers,
                    "context_tokens": model.context_tokens,
                    "admitted": False,
                    **state,
                    "checked_at": self.store.clock(),
                }
            )
        return result

    def installation(self):
        if self.gateway is not None:
            raise Conflict("observation registry is already installed")
        self._unchanged()
        with self.store.connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO observation_installation_snapshots VALUES (?,?,?)",
                (self.digest, canonical(self.config), self.store.clock()),
            )
        self.store.set_meta(
            "observation_installation",
            {"digest": self.digest, "configuration": self.config.model_dump(mode="json")},
        )
        self.gateway = ObservationGateway(
            self.ledger,
            self._source,
            self._runner,
            self._stop_motion,
            policy=self.config.gateway_policy,
            recover_resources=self._recover,
            backfill_factory=self._backfill_runner,
        )
        try:
            cleanup = ObservationCacheCleanup(
                self.decisions,
                self.analysis,
                self.source_path,
                captured_resolver=self.captured_path,
            )
            self.retention = ObservationRetentionWorker(
                self.decisions,
                self.archive,
                max_output_bytes=self.config.max_output_bytes,
                cleanup=cleanup,
            )
        except BaseException:
            # No run exists until the host has finished starting.
            import fcntl

            from .observation_gateway import _active_gateways

            fcntl.flock(self.gateway.owner_lock, fcntl.LOCK_UN)
            self.gateway.owner_lock.close()
            _active_gateways.pop(self.gateway.instance_key, None)
            self.gateway = None
            raise
        return ObservationInstallation(
            self.gateway,
            self.validate,
            self.archive,
            self.retention,
            catalogue=self.catalogue,
            validate_start=self.validate_start,
            close_resources=self.close_resources,
            gimbal=self.gimbal,
            model_status=self.model_status,
            resolve_start=self.resolve_start,
            decisions=self.decisions,
        )


def load_observation_installation(host, path):
    return ObservationRegistry(host, path).installation()
