from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from cloud_loop.contracts import Constraints, Invocation, ModelCapabilities, load_profile, utcnow
from cloud_loop.plugins import require_profile, validate_profile


def profile():
    return load_profile(Path(__file__).parents[1] / "profiles/demo-bridge.json")


def test_disabled_remote_tool_does_not_break_transport():
    p = profile()
    p.cloud.plugins.remove("remote-capture")
    b = require_profile(p)
    assert "edge_capture_task" not in b["cloud"]["tools"]
    assert "bridge" in b["cloud"]["capabilities"]


@pytest.mark.parametrize(
    "remove,add", [("safety-guard", None), (None, "semif"), (None, "codex"), (None, "nanobot")]
)
def test_invalid_assembly_fails_closed(remove, add):
    p = profile()
    if remove:
        p.edge.plugins.remove(remove)
    if add:
        p.edge.plugins.append(add)
    assert not validate_profile(p)["valid"]


def test_constraints_cannot_disable_presence_or_enable_flash():
    for patch in [
        {"requires_user_presence": False},
        {"flash_allowed": True},
        {"minimum_duration_ms": 4},
    ]:
        with pytest.raises(ValidationError):
            Constraints(**patch)


def test_wire_round_trip_preserves_constraints_and_correlation():
    now = utcnow()
    message = Invocation(
        message_id="m1",
        node_id="n1",
        sent_at=now,
        expires_at=now + timedelta(seconds=10),
        idempotency_key="cloud1:shot1",
        original_call_id="native-call",
        task_id="e1",
        arguments={"prompt": "拍近景", "constraints": {"minimum_duration_ms": 6000}},
    )
    restored = Invocation.model_validate_json(message.model_dump_json())
    assert restored.arguments.constraints.orientation == "portrait"
    assert restored.arguments.constraints.minimum_duration_ms == 6000
    assert restored.original_call_id == "native-call"
    with pytest.raises(ValidationError):
        Invocation.model_validate(
            {**message.model_dump(), "protocol_version": "edge-cloud-photo/1.0"}
        )


def test_profile_identity_changes_with_tools_and_model():
    p = profile()
    old = p.fingerprint
    p.cloud.model = "DeepSeek-V4-Flash"
    assert p.fingerprint != old
    assert require_profile(p)["cloud"]["model"] == "DeepSeek-V4-Flash"


def test_verified_gateway_route_uses_declared_capability_and_protocol():
    p = profile()
    p.cloud.model = "GPT-6.1-Sol[1m]"
    p.cloud.model_capabilities = ModelCapabilities(
        model_id=p.cloud.model,
        context_window=1_000_000,
        max_output_tokens=128_000,
        input_modalities=["text", "image"],
        source="gateway model listing fixture",
    )
    assert require_profile(p)["cloud"]["model"] == p.cloud.model
    p.cloud.model_capabilities.model_id = "different-model"
    assert not validate_profile(p)["valid"]


def test_tool_schema_preserves_title_fields_and_nested_objects():
    from cloud_loop.contracts import Contract, tool_schema
    from cloud_loop.harness_media_review import ReviewRenderedMedia

    class NamedInput(Contract):
        title: str

    class NestedInput(Contract):
        source: NamedInput = NamedInput(title="Preserve this value")

    assert "title" in tool_schema(ReviewRenderedMedia)["properties"]
    nested = tool_schema(NestedInput)["properties"]["source"]
    assert nested["properties"]["title"]["type"] == "string"
    assert nested["required"] == ["title"]
    assert nested["default"] == {"title": "Preserve this value"}
    assert "title" not in nested
