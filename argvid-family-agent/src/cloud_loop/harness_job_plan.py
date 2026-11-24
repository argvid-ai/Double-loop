"""Bind a saved media goal to verified intermediate files without rewriting the goal."""

from .harness_media_contracts import MediaRecipe
from .store import Conflict


def prepare_job_plan(host, task, operation, requested=None):
    saved = task["constraints"].get("media_recipe")
    if not saved:
        if task["kind"] in {"goal", "edit"} and requested:
            selected = MediaRecipe.model_validate(requested)
            duration = task["constraints"].get("duration_ms")
            if duration and (
                selected.duration_ms != duration
                if operation == "compose"
                else selected.duration_ms > duration
            ):
                raise Conflict("period plan must respect saved target duration")
            saved = selected.model_dump(mode="json")
        elif host.settings.legacy_media_compatibility:
            if requested:
                raise Conflict("save an explicit task media recipe before selecting job inputs")
            return None
        else:
            raise Conflict(
                "explicit media_recipe required; a continuing goal can select this period inputs in start_job"
            )
    planned = MediaRecipe.model_validate(saved)
    actual = MediaRecipe.model_validate(requested or saved)
    if actual.renderer == "remotion" and (
        operation != "compose" or not host.settings.remotion_runtime_dir
    ):
        raise Conflict("Remotion requires a configured composition backend on this node")
    geometry = ("width", "height", "fps", "audio", "renderer")
    if any(getattr(actual, k) != getattr(planned, k) for k in geometry):
        raise Conflict("job cannot change saved output dimensions, rate or audio policy")
    available = {a["id"]: a for a in host.effects.composition_assets(task["id"], task["principal"])}
    captured = []
    expanded = []
    bindings = []
    for segment in actual.segments:
        wire = segment.model_dump(mode="json")
        if segment.source_id:
            # Capture may refer to a registered source on this executor. Its
            # current bytes are checked again by the materializer before use.
            source = host.sources.get(task["principal"], segment.source_id)
            if segment.start_ms + segment.duration_ms > source["facts"]["duration_ms"]:
                raise Conflict("segment exceeds registered source")
            expanded.append(wire)
            bindings.append({"source_id": segment.source_id, "sha256": source["facts"]["sha256"]})
            continue
        asset = available.get(segment.asset_id)
        if not asset:
            raise Conflict("selected intermediate is not verified for this task/version")
        info = asset["info"]
        facts = info["asset"]
        if segment.start_ms + segment.duration_ms > facts["duration_ms"]:
            raise Conflict("segment exceeds verified intermediate")
        if asset["kind"] == "capture":
            captured.append(asset["id"])
        bindings.append({"asset_id": asset["id"], "sha256": facts["sha256"]})
        # An explicitly planned asset can be used directly. Replacing planned
        # source segments with a completed capture is allowed only when its
        # receipt proves the same whole segments, in the same output geometry.
        if wire in [s.model_dump(mode="json") for s in planned.segments]:
            expanded.append(wire)
            continue
        if (
            segment.start_ms != 0
            or segment.duration_ms != facts["duration_ms"]
            or segment.crop is not None
        ):
            raise Conflict(
                "replacement intermediate must be used whole; modify the task to request a different trim"
            )
        original = info.get("requested_recipe") or info.get("recipe")
        if not original or any(
            original.get(k, "ffmpeg" if k == "renderer" else None) != getattr(planned, k)
            for k in geometry
        ):
            raise Conflict("intermediate receipt cannot establish the saved source selection")
        original = MediaRecipe.model_validate(original)
        if original.duration_ms != segment.duration_ms:
            raise Conflict("intermediate receipt duration mismatch")
        expanded.extend(s.model_dump(mode="json") for s in original.segments)
    expected = [s.model_dump(mode="json") for s in planned.segments]
    if operation == "compose":
        host.media_reviews.require_inputs(task, actual)
        if expanded != expected:
            raise Conflict("selected inputs change saved segment order or selection")
        if task["constraints"].get("requires_capture") and not captured:
            raise Conflict("this task requires a completed capture intermediate before composition")
    else:
        # One capture can materialize an ordered subset of this matter's plan.
        remaining = iter(expected)
        for segment in expanded:
            if not any(candidate == segment for candidate in remaining):
                raise Conflict("capture must be an ordered subset of the saved media plan")
    requested = (
        planned.model_dump(mode="json")
        if operation == "compose"
        else {**planned.model_dump(mode="json"), "segments": expanded}
    )
    return {
        "recipe": actual.model_dump(mode="json"),
        "requested_recipe": requested,
        "saved_task_recipe": planned.model_dump(mode="json"),
        "expanded_segments": expanded,
        "input_bindings": bindings,
        "capture_asset_ids": captured,
        "requires_capture": bool(task["constraints"].get("requires_capture")),
        "operation": operation,
        "authority": "saved_task_and_verified_asset_receipts",
    }
