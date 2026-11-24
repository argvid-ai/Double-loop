"""Node assembly: bind trusted implementations to shared observation policies."""

import hashlib
import json
import platform
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .contracts import ID, Contract
from .model_service_connection import ModelServicePreset, ModelServiceBackend
from .node_adapters import resolve
from .observation_media import _fingerprint


class AdapterBinding(Contract):
    adapter: str = Field(pattern=ID)
    at: str = Field(
        pattern=r"^((edge|cloud)\.(harness|model_service)\.(cpu|gpu|npu)|remote\.model_service\.external)$"
    )


class ServiceAssembly(Contract):
    request: AdapterBinding
    transport: AdapterBinding
    compute: AdapterBinding
    service: ModelServicePreset

    @model_validator(mode="after")
    def compatible(self):
        request = resolve(self.request, "request")
        resolve(self.transport, "transport")
        resolve(self.compute, "compute")
        if self.transport.adapter == "loopback-http.v1" and urlsplit(
            self.service.endpoint
        ).hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("loopback transport requires a local model service")
        if request.family != self.service.family:
            raise ValueError("request adapter does not match model service family")
        return self


class AssemblyPlan(Contract):
    id: str = Field(pattern=ID)
    revision: int = Field(ge=1)
    host_os: str
    host_arch: str
    inputs: dict[str, AdapterBinding] = Field(min_length=1, max_length=32)
    video_reader: AdapterBinding
    frame_preparer: AdapterBinding
    models: dict[str, ServiceAssembly] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def installed(self):
        resolve(self.video_reader, "video_reader")
        resolve(self.frame_preparer, "frame_preparer")
        for binding in self.inputs.values():
            if resolve(binding, "input").recover is None:
                raise ValueError("input adapter must implement restart recovery")
        identities = [(b.service.endpoint, b.service.model_alias) for b in self.models.values()]
        if len(identities) != len(set(identities)):
            raise ValueError("model service bindings must have unique identities")
        return self

    def check_host(self):
        if (self.host_os, self.host_arch) != (
            platform.system().lower(),
            platform.machine().lower(),
        ):
            raise ValueError("node assembly host OS/architecture differs from this runtime")

    def preparation_factories(self):
        return {
            "video_reader_factory": resolve(self.video_reader, "video_reader").factory,
            "frame_preparer_factory": resolve(self.frame_preparer, "frame_preparer").factory,
        }


class InstallationReference(Contract):
    path: str = Field(min_length=1, max_length=1000)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class NodeAssemblyFile(Contract):
    schema_version: Literal["node-assembly/1"]
    installation: InstallationReference
    assembly: AssemblyPlan
    media_paths: dict[str, str] = Field(default_factory=dict, max_length=32)


def load_installation(path):
    """No model start, dynamic import, or configuration writes during assembly."""
    path = Path(path).resolve(strict=True)
    before = _fingerprint(path)
    if path.stat().st_size > 1024**2:
        raise ValueError("observation configuration exceeds size bound")
    value = json.loads(path.read_text())
    if _fingerprint(path) != before:
        raise ValueError("assembly changed while loading")
    files = {str(path): before}
    if value.get("schema_version") != "node-assembly/1":
        return value, files
    spec = NodeAssemblyFile.model_validate(value)
    spec.assembly.check_host()
    base = (path.parent / spec.installation.path).resolve(strict=True)
    if base == path or base.stat().st_size > 1024**2:
        raise ValueError("invalid shared installation reference")
    identity = _fingerprint(base)
    raw = base.read_bytes()
    if (
        hashlib.sha256(raw).hexdigest() != spec.installation.sha256
        or _fingerprint(base) != identity
    ):
        raise ValueError("shared installation differs from pinned assembly reference")
    value = json.loads(raw)
    if value.get("schema_version", "observation-installation/1") not in {
        "observation-installation/1",
        "observation-policy-template/1",
    } or value.get("node_assembly"):
        raise ValueError("nested node assemblies are not supported")
    value["schema_version"] = "observation-installation/1"
    if set(spec.assembly.models) != set(value["models"]) or set(spec.assembly.inputs) != set(
        value["sources"]
    ):
        raise ValueError("assembly must explicitly bind all installed models and inputs")
    for key, location in spec.media_paths.items():
        if (
            key not in value["sources"]
            or "file" not in value["sources"][key]
            or not Path(location).is_absolute()
        ):
            raise ValueError("media override must bind a registered replay to an absolute path")
        value["sources"][key]["file"]["path"] = location
    value["models"] = {
        key: binding.service.model_dump(mode="json")
        for key, binding in spec.assembly.models.items()
    }
    value["node_assembly"] = spec.assembly.model_dump(mode="json")
    files[str(base)] = identity
    return value, files


class AssembledModelBackend(ModelServiceBackend):
    """Reuse the semantic loop; only factories and declared placement differ."""

    def __init__(self, binding):
        super().__init__(binding.service)
        self.binding = binding

    def process(self, root):
        return resolve(self.binding.transport, "transport").factory(self.model, root)

    def scorer(self, process, payloads, principal, task_id, audit, guard):
        audit(
            "observation.adapters.model",
            {
                "request": self.binding.request.model_dump(),
                "transport": self.binding.transport.model_dump(),
                "compute": self.binding.compute.model_dump(),
                "compute_identity_basis": "declared_deployment_and_expected_service_alias; not accelerator_auto_detection",
            },
        )
        return resolve(self.binding.request, "request").factory(
            self.model, process, payloads, principal, task_id, audit, guard
        )
