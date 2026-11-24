"""User-facing observation controls; no caller-selected principal or host paths."""

import asyncio

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse

from .browser_video import BrowserVideoCache
from .harness_observation import (
    ConfigureObservation,
    ControlObservation,
    ObservationEnvelope,
    ObservationMarkerQuery,
    ObservationMediaQuery,
    QueryObservation,
)
from .observation_contracts import Reference
from .observation_tools import ManualCameraControl


class ConfigureFromPage(ConfigureObservation):
    request_id: Reference


class ControlFromPage(ControlObservation):
    request_id: Reference


def managed_previews(host, principal):
    sources, files = [], []
    for registered in host.input_media.list(principal):
        spec = registered["spec"]
        binding = spec["binding"]
        ref = binding["source_ref"]
        source = {
            "id": ref,
            "title": spec["title"],
            "device_ref": binding["device_ref"],
            "managed_input": True,
            "simulated": binding["simulated"],
            "modalities": binding["modalities"],
            "kind": binding["kind"],
        }
        with host.store.connect() as db:
            last = db.execute(
                "SELECT MAX(sequence) FROM managed_input_entries WHERE principal=? AND source_ref=?",
                (principal, ref),
            ).fetchone()[0]
        entries = host.input_media.entries(principal, ref, max(-1, (last or 0) - 200), 200)
        current = []
        for item in entries:
            if item["kind"] != "media":
                continue
            media_ref = item["media_ref"]
            base = "input-media/media/" + media_ref
            visual = "image" in item["facts"]["modalities"]
            sealed = item.get("recording_state") != "growing"
            current.append(
                {
                    **source,
                    "id": media_ref,
                    "source_ref": ref,
                    "media_ref": media_ref,
                    "recorded_start_at": item.get("recorded_start_at"),
                    "availability": item.get("availability", {"state": "available"}),
                    "recording_state": item.get("recording_state", "sealed"),
                    "start_ms": item["start_ms"],
                    "end_ms": item["end_ms"],
                    "modalities": item["facts"]["modalities"],
                    "url": base,
                    "preview_url": (base + "/preview" if visual else base) if sealed else None,
                    "poster_url": base + "/poster"
                    if visual and sealed
                    and item.get("availability", {}).get("state", "available") == "available"
                    else None,
                    "preview_start_ms": item["start_ms"],
                }
            )
        files.extend(current)
        available = [
            item for item in current
            if item["availability"]["state"] == "available"
            and item["recording_state"] != "growing"
        ]
        sources.append(
            {
                **source,
                **(
                    {**available[-1], "id": ref}
                    if available
                    else {"url": None, "unavailable_reason": "waiting_for_sealed_media"}
                ),
                "history_truncated": bool(last and last >= 200),
            }
        )
    return sources, files


def mount_observation_workbench(app, host, principal, node_auth):
    previews = BrowserVideoCache(
        host.store.root, budget_bytes=host.settings.browser_preview_budget_bytes,
        shared_storage=getattr(host.store, "shared_media_storage", None),
        shutdown=host.stop,
    )

    @app.get("/harness/observation/view", dependencies=[Depends(node_auth)])
    async def view():
        capabilities = await asyncio.to_thread(host.observations.capabilities)
        managed, media_inputs = await asyncio.to_thread(managed_previews, host, principal)
        if not capabilities["installed"]:
            return {
                "installed": False,
                "tasks": [],
                "sources": managed,
                "media_inputs": media_inputs,
                "gimbals": [],
                "models": [],
                "catalogue": None,
                "model_admitted": False,
            }
        state = await asyncio.to_thread(
            host.observations.local_query, principal, QueryObservation(limit=100)
        )
        catalogue = capabilities.get("catalogue")
        sources = list(managed)
        if catalogue:
            for source in catalogue["sources"]:
                if source.get("managed_input"):
                    continue
                base = "observation/sources/" + source["id"]
                sources.append(
                    {
                        **source,
                        "url": base + "/video",
                        "preview_url": base + "/preview",
                        "poster_url": base + "/poster",
                        "preview_start_ms": source.get("start_ms", 0),
                    }
                )
        tasks = []
        for runtime in state["tasks"]:
            task = runtime["task"]
            config = task["config"]
            ledger = task["ledger"]
            latest = ledger["latest_ingested"]
            tasks.append(
                {
                    "task_id": task["task_id"],
                    "version": task["version"],
                    "generation": task["generation"],
                    "goal": config["goal"],
                    "source_ref": config["source_ref"],
                    "model_ref": config["model_preset_ref"],
                    "executor_ref": config.get("executor_ref"),
                    "replay_mode": "non_realtime" if config.get("executor_ref") else "realtime",
                    "output_mode": config.get("output_mode", "retained_media"),
                    "marker_summary": runtime.get("marker_summary"),
                    "status": task["status"],
                    "reason": task["reason"],
                    "worker_active": runtime["worker_active"],
                    "shutdown_confirmed": bool((runtime["shutdown"] or {}).get("confirmed"))
                    or task["generation"] == 0,
                    "unprocessed_seconds": ledger["unseen_media_ms"] / 1000,
                    "gap_count": ledger["unresolved_gap_count"],
                    "source_simulated": runtime.get("source_simulated"),
                    "inference_simulated": runtime.get("inference_simulated"),
                    "preview_position_ms": latest["end_ms"]
                    if latest and latest["kind"] == "batch"
                    else None,
                    "retention": runtime.get("retention"),
                    "health": [
                        r
                        for r in state.get("health", [])
                        if (r.get("active") or r.get("code") in {"paused", "restarting"})
                        and r.get("task_id") in {None, task["task_id"]}
                    ],
                }
            )
        gimbals = []
        driver = host.observations.installation.gimbal
        if driver is not None:
            with host.store.connect() as db:
                devices = [
                    row[0]
                    for row in db.execute(
                        "SELECT id FROM observation_gimbals WHERE principal=? ORDER BY id",
                        (principal,),
                    )
                ]
            for device in devices:
                pose = driver.state(principal, device)
                gimbals.append(
                    {
                        key: pose[key]
                        for key in (
                            "id",
                            "pan",
                            "tilt",
                            "revision",
                            "manual",
                            "spec",
                            "simulated",
                            "affects_video",
                        )
                    }
                )
                gimbals[-1]["automatic_active"] = pose["lease"] is not None
        probe = host.observations.installation.model_status
        try:
            models = await probe(principal) if probe else []
        except Exception:
            models = [
                {
                    "id": item["id"],
                    "title": item["title"],
                    "state": "unconfirmed",
                    "context_ready": False,
                    "admitted": False,
                }
                for item in (catalogue or {}).get("models", [])
            ]
        media = []
        if catalogue and host.observations.installation.archive is not None:
            projects = {value["project_id"] for value in catalogue.get("storage", {}).values()}
            for project in sorted(projects):
                media.extend(
                    host.observations.local_media(
                        principal, ObservationMediaQuery(project_id=project)
                    )["media"]
                )
        return {
            "installed": True,
            "tasks": tasks,
            "sources": sources,
            "media_inputs": media_inputs,
            "gimbals": gimbals,
            "models": models,
            "catalogue": catalogue,
            "model_admitted": False,
            "media": media,
            "media_page_limit": 200,
            "media_history_endpoint": "input-media/sources/{source_ref}/entries",
        }

    @app.get("/harness/observation/markers", dependencies=[Depends(node_auth)])
    async def markers(
        task_id: str, after_sequence: int = -1, limit: int = 100, decision: str | None = None
    ):
        return host.observations.local_markers(
            principal,
            ObservationMarkerQuery(
                task_id=task_id, after_sequence=after_sequence, limit=limit, decision=decision
            ),
        )

    @app.get(
        "/harness/observation/markers/{task_id}/{marker_id}/frame",
        dependencies=[Depends(node_auth)],
    )
    def marker_frame(task_id: str, marker_id: str, index: int = 0):
        from fastapi.responses import Response

        # FastAPI runs sync routes in its bounded worker pool; decoding a reread
        # must not block capture/status/control on the service's event loop.
        return Response(
            host.observations.marker_frame(principal, task_id, marker_id, index),
            media_type="image/jpeg",
        )

    @app.post("/harness/observation/gimbal", dependencies=[Depends(node_auth)])
    async def manual_camera(body: ManualCameraControl):
        installed = host.observations._installed()
        if installed.gimbal is None:
            raise HTTPException(409, "manual gimbal control is not installed")
        return installed.gimbal.manual_command(principal, body)

    @app.get(
        "/harness/observation/gimbals/{device}/receipts/{request_id}",
        dependencies=[Depends(node_auth)],
    )
    async def manual_camera_receipt(device: str, request_id: str):
        installed = host.observations._installed()
        if installed.gimbal is None:
            raise HTTPException(409, "manual gimbal control is not installed")
        return installed.gimbal.manual_receipt(principal, device, request_id)

    @app.post("/harness/observation/configure", dependencies=[Depends(node_auth)])
    async def configure(body: ConfigureFromPage):
        return await host.observations.receive(
            ObservationEnvelope(
                request_id=body.request_id,
                principal=principal,
                operation="configure",
                configuration=ConfigureObservation.model_validate(
                    body.model_dump(exclude={"request_id"})
                ),
            )
        )

    @app.post("/harness/observation/control", dependencies=[Depends(node_auth)])
    async def control(body: ControlFromPage):
        return await host.observations.receive(
            ObservationEnvelope(
                request_id=body.request_id,
                principal=principal,
                operation="control",
                control=ControlObservation.model_validate(body.model_dump(exclude={"request_id"})),
            )
        )

    @app.get("/harness/observation/sources/{source_id}/video", dependencies=[Depends(node_auth)])
    async def source_video(source_id: str):
        if host.observation_registry is None:
            raise HTTPException(409, "observation sources are not configured")
        path = host.observation_registry.source_path(principal, source_id)
        return FileResponse(path)

    @app.get("/harness/observation/sources/{source_id}/{kind}", dependencies=[Depends(node_auth)])
    async def source_preview(source_id: str, kind: str):
        if kind not in {"preview", "poster"}:
            raise HTTPException(404, "preview not available")
        registry = host.observation_registry
        if registry is None:
            raise HTTPException(409, "observation sources are not configured")
        path = registry.source_path(principal, source_id)
        spec = registry.config.sources[source_id]
        result = await previews.registered_range(
            path, spec.start_ms, spec.end_ms, "poster" if kind == "poster" else "video"
        )
        return previews.response(result, kind)
