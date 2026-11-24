"""Local functional API. The node credential is only accepted on private adapter routes."""

import asyncio
import json
import re
import secrets
import threading
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict


from .harness_contracts import (
    AssetTransfer,
    ForwardedInput,
    ForwardedVoice,
    PeerMessage,
    PeerRecordMutation,
    PeerRecordQuery,
    ReportDelivery,
    ReportWindowQuery,
    UserInput,
    VoiceInput,
    VoiceReceiptQuery,
    VoiceReplyDelivery,
    VoiceUtterance,
    WakeConfiguration,
)
from .harness_host import HarnessHost
from .harness_media_contracts import (
    MediaTransfer,
    PeerFrameRead,
    PeerSourceQuery,
    PlaybackControl,
    SourceFrame,
    SourceRegistration,
)
from .harness_voice_notifications import VoiceNoticeReceipt
from .inputs import InputEvent
from .store import Conflict


class RevalidatedStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def versioned_page(web, name, version):
    content = (web / name).read_text()
    content = re.sub(
        r"(/harness-static/[\w.-]+\.(?:js|css))(?:\?[^\"']*)?",
        lambda match: match[1] + "?v=" + version,
        content,
    )
    return content.replace("<html ", '<html data-asset-version="' + version + '" ', 1)


class TestClock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    through: datetime


class TestBarrier(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hold: bool


def create_harness_app(
    profile, root, side, token, *, runtime_factory=None, observation_factory=None
):
    kwargs = {"runtime_factory": runtime_factory} if runtime_factory else {}
    host = HarnessHost(
        profile, root, side, token, observation_factory=observation_factory, **kwargs
    )
    # This local single-user demo derives identity here; neither tools nor
    # client-supplied principal fields may select another user's records.
    principal = "local-user"

    @asynccontextmanager
    async def lifespan(app):
        await host.start()
        try:
            yield
        finally:
            await host.close()

    app = FastAPI(title=f"端云 Harness · {side}", version="0.4.0-dev", lifespan=lifespan)
    app.state.host = host
    home_origins = set()
    if profile.harness.home_access_file:
        from .home_access import HomeAccessConfig

        home_origins = HomeAccessConfig.model_validate_json(
            Path(profile.harness.home_access_file).read_text()
        ).origins

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        allowed = {
            f"http://{name}:{port}"
            for name in ("127.0.0.1", "localhost")
            for port in (profile.cloud_port, profile.edge_port)
        }
        allowed.update(home_origins)
        capture_route = request.url.path.startswith("/harness/capture/")
        if capture_route:
            allowed.update(profile.harness.capture_allowed_origins)
        if origin and origin not in allowed:
            return JSONResponse({"error": "origin rejected"}, status_code=403)
        if request.method == "OPTIONS" and capture_route and origin in allowed:
            return JSONResponse(
                {},
                headers={
                    "Access-Control-Allow-Origin": origin,
                    "Vary": "Origin",
                    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers": "authorization,content-type,x-media-event,x-voice-event",
                },
            )
        response = await call_next(request)
        if capture_route and origin in allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
        return response

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    async def invalid_request(request, exc):
        return JSONResponse(
            {"error": "invalid request or unsupported media specification"}, status_code=422
        )

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"error": "not found"}, status_code=404)

    async def node_auth(request: Request):
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            raise HTTPException(401, "node authentication required")

    from .harness_observation import AnalysisControl

    @app.post("/harness/observation/analysis-control", dependencies=[Depends(node_auth)])
    async def analysis_control(body: AnalysisControl):
        if host.side != "edge" or body.principal != principal:
            raise Conflict("analysis control belongs to the authorized edge principal")
        return await host.observations._installed().gateway.control_analysis(body)

    from .observation_workbench import mount_observation_workbench

    mount_observation_workbench(app, host, principal, node_auth)
    from .harness_input_api import mount_input_api

    mount_input_api(app, host, principal, node_auth)
    from .observation_evidence import EvidenceSearch, PeriodReview

    @app.post("/harness/observation/evidence/search", dependencies=[Depends(node_auth)])
    async def evidence_search(body: EvidenceSearch):
        return host.observation_evidence.search(principal, body)

    @app.post("/harness/observation/evidence/period", dependencies=[Depends(node_auth)])
    async def evidence_period(body: PeriodReview):
        return host.observation_evidence.period(principal, body)

    from .harness_observation import ObservationEnvelope, ObservationMediaQuery, QueryObservation

    from .raw_retention import ConfigureRawRetention
    from .edge_render import EdgeRenderRequest
    from .media_storage import ConfigureMediaStorage, CollectMediaStorage, ProtectMediaFile

    @app.post("/harness/media/storage/protect", dependencies=[Depends(node_auth)])
    async def protect_media_file(body: ProtectMediaFile):
        return host.media_storage.protect(body)

    @app.get("/harness/media/storage", dependencies=[Depends(node_auth)])
    async def read_media_storage():
        from .storage_summary import storage_with_summary
        return await asyncio.to_thread(storage_with_summary, host, principal)

    @app.post("/harness/media/storage/policy", dependencies=[Depends(node_auth)])
    async def configure_media_storage(body: ConfigureMediaStorage):
        return host.media_storage.configure(body)

    @app.post("/harness/media/storage/collect", dependencies=[Depends(node_auth)])
    async def collect_media_storage(body: CollectMediaStorage):
        return await asyncio.to_thread(host.media_storage.collect, body.category)

    @app.post("/harness/media/render", dependencies=[Depends(node_auth)])
    async def render_on_node(body: EdgeRenderRequest, request: Request):
        return await host.edge_renderer.render(principal, body, request.is_disconnected)

    @app.get("/harness/media/render/{job_id}", dependencies=[Depends(node_auth)])
    async def rendered_on_node(job_id: str):
        from starlette.background import BackgroundTask

        path = host.edge_renderer.root / (job_id + ".mp4")
        lease = host.media_storage.pin(path)
        lease.__enter__()
        try:
            _, path = host.edge_renderer.result(principal, job_id)
            return FileResponse(
                path,
                media_type="video/mp4",
                background=BackgroundTask(lease.__exit__, None, None, None),
            )
        except BaseException:
            lease.__exit__(None, None, None)
            raise

    from .observation_candidates import ReviewObservationCandidates

    @app.post("/harness/observation/candidates/review", dependencies=[Depends(node_auth)])
    async def review_candidates(body: ReviewObservationCandidates):
        host.observations._installed()
        return await asyncio.to_thread(
            host.input_media.retention.candidate_reviews.apply, principal, body
        )

    @app.get("/harness/recordings/policy", dependencies=[Depends(node_auth)])
    async def raw_policy_read():
        return {
            **host.input_media.retention.configuration(principal),
            "usage": host.input_media.retention.last_usage,
            "history": await asyncio.to_thread(host.input_media.retention.history, principal),
        }

    @app.post("/harness/recordings/policy", dependencies=[Depends(node_auth)])
    async def raw_policy_apply(body: ConfigureRawRetention):
        if host.side != "edge":
            raise Conflict("recording storage belongs to the edge node")
        return await asyncio.to_thread(host.input_media.retention.configure, principal, body)

    from .observation_defaults import DefaultObservationRequest

    @app.get("/harness/observation/default", dependencies=[Depends(node_auth)])
    async def default_observation_read():
        return host.default_observation.read(principal)

    @app.post("/harness/observation/default", dependencies=[Depends(node_auth)])
    async def default_observation_apply(body: DefaultObservationRequest):
        return await host.default_observation.apply(principal, body)

    @app.get("/harness/observation/catalogue", dependencies=[Depends(node_auth)])
    async def observation_catalogue():
        return host.observations.capabilities()

    @app.post("/harness/observation/commands", dependencies=[Depends(node_auth)])
    async def observation_commands(body: ObservationEnvelope, request: Request):
        from .observation_control_trace import receive_control

        return await receive_control(host, body, request)

    @app.get("/harness/observation/current", dependencies=[Depends(node_auth)])
    async def current_shared_recording():
        from .shared_recording_status import current_recording

        host.observations._installed()
        return await __import__("asyncio").to_thread(current_recording, host.store, principal)

    @app.post("/harness/observation/withdraw", dependencies=[Depends(node_auth)])
    async def observation_withdraw(body: ObservationEnvelope):
        return await host.observations.withdraw(body)

    @app.post("/harness/observation/query", dependencies=[Depends(node_auth)])
    async def observation_query(body: QueryObservation):
        return await asyncio.to_thread(host.observations.local_query, principal, body)

    from .observation_notifications import ObservationUpdates

    @app.post("/harness/observation/updates", dependencies=[Depends(node_auth)])
    async def observation_updates(body: ObservationUpdates):
        return await asyncio.to_thread(host.observations.notifications.receive, body)

    @app.post("/harness/observation/health", dependencies=[Depends(node_auth)])
    async def observation_health(body: QueryObservation):
        def snapshot():
            return {
                "reachable": True,
                "health": host.health.snapshot(principal),
                "health_events": host.health.events(principal, body.health_after),
            }

        return await asyncio.to_thread(snapshot)

    @app.post("/harness/observation/media", dependencies=[Depends(node_auth)])
    async def observation_media(body: ObservationMediaQuery):
        return host.observations.local_media(principal, body)

    from .observation_attachment import ObservationAssetQuery

    @app.post("/harness/observation/asset", dependencies=[Depends(node_auth)])
    async def observation_asset(body: ObservationAssetQuery, request: Request):
        cancelled = threading.Event()

        def check():
            if cancelled.is_set() or host.stop.is_set():
                raise Conflict("requested media operation cancelled")

        work = asyncio.create_task(
            asyncio.to_thread(host.observations.local_asset, principal, body, check_active=check)
        )
        host.control_tasks.add(work)

        async def watch_disconnect():
            while not work.done():
                if await request.is_disconnected():
                    cancelled.set()
                    return
                await asyncio.sleep(0.1)

        monitor = asyncio.create_task(watch_disconnect())
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            cancelled.set()
            with suppress(Exception):
                await asyncio.shield(work)
            raise
        finally:
            monitor.cancel()
            with suppress(asyncio.CancelledError):
                await monitor
            host.control_tasks.discard(work)

    @app.get("/harness/observation/requested-input/{asset_id}", dependencies=[Depends(node_auth)])
    async def requested_input(asset_id: str):
        path = host.requested_observation_media.root / (asset_id + ".mp4")
        lease = host.media_storage.pin(path)
        lease.__enter__()
        try:
            item = await asyncio.to_thread(
                host.requested_observation_media.get, principal, asset_id
            )
            class RequestedInputResponse(FileResponse):
                async def __call__(self, scope, receive, send):
                    try:
                        return await super().__call__(scope, receive, send)
                    finally:
                        lease.__exit__(None, None, None)
                        if getattr(host.store, "shared_media_storage", None):
                            try:
                                host.media_storage.release_temporary(path, "requested-edit-inputs")
                            except Exception as exc:
                                host.health.record(principal, "shared_temporary_cleanup", type(exc).__name__)

            return RequestedInputResponse(
                item["path"],
                media_type="video/mp4",
                headers={"ETag": '"' + item["sha256"] + '"'},
            )
        except BaseException:
            lease.__exit__(None, None, None)
            raise

    @app.get("/harness/observation/receipts/{request_id}", dependencies=[Depends(node_auth)])
    async def observation_receipt(request_id: str):
        return host.observations.receipt(principal, request_id)

    @app.get("/harness/observation/media/{asset_id}", dependencies=[Depends(node_auth)])
    async def observation_file(asset_id: str):
        installed = host.observations._installed()
        if installed.archive is None:
            raise Conflict("observation archive is not installed")
        path = await __import__("asyncio").to_thread(
            installed.archive.media_path, principal, asset_id
        )
        return FileResponse(path, media_type="video/mp4", filename=asset_id + ".mp4")

    @app.get("/health")
    async def health():
        failed = any(w.done() for w in host.workers)
        return JSONResponse(
            {
                "side": side,
                "runtime": getattr(host.runtime, "name", "nanobot"),
                "instance_id": __import__("os").environ.get("CIX_HARNESS_INSTANCE"),
                "profile_digest": profile.fingerprint,
                "source_digest": host.source_digest,
                "host": __import__("platform").node(),
                "simulated_devices": True,
                "functional": True,
                "worker_started": bool(host.workers),
                "worker_failed": failed,
            },
            status_code=503 if failed else 200,
        )

    web = Path(__file__).parent / "web"
    from .harness_component_api import mount_components

    mount_components(app, host, principal, node_auth)
    app.mount("/harness-static", RevalidatedStaticFiles(directory=web), name="harness-static")
    pages = {
        name: versioned_page(web, name, host.source_digest[:16])
        for name in ("harness.html", "capture.html")
    }

    @app.get("/")
    async def home():
        return HTMLResponse(pages["harness.html"], headers={"Cache-Control": "no-store"})

    @app.get("/capture")
    async def capture_client():
        return HTMLResponse(pages["capture.html"], headers={"Cache-Control": "no-store"})

    @app.post("/harness/source-frame", dependencies=[Depends(node_auth)])
    async def source_frame_read(message: PeerFrameRead):
        from .harness_frame_read import local_frame_packet

        return await asyncio.to_thread(local_frame_packet, host, message.principal, message)

    @app.get("/harness/overview")
    async def overview():
        peer = {"online": False}
        try:
            response = await host.client.get("/harness/capabilities", timeout=1)
            response.raise_for_status()
            peer = {"online": True, "binding": response.json()}
        except __import__("httpx").HTTPError:
            pass
        return {
            side: {"online": True, "binding": host.binding},
            host.peer: peer,
            "tasks": host.store.search(principal, "", "tasks", 10)["records"],
            "events": host.store.events_for(principal),
            "assets": host.effects.assets(None, principal),
        }

    @app.get("/harness/trace/{event_id}")
    async def trace(event_id: str):
        event = host.store.event(event_id, principal)
        with host.store.connect() as db:
            rows = db.execute(
                "SELECT seq,kind,payload,created FROM audit WHERE json_extract(payload,'$.event_id')=? ORDER BY seq",
                (event_id,),
            ).fetchall()
        with host.store.connect() as db:
            outgoing = db.execute(
                "SELECT * FROM peer_mail WHERE json_extract(body,'$.payload.origin_event_id')=? OR id=? OR json_extract(body,'$.correlation_id')=?",
                (event_id, event_id, event_id),
            ).fetchall()
            ids = [r["id"] for r in outgoing]
            related = []
            for ident in ids:
                related.extend(
                    db.execute(
                        "SELECT * FROM peer_mail WHERE json_extract(body,'$.correlation_id')=?",
                        (ident,),
                    ).fetchall()
                )
            mail = {
                r["id"]: {
                    **dict(r),
                    "body": json.loads(r["body"]),
                    "result": json.loads(r["result"]) if r["result"] else None,
                }
                for r in [*outgoing, *related]
            }
        return {
            "event": event,
            "trace": [{**dict(r), "payload": host.store.audit_payload(r["payload"])} for r in rows],
            "peer_messages": list(mail.values()),
        }

    @app.get("/harness/assets/{asset_id}")
    async def asset_file(asset_id: str):
        if asset_id not in {a["id"] for a in host.effects.assets(None, principal)}:
            raise HTTPException(404, "asset not found")
        asset = host.store.asset(asset_id)
        from .edge_render import ensure_local_asset
        from starlette.background import BackgroundTask

        lease = host.media_storage.pin(asset["path"])
        lease.__enter__()
        try:
            path = await ensure_local_asset(host, asset)
        except BaseException:
            lease.__exit__(None, None, None)
            raise
        return FileResponse(
            path,
            media_type="video/mp4",
            filename=asset_id + ".mp4",
            background=BackgroundTask(lease.__exit__, None, None, None),
        )

    @app.get("/harness/sources")
    async def sources():
        return {"sources": host.sources.list(principal)}

    @app.post("/harness/sources/query", dependencies=[Depends(node_auth)])
    async def query_sources(body: PeerSourceQuery):
        from .harness_sources import list_available_sources

        return list_available_sources(host, body.principal, body.source_id)

    @app.post("/harness/sources", dependencies=[Depends(node_auth)])
    async def register_source(body: SourceRegistration):
        return await __import__("asyncio").to_thread(host.sources.register, principal, body)

    @app.put("/harness/sources/playback", dependencies=[Depends(node_auth)])
    async def playback(body: PlaybackControl):
        return host.sources.control(principal, body)

    @app.get("/harness/sources/{source_id}/video")
    async def source_video(source_id: str):
        source = host.sources.get(principal, source_id, include_path=True)
        return FileResponse(source["path"])

    @app.get("/harness/sources/{source_id}/frame")
    async def source_frame(source_id: str, position_ms: int = 0):
        value = await __import__("asyncio").to_thread(
            host.sources.frame, principal, SourceFrame(source_id=source_id, position_ms=position_ms)
        )
        return FileResponse(
            value["path"],
            media_type="image/jpeg",
            headers={"X-Frame-Time-Ms": str(value["frame_time_ms"]), "X-Simulated": "true"},
        )

    @app.get("/harness/capabilities")
    async def capabilities():
        return host.capabilities()

    @app.get("/harness/autonomy")
    async def autonomy_config():
        return (
            host.autonomy.read(principal)
            if getattr(host, "autonomy", None)
            else {"available": False, "enabled": False}
        )

    @app.get("/harness/channels/feishu")
    async def feishu_status():
        return (
            host.channels.status()
            if host.channels
            else {"configured": False, "enabled": False, "connected": False}
        )

    @app.post("/harness/channels/feishu/authorize/{sender}", dependencies=[Depends(node_auth)])
    async def feishu_authorize(sender: str):
        if host.channels is None:
            raise HTTPException(409, "Feishu is not configured")
        return host.channels.authorize(sender)

    @app.get("/harness/autonomy/activity")
    async def autonomy_activity():
        from .harness_autonomy_view import activity

        return activity(host.store, principal)

    @app.get("/harness/autonomy/activity/{event_id}")
    async def autonomy_activity_detail(event_id: str):
        from .harness_autonomy_view import activity

        return activity(host.store, principal, event_id)

    @app.post("/harness/autonomy/run")
    async def autonomy_run(request: Request):
        await node_auth(request)
        if not getattr(host, "autonomy", None):
            raise HTTPException(409, "autonomy is not installed")
        return host.autonomy.wake_now(principal)

    from .autonomy_contracts import ConfigureAutonomy

    @app.post("/harness/autonomy")
    async def autonomy_configure(body: ConfigureAutonomy, request: Request):
        await node_auth(request)
        if not getattr(host, "autonomy", None):
            raise HTTPException(409, "autonomy is not installed")
        return host.autonomy.configure(principal, body, "user-interface")

    @app.get("/harness/conversations")
    async def conversations():
        if side == "cloud":
            host.store.conversations.main(principal)
        return {
            "side": side,
            "model": getattr(profile, side).model,
            "conversations": [
                {**record, "read_only": side == "edge" and record["kind"] == "main"}
                for record in host.store.conversations.list(principal)
            ],
        }

    @app.post("/harness/conversations")
    async def new_work(body: UserInput):
        return host.open_work(body, principal)

    @app.get("/harness/conversations/{conversation_id}")
    async def conversation(conversation_id: str, before_seq: int | None = None, limit: int = 30):
        record = host.store.conversations.get(principal, conversation_id)
        record["read_only"] = side == "edge" and record["kind"] == "main"
        if not 1 <= limit <= 100:
            raise HTTPException(422, "limit must be between 1 and 100")
        with host.store.connect() as db:
            rows = db.execute(
                """SELECT rowid AS seq,id,created FROM harness_events WHERE principal=? AND
                (session_key=? OR (session_key IS NULL AND (
                    json_extract(body,'$.conversation_id')=? OR
                    (?='main' AND kind IN ('user','voice') AND json_extract(body,'$.conversation_id') IS NULL) OR
                    (? IS NOT NULL AND task_id=?))))
                AND NOT EXISTS (SELECT 1 FROM conversation_event_archives a
                    WHERE a.event_id=harness_events.id AND a.principal=harness_events.principal
                    AND a.conversation_id=harness_events.session_key)
                AND (? IS NULL OR rowid<?) ORDER BY rowid DESC LIMIT ?""",
                (
                    principal,
                    conversation_id,
                    conversation_id,
                    record["kind"],
                    record["task_id"],
                    record["task_id"],
                    before_seq,
                    before_seq,
                    limit + 1,
                ),
            ).fetchall()
        page = rows[:limit]
        return {
            "conversation": record,
            "events": [
                {**host.store.event(r["id"], principal), "sequence": r["seq"]}
                for r in reversed(page)
            ],
            "next_before_seq": page[-1]["seq"] if len(rows) > limit else None,
        }

    @app.get("/harness/conversations/{conversation_id}/history")
    async def native_history(conversation_id: str):
        host.store.conversations.get(principal, conversation_id)
        if hasattr(host.runtime, "history"):
            return await host.runtime.history(conversation_id)
        session = host.runtime.loop.sessions.get_or_create(conversation_id)
        return {
            "conversation_id": conversation_id,
            "model": getattr(profile, side).model,
            "last_consolidated": session.last_consolidated,
            "summary": session.metadata.get("_last_summary"),
            "messages": [
                {
                    k: v
                    for k, v in m.items()
                    if k not in {"reasoning_content", "reasoning", "thinking"}
                }
                for m in session.messages
            ],
            "boundary": "native saved history, distinct from per-request provider messages",
        }

    @app.post("/harness/input")
    async def user_input(body: UserInput):
        return host.submit_user(body, principal)

    @app.post("/harness/ingress/forward", dependencies=[Depends(node_auth)])
    async def input_forward(body: ForwardedInput):
        return host.voice.forward.accept(body, principal)

    @app.post("/harness/ingress/receipt", dependencies=[Depends(node_auth)])
    async def input_receipt(body: VoiceReceiptQuery):
        return host.voice.forward.lookup(body, principal)

    @app.get("/harness/ingress/events/{event_id}")
    async def input_status(event_id: str):
        return host.voice.status(principal, event_id)

    @app.post("/harness/ingress/delivered", dependencies=[Depends(node_auth)])
    async def input_delivered(body: VoiceReplyDelivery):
        return host.voice.acknowledge_delivery(body, principal)

    @app.get("/harness/voice/config")
    async def voice_config():
        return {
            **host.voice.configuration().model_dump(),
            "acoustic_detector_installed": False,
            "followup_window_is_not_conversation_expiry": True,
        }

    from .voice_device_proxy import mount_voice_device_proxy
    mount_voice_device_proxy(app, host, node_auth)

    from .harness_delivery import DeliveryUpdate

    @app.get("/harness/delivery", dependencies=[Depends(node_auth)])
    async def delivery_settings(task_id: str | None = None):
        return host.store.delivery.read(principal, task_id)

    @app.put("/harness/delivery", dependencies=[Depends(node_auth)])
    async def delivery_configure(body: DeliveryUpdate):
        if side != "cloud":
            raise HTTPException(409, "用户汇报配置由主服务管理")
        return host.store.delivery.configure(principal, body, source="owner_settings_page")

    @app.put("/harness/voice/config", dependencies=[Depends(node_auth)])
    async def voice_configure(body: WakeConfiguration):
        return host.voice.configure(body)

    @app.post("/harness/voice/mock", dependencies=[Depends(node_auth)])
    async def voice_wake_mock(body: VoiceUtterance):
        return host.voice.ingest(body, principal)

    @app.post("/harness/voice/file-asr", dependencies=[Depends(node_auth)])
    async def voice_file_asr(body: VoiceUtterance):
        return host.voice.ingest(body, principal, adapter="file-asr")

    @app.post("/harness/voice/streaming-asr", dependencies=[Depends(node_auth)])
    async def voice_stream_asr(body: VoiceUtterance):
        return host.voice.ingest(body, principal, adapter="streaming-asr")

    @app.get("/harness/voice/streaming-asr/config", dependencies=[Depends(node_auth)])
    async def voice_stream_config():
        return {
            "available": host.side == "edge" and "inputs" in host.binding["capabilities"],
            "sources": host.settings.voice_stream_sources,
            "activation": "asr_text_gate",
            "wake_words": host.voice.configuration().wake_words,
        }

    @app.post("/harness/voice/forward", dependencies=[Depends(node_auth)])
    async def voice_forward(body: ForwardedVoice):
        return host.voice.forward.accept(body, principal)

    @app.post("/harness/voice/receipt", dependencies=[Depends(node_auth)])
    async def voice_receipt(body: VoiceReceiptQuery):
        return host.voice.forward.lookup(body, principal)

    @app.get("/harness/voice/audio/{event_id}/status", dependencies=[Depends(node_auth)])
    async def voice_audio_status(event_id: str):
        return host.voice_audio.status(principal, event_id)

    @app.get("/harness/voice/audio/{event_id}", dependencies=[Depends(node_auth)])
    async def voice_audio_file(event_id: str):
        from fastapi.responses import FileResponse
        from starlette.background import BackgroundTask
        from .contracts import digest

        path = host.voice_audio.root / (digest([principal, event_id]) + ".wav")
        lease = host.media_storage.pin(path)
        lease.__enter__()
        try:
            return FileResponse(
                host.voice_audio.path(principal, event_id),
                media_type="audio/wav",
                background=BackgroundTask(lease.__exit__, None, None, None),
            )
        except BaseException:
            lease.__exit__(None, None, None)
            raise

    @app.get("/harness/voice/events/{event_id}")
    async def voice_status(event_id: str):
        return host.voice.status(principal, event_id)

    @app.get("/harness/voice/notifications/pending", dependencies=[Depends(node_auth)])
    async def voice_notifications_pending(after_sequence: int = 0):
        return host.voice.notifications.pending_cloud(principal, after_sequence=after_sequence)

    @app.post("/harness/voice/notifications/received", dependencies=[Depends(node_auth)])
    async def voice_notifications_received(body: VoiceNoticeReceipt):
        return host.voice.notifications.received_cloud(principal, body)

    @app.get("/harness/voice/notifications", dependencies=[Depends(node_auth)])
    async def voice_notifications_local(origin_event_id: str | None = None):
        return host.voice.notifications.local(principal, origin_event_id=origin_event_id)

    @app.post("/harness/voice/delivered", dependencies=[Depends(node_auth)])
    async def voice_delivered(body: VoiceReplyDelivery):
        return host.voice.acknowledge_delivery(body, principal)

    @app.post("/harness/mock/voice", dependencies=[Depends(node_auth)])
    async def voice(body: VoiceInput):
        if side != "edge":
            raise HTTPException(404, "voice adapter is edge-owned")
        if (
            not body.simulated
            or body.mode != "interact"
            or body.task_id
            or body.conversation_id
            or body.valid_for_ms != 60000
        ):
            raise Conflict(
                "legacy voice options cannot be promoted to cloud user authorization; use the wake-gated utterance route"
            )
        return host.voice.ingest(
            VoiceUtterance(
                event_id=body.event_id,
                transcript=body.text,
                received_at=body.observed_at,
                source_ref=body.source_ref,
                purpose=body.purpose,
            ),
            principal,
        )

    @app.post("/harness/mock/media-transcript", dependencies=[Depends(node_auth)])
    async def media_transcript(body: VoiceInput):
        if side != "edge":
            raise HTTPException(404, "media input is edge-owned")
        body.purpose = "media_content"  # The installed source, not text, fixes authority.
        return host.submit_user(body, principal, channel="voice", source="media")

    @app.get("/harness/rules")
    async def wake_rules():
        return {"rules": host.store.scheduler.list(principal)}

    @app.get("/harness/reports")
    async def reports():
        return {"deliveries": host.reports.list(principal)}

    @app.post("/harness/reports/period", dependencies=[Depends(node_auth)])
    async def report_period(body: ReportWindowQuery):
        return host.reports.window_records(
            body.principal, body.start.timestamp(), body.end.timestamp()
        )

    @app.post("/harness/reports/receive", dependencies=[Depends(node_auth)])
    async def report_receive(body: ReportDelivery):
        return host.reports.receive(body.model_dump(mode="json", exclude_unset=True))

    @app.get("/harness/events")
    async def events():
        return {"events": host.store.events_for(principal)}

    @app.get("/harness/events/{event_id}")
    async def event(event_id: str):
        return host.store.event(event_id, principal)

    @app.get("/harness/tasks/{task_id}")
    async def task_detail(task_id: str):
        task = host.store.business_task(task_id, principal)
        with host.store.connect() as db:
            jobs = [
                {**dict(r), "result": json.loads(r["result"]) if r["result"] else None}
                for r in db.execute(
                    "SELECT * FROM harness_jobs WHERE task_id=? ORDER BY created", (task_id,)
                )
            ]
            for job in jobs:
                plan = db.execute(
                    "SELECT plan FROM harness_job_plans WHERE job_id=?", (job["id"],)
                ).fetchone()
                job["frozen_plan"] = json.loads(plan["plan"]) if plan else None
            actions = [
                {**dict(r), "result": json.loads(r["result"])}
                for r in db.execute(
                    "SELECT a.* FROM actions a JOIN harness_jobs j ON j.id=a.task_id WHERE j.task_id=? ORDER BY a.created",
                    (task_id,),
                )
            ]
            timeline = [
                {**dict(r), "payload": host.store.audit_payload(r["payload"])}
                for r in db.execute(
                    "SELECT * FROM audit WHERE task_id=? OR task_id IN (SELECT id FROM harness_jobs WHERE task_id=?) ORDER BY seq",
                    (task_id, task_id),
                )
            ]
        return {"task": task, "jobs": jobs, "actions": actions, "timeline": timeline}

    @app.get("/harness/records")
    async def records(
        query: str = "", collection: str = "all", limit: int = 5, include_generated: bool = False
    ):
        if collection not in {"all", "tasks", "history", "memory"} or not 1 <= limit <= 10:
            raise HTTPException(422, "invalid search scope")
        return host.store.search(
            principal, query, collection, limit, include_generated=include_generated
        )

    @app.post("/harness/peer", dependencies=[Depends(node_auth)])
    async def peer(body: PeerMessage):
        return host.receive_peer(body)

    @app.post("/harness/records/query", dependencies=[Depends(node_auth)])
    async def peer_records(body: PeerRecordQuery):
        return host.peer_records.query(body)

    @app.post("/harness/records/update", dependencies=[Depends(node_auth)])
    async def peer_update(body: PeerRecordMutation):
        return host.peer_records.apply(body)

    @app.post("/harness/media/receive", dependencies=[Depends(node_auth)])
    async def transfer_media(body: MediaTransfer):
        from .harness_media_transfer import receive_media

        return await __import__("asyncio").to_thread(receive_media, host, body)

    @app.post("/harness/assets/receive", dependencies=[Depends(node_auth)])
    async def transfer(body: AssetTransfer):
        # Hash/decode work must not block the event loop or urgent controls.
        return await __import__("asyncio").to_thread(host.mail.transfer, body)

    @app.post("/harness/observations", dependencies=[Depends(node_auth)])
    async def observations(body: list[InputEvent]):
        if not body or len(body) > 10:
            raise HTTPException(422, "one to ten observations required")
        return {"results": host.ingest_observations(body)}

    @app.post("/harness/test/clock", dependencies=[Depends(node_auth)])
    async def test_clock(body: TestClock):
        if not host.settings.test_controls:
            raise HTTPException(404, "test controls disabled")
        if body.through.tzinfo is None:
            raise HTTPException(422, "timezone required")
        events = host.store.due_timers(now=body.through.timestamp())
        host.store.audit(
            None,
            "test.scheduler.advance",
            {"through": body.through.isoformat(), "event_count": len(events), "simulated": True},
        )
        return {"events": events, "simulated_clock": True}

    @app.post("/harness/test/barrier", dependencies=[Depends(node_auth)])
    async def test_barrier(body: TestBarrier):
        if not host.settings.test_controls:
            raise HTTPException(404, "test controls disabled")
        host.store.set_meta("test_commit_hold", body.hold)
        if body.hold:
            host.store.set_meta("test_barrier_reached", None)
        return {"hold": body.hold, "test_only": True}

    @app.get("/harness/test/barrier", dependencies=[Depends(node_auth)])
    async def barrier_status():
        if not host.settings.test_controls:
            raise HTTPException(404, "test controls disabled")
        return {
            "hold": host.store.meta("test_commit_hold", False),
            "reached": host.store.meta("test_barrier_reached"),
        }

    @app.post("/harness/test/clear-sessions", dependencies=[Depends(node_auth)])
    async def clear_sessions():
        if not host.settings.test_controls:
            raise HTTPException(404, "test controls disabled")
        return await host.runtime.clear_test_sessions(principal)

    @app.get("/harness/test/status", dependencies=[Depends(node_auth)])
    async def test_status():
        if not host.settings.test_controls:
            raise HTTPException(404, "test controls disabled")
        with host.store.connect() as db:
            events = [
                dict(r)
                for r in db.execute(
                    "SELECT status,COUNT(*) AS count FROM harness_events GROUP BY status"
                )
            ]
            jobs = [
                dict(r)
                for r in db.execute(
                    "SELECT status,COUNT(*) AS count FROM harness_jobs GROUP BY status"
                )
            ]
        return {
            "events": events,
            "jobs": jobs,
            "model_busy": getattr(host.runtime, "event", None) is not None,
            "workers": [{"done": w.done(), "cancelled": w.cancelled()} for w in host.workers],
        }

    from .harness_workbench import mount_workbench

    mount_workbench(app, host, principal, token, node_auth)
    return app
