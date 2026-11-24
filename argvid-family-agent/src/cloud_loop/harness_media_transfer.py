"""Explicit intermediate-file transfer. Final deliverables normally stay at executor."""

import base64
import hashlib
import tempfile
from pathlib import Path

from .contracts import canonical, digest
from .harness_contracts import PeerMessage
from .harness_media_contracts import MediaRecipe
from .harness_media_render import check_recipe_output
from .media import file_hash
from .store import Conflict


def validate_manifest(manifest, request):
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != "cix.media-manifest/0.4"
        or type(manifest.get("simulated")) is not bool
    ):
        raise Conflict("unsupported functional media manifest")
    recipe = MediaRecipe.model_validate(manifest["recipe"])
    requested = MediaRecipe.model_validate(manifest.get("requested_recipe") or manifest["recipe"])
    if requested.model_dump(mode="json") != request.payload.get("media_recipe"):
        raise Conflict("result recipe differs from delegated work")
    if recipe.model_dump(mode="json") != requested.model_dump(mode="json"):
        receipt = manifest.get("selection_receipt") or {}
        if receipt.get("expanded_segments") != requested.model_dump(mode="json")["segments"] or any(
            getattr(recipe, k) != getattr(requested, k)
            for k in ("width", "height", "fps", "audio", "duration_ms")
        ):
            raise Conflict("materialized inputs do not prove the requested selection")
    if request.payload.get("operation") == "compose" and request.payload.get("task", {}).get(
        "constraints", {}
    ).get("requires_capture"):
        if not (manifest.get("selection_receipt") or {}).get("capture_asset_ids"):
            raise Conflict("required captured intermediate missing")
    facts = manifest.get("asset") or {}
    if (
        facts.get("width"),
        facts.get("height"),
        facts.get("frames"),
        facts.get("duration_ms"),
        facts.get("codec"),
    ) != (
        recipe.width,
        recipe.height,
        recipe.duration_ms * recipe.fps // 1000,
        recipe.duration_ms,
        "h264",
    ):
        raise Conflict("manifest output does not match recipe")
    if (
        abs(facts.get("fps", 0) - recipe.fps) > 0.01
        or facts.get("simulated") is not manifest["simulated"]
        or (
            facts.get("audio_streams") != 0
            if recipe.audio == "mute"
            else facts.get("audio_streams") not in (0, 1)
        )
    ):
        raise Conflict("invalid media rate, audio or provenance")
    if (
        not isinstance(facts.get("sha256"), str)
        or len(facts["sha256"]) != 64
        or not facts.get("asset_id")
    ):
        raise Conflict("manifest lacks asset identity")
    return recipe, facts


def receive_media(host, upload):
    store = host.store
    with store.connect() as db:
        row = db.execute(
            "SELECT body FROM peer_mail WHERE id=? AND direction='out'", (upload.request_id,)
        ).fetchone()
    if not row:
        raise Conflict("media upload has no originating request")
    request = PeerMessage.model_validate_json(row["body"])
    if (
        request.kind != "task"
        or request.principal != upload.principal
        or request.payload.get("transfer_result") is not True
    ):
        raise Conflict("request does not authorize file transfer")
    task = store.business_task(request.task_id, upload.principal)
    recipe, remote = validate_manifest(upload.manifest, request)
    data = base64.b64decode(upload.content_base64, validate=True)
    if (
        len(data) != remote.get("size_bytes")
        or hashlib.sha256(data).hexdigest() != remote["sha256"]
    ):
        raise Conflict("media bytes differ from manifest")
    asset_id = "received-" + digest({"request": request.id, "asset": remote["asset_id"]})[:32]
    root = store.root / "assets"
    root.mkdir(parents=True, exist_ok=True)
    target = root / (asset_id + ".mp4")
    with store.connect() as db:
        prior = db.execute(
            "SELECT * FROM transferred_assets WHERE request_id=? AND asset_id=?",
            (request.id, remote["asset_id"]),
        ).fetchone()
        if prior:
            saved = db.execute("SELECT info FROM assets WHERE id=?", (asset_id,)).fetchone()
            import json

            expected = {
                **upload.manifest,
                "asset": {
                    **remote,
                    "asset_id": asset_id,
                    "uri": host.side + "-asset://" + asset_id,
                },
                "remote_asset_id": remote["asset_id"],
            }
            if (
                not saved
                or json.loads(saved["info"]) != expected
                or prior["sha256"] != remote["sha256"]
                or not target.is_file()
                or file_hash(target) != remote["sha256"]
            ):
                raise Conflict("committed media transfer changed")
            return {"verified": True, "duplicate": True, "asset_id": asset_id}
    if (
        task["version"] != request.task_version
        or task["status"] in {"paused", "cancelled", "completed", "expired"}
        or request.expires_at <= host.mail.now()
    ):
        raise Conflict("media upload is stale")
    with tempfile.NamedTemporaryFile(dir=root, suffix=".incoming.mp4", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
    try:
        facts = check_recipe_output(temporary, recipe)
        for key in (
            "sha256",
            "size_bytes",
            "width",
            "height",
            "frames",
            "duration_ms",
            "audio_streams",
        ):
            if facts[key] != remote[key]:
                raise Conflict("decoded media facts differ from manifest")
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            fresh = store._task(db, task["id"], upload.principal)
            if (
                fresh["version"] != request.task_version
                or fresh["status"] in {"paused", "cancelled", "completed", "expired"}
                or request.expires_at <= host.mail.now()
            ):
                raise Conflict("media upload superseded during verification")
            prior = db.execute(
                "SELECT * FROM transferred_assets WHERE request_id=? AND asset_id=?",
                (request.id, remote["asset_id"]),
            ).fetchone()
            if prior:
                if (
                    prior["sha256"] != remote["sha256"]
                    or not target.is_file()
                    or file_hash(target) != remote["sha256"]
                ):
                    raise Conflict("committed media transfer changed")
                return {"verified": True, "duplicate": True, "asset_id": asset_id}
            info = {
                **upload.manifest,
                "asset": {
                    **remote,
                    "asset_id": asset_id,
                    "uri": host.side + "-asset://" + asset_id,
                },
                "remote_asset_id": remote["asset_id"],
            }
            temporary.replace(target)
            db.execute(
                "INSERT INTO assets VALUES (?,?,?,?,?)",
                (
                    asset_id,
                    task["id"],
                    str(target),
                    canonical(info),
                    "capture" if request.payload["operation"] == "capture" else "deliverable",
                ),
            )
            db.execute(
                "INSERT INTO transferred_assets VALUES (?,?,?,?,?,?)",
                (
                    request.id,
                    remote["asset_id"],
                    task["id"],
                    task["version"],
                    upload.principal,
                    remote["sha256"],
                ),
            )
            store._audit(
                db,
                task["id"],
                "asset.transferred",
                {
                    "request_id": request.id,
                    "asset_id": asset_id,
                    "remote_asset_id": remote["asset_id"],
                    "sha256": remote["sha256"],
                    "verified": True,
                    "requested_transfer": True,
                },
            )
        return {"verified": True, "duplicate": False, "asset_id": asset_id}
    finally:
        temporary.unlink(missing_ok=True)
