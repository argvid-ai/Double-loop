"""Independent board voice adapter: no recorder ownership and no agent loop."""

import argparse
import asyncio
import fcntl
import os
import secrets
import shutil
import socket
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import Field, field_validator

from .contracts import Contract, InteractionSettings
from .harness_contracts import VoiceReplyDelivery, VoiceUtterance, WakeConfiguration
from .dual_store import DualStore
from .harness_voice import VoiceGateway
from .store import Conflict
from .streaming_asr import StreamingRecognizerConfig
from .voice_device_audio import BoardPlayback
from .voice_device_input import LiveVoiceInput


class VoiceDeviceConfig(Contract):
    state: str
    edge_state: str
    edge_url: str
    cloud_url: str
    edge_token_file: str
    cloud_token_file: str
    token_file: str
    principal: str = "local-user"
    recording_source: str
    interaction_source: str = "board-microphone"
    recognizer: StreamingRecognizerConfig
    port: int = Field(default=9193, ge=1024, le=65535)
    max_input_lag_seconds: int = Field(default=15, ge=1, le=30)
    reply_max_age_seconds: int = Field(default=60, ge=10, le=300)

    @field_validator("edge_url", "cloud_url")
    @classmethod
    def local_endpoint(cls, value):
        return InteractionSettings.loopback(value)

    @field_validator("state", "edge_state", "edge_token_file", "cloud_token_file", "token_file")
    @classmethod
    def absolute(cls, value):
        if not Path(value).is_absolute():
            raise ValueError("voice paths must be absolute")
        return value


class VoiceDevicePreferences(Contract):
    enabled: bool = True
    output_device: str = Field(default="", max_length=240)
    volume: float = Field(default=0.35, ge=0, le=1, allow_inf_nan=False)


class VoiceDeviceSettings(WakeConfiguration, VoiceDevicePreferences):
    pass


class VoiceScenario(Contract):
    event_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,159}$")
    text: str = Field(min_length=1, max_length=500)


class VoiceDeviceStore(DualStore):
    """Reuse voice receipts without instantiating a scheduler or an agent."""

    def __init__(self, root):
        super().__init__(root, "edge")
        self.clock = time.time
        with self.connect() as db:
            # VoiceGateway checks this namespace for old local input identities;
            # this adapter never creates or executes local agent events.
            db.execute("""CREATE TABLE IF NOT EXISTS harness_events (
                id TEXT PRIMARY KEY, principal TEXT NOT NULL, body TEXT NOT NULL,
                status TEXT NOT NULL)""")


class VoiceDeviceRuntime:
    def __init__(self, config, *, store=None, client=None, edge=None):
        self.config = config
        self.store = store or VoiceDeviceStore(Path(config.state))
        self.side = "edge"
        self.binding = {"capabilities": ["inputs"]}
        self.settings = SimpleNamespace(voice_stream_sources={config.interaction_source: "microphone"})
        self.voice_sources = {"mock-user": (True, "user_command")}
        self.client = client or httpx.AsyncClient(
            base_url=config.cloud_url, trust_env=False, timeout=5,
            headers={"Authorization": "Bearer " + Path(config.cloud_token_file).read_text().strip()},
        )
        self.edge = edge or httpx.AsyncClient(
            base_url=config.edge_url, trust_env=False, timeout=5,
            headers={"Authorization": "Bearer " + Path(config.edge_token_file).read_text().strip()},
        )
        self.voice = VoiceGateway(self)
        self.configuration_ready = self.store.meta("voice_wake_config") is not None
        self.prefs = VoiceDevicePreferences.model_validate(
            self.store.meta("board_voice_preferences", {})
        ).model_dump()
        self.wake_words = self.voice.configuration().wake_words
        self.stopping, self.speaking = threading.Event(), threading.Event()
        self.listen_after = 0
        self.sync_error = "configuration_not_loaded"
        self.errors = {}
        self.synthesis = {"status": "not_used_yet"}
        self.checked_at = time.time()
        self.cache = self.store.root / "playback-temporary"
        self.cache.mkdir(exist_ok=True)
        # Only this adapter's ephemeral reply directories, under the held lease.
        for child in self.cache.glob("reply-*"):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
        self.playback = BoardPlayback(self)
        self.input = LiveVoiceInput(self)
        self.revision_lock = asyncio.Lock()

    def preferences(self):
        return self.prefs

    async def sync_configuration(self):
        async with self.revision_lock:
            try:
                response = await self.edge.get("/harness/voice/config")
                response.raise_for_status()
                body = response.json()
                config = WakeConfiguration(wake_words=body["wake_words"], followup_seconds=body["followup_seconds"])
                if config != self.voice.configuration() or not self.configuration_ready:
                    self.voice.configure(config)
                self.wake_words = config.wake_words
                self.configuration_ready = True
                self.sync_error = None
            except (httpx.HTTPError, ValueError, KeyError):
                self.sync_error = "wake_configuration_unavailable"

    def configuration(self):
        return {**self.prefs, **self.voice.configuration().model_dump()}

    async def configure(self, config):
        async with self.revision_lock:
            # Existing edge settings remain the single owner of the word list.
            requested = WakeConfiguration(**config.model_dump(include={"wake_words", "followup_seconds"}))
            if requested != self.voice.configuration() or not self.configuration_ready:
                response = await self.edge.put("/harness/voice/config", json=requested.model_dump())
                response.raise_for_status()
                actual = WakeConfiguration.model_validate(response.json())
                self.voice.configure(actual)
                self.wake_words = actual.wake_words
                self.configuration_ready = True
                self.sync_error = None
            # Local volume/enabled changes do not need another service to save
            # an unchanged wake-word list.
            self.prefs = VoiceDevicePreferences.model_validate(config.model_dump(include={"enabled", "output_device", "volume"})).model_dump()
            self.store.set_meta("board_voice_preferences", self.prefs)
            return self.configuration()

    def should_admit(self, text):
        if VoiceGateway.match(text, self.wake_words)[0]:
            return True
        with self.store.connect() as db:
            if db.execute("SELECT 1 FROM voice_windows WHERE principal=? AND until>?",
                          (self.config.principal, self.store.clock())).fetchone():
                return True
            return bool(db.execute("""SELECT 1 FROM voice_ingress WHERE principal=?
                AND adapter='streaming-asr' AND json_extract(body,'$.source_ref')=?
                AND json_extract(decision,'$.collect_until')>?
                AND json_extract(decision,'$.collect_consumed')=0 LIMIT 1""",
                (self.config.principal, self.config.interaction_source, self.store.clock())).fetchone())

    def admit_live(self, utterance):
        if not self.input.ready_to_listen():
            return None
        return self.voice.ingest(utterance, self.config.principal, adapter="streaming-asr")

    def simulate(self, value):
        if not self.configuration_ready:
            raise Conflict("wake settings are unavailable")
        # Keep retries identical: receipt time is assigned once, server side.
        with self.store.connect() as db:
            old = db.execute("SELECT body FROM voice_ingress WHERE id=?", (value.event_id,)).fetchone()
        utterance = VoiceUtterance.model_validate_json(old[0]) if old else VoiceUtterance(
            event_id=value.event_id, transcript=value.text, source_ref="board-web-simulation",
            received_at=datetime.now(timezone.utc),
            asr={"source_simulated": True, "recognition_simulated": True, "scenario_read_only": True},
        )
        if utterance.transcript != value.text or utterance.source_ref != "board-web-simulation":
            raise Conflict("simulation identity mismatch")
        return self.voice.ingest(utterance, self.config.principal)

    def event(self, ident):
        status = self.voice.status(self.config.principal, ident)
        return {**status, "playback": self.playback.item(ident)}

    def status(self):
        output_state = ("ready" if self.playback.selected else "choose_output"
                        if len(self.playback.devices) > 1 else "waiting_speaker")
        with self.store.connect() as db:
            latest = db.execute("SELECT id FROM voice_ingress ORDER BY rowid DESC LIMIT 1").fetchone()
        return {
            "checked_at": time.time(), "service_tick": self.checked_at,
            "configuration": self.configuration(),
            "input": dict(self.input.state), "configuration_error": self.sync_error,
            "configuration_ready": self.configuration_ready,
            "outputs": self.playback.devices, "output_state": output_state,
            "output_error": self.playback.probe_error, "playing": self.speaking.is_set(),
            "last_event": self.event(latest[0]) if latest else None,
            "errors": dict(self.errors), "source_ref": self.config.recording_source,
            "synthesis": dict(self.synthesis),
            "physical_playback_verified": False,
            "mode": "half_duplex_shared_recording",
        }

    async def refresh_synthesis(self):
        try:
            with self.store.connect() as db:
                row = db.execute("""SELECT id FROM voice_forward_outbox
                    WHERE reply IS NOT NULL ORDER BY rowid DESC LIMIT 1""").fetchone()
            if row:
                response = await self.client.get(f"/harness/voice/audio/{row[0]}/status")
                response.raise_for_status()
                value = response.json()
                self.synthesis = {k: value.get(k) for k in
                                  ("status", "audio_generated", "duration_ms", "error")}
            self.errors.pop("synthesis_status", None)
        except Exception as exc:
            self.errors["synthesis_status"] = type(exc).__name__
            self.synthesis = {"status": "unavailable", "audio_generated": None}

    async def watch(self):
        while not self.stopping.is_set():
            self.checked_at = time.time()
            await asyncio.gather(
                self.sync_configuration(), self.playback.probe(), self.refresh_synthesis()
            )
            # Independent systemd watchdog also covers a stuck polling loop.
            # A live recognition thread must keep making fresh audio progress.
            stamp = self.input.state.get("last_audio_at")
            stalled = self.input.state["state"] == "listening" and (
                not stamp or time.time() - stamp > 30)
            address = os.environ.get("NOTIFY_SOCKET")
            if address and not stalled:
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as notifier:
                        notifier.sendto(b"WATCHDOG=1", "\0" + address[1:] if address.startswith("@") else address)
                except OSError:
                    self.errors["watchdog"] = "notification_unavailable"
            await asyncio.sleep(2)

    async def pump(self, key, operation):
        while not self.stopping.is_set():
            try:
                await operation()
                self.errors.pop(key, None)
            except Exception as exc:
                self.errors[key] = type(exc).__name__
            await asyncio.sleep(0.5)


def create_voice_device_app(config, *, runtime=None):
    token = Path(config.token_file).read_text().strip()
    if not token:
        raise ValueError("missing voice device credential")

    @asynccontextmanager
    async def lifespan(app):
        app.state.lease = (Path(config.state) / "adapter.lock").open("a")
        try:
            fcntl.flock(app.state.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            host = runtime or VoiceDeviceRuntime(config)
            app.state.runtime = host
            await host.sync_configuration()
            thread = threading.Thread(target=host.input.run, name="shared-voice-input", daemon=True)
            thread.start()
            tasks = [asyncio.create_task(host.watch()),
                     asyncio.create_task(host.pump("cloud_delivery", host.voice.forward.deliver_once)),
                     asyncio.create_task(host.pump("playback", host.playback.tick))]
            try:
                yield
            finally:
                host.stopping.set()
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await asyncio.to_thread(thread.join, 8)
                await host.client.aclose()
                await host.edge.aclose()
        finally:
            app.state.lease.close()

    Path(config.state).mkdir(parents=True, exist_ok=True)
    app = FastAPI(lifespan=lifespan)
    if runtime:
        app.state.runtime = runtime

    async def auth(request: Request):
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            raise HTTPException(401, "node authentication required")

    @app.get("/status", dependencies=[Depends(auth)])
    async def status():
        return app.state.runtime.status()

    @app.put("/config", dependencies=[Depends(auth)])
    async def configure(body: VoiceDeviceSettings):
        return await app.state.runtime.configure(body)

    @app.post("/simulate", dependencies=[Depends(auth)])
    async def simulate(body: VoiceScenario):
        return app.state.runtime.simulate(body)

    @app.get("/events/{ident}", dependencies=[Depends(auth)])
    async def event(ident: str):
        try:
            return app.state.runtime.event(ident)
        except KeyError:
            raise HTTPException(404, "unknown voice event") from None

    @app.post("/displayed", dependencies=[Depends(auth)])
    async def displayed(body: VoiceReplyDelivery):
        # An owner browser confirms the actual displayed text, never a speaker.
        return app.state.runtime.voice.acknowledge_delivery(body, config.principal)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": str(exc)}, status_code=409)

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = VoiceDeviceConfig.model_validate_json(args.config.read_text())
    uvicorn.run(create_voice_device_app(config), host="127.0.0.1", port=config.port, log_level="warning")


if __name__ == "__main__":
    main()
