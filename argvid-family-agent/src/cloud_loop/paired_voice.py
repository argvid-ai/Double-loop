"""Explicit paired push-to-talk -> actual audio bytes -> ASR -> existing main.

Observed media never calls this path. Interaction requires a separately scoped
device key; client JSON cannot grant that role. No acoustic wake is implied.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib

import httpx
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException, Request
from pydantic import Field, TypeAdapter

from .contracts import Contract, canonical
from .harness_contracts import VoiceReplyDelivery, VoiceUtterance
from .harness_input_store import probe_input
from .media import ffmpeg, file_hash
from .media_semantic_provider import MediaProviderConnection, MediaProviderSpec
from .observation_contracts import Reference
from .observation_media import _fingerprint
from .store import Conflict


class TranscriptionResult(Contract):
    request_id: Reference
    model: str = Field(min_length=1, max_length=160)
    audio_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    transcript: str = Field(min_length=1, max_length=8000)
    simulated: bool = Field(strict=True)
    model_revision: str | None = Field(default=None, max_length=160)


class PairedVoiceGateway:
    def __init__(self, host):
        self.host, self.store = host, host.store
        self.root = self.store.root.resolve() / "paired-voice"
        self.locks = {}
        self.provider = None
        self.provider_guard = None
        self.connection = None
        self.provider_lock = asyncio.Lock()
        self.local_client = None
        local = host.settings.voice_local_asr_config_file
        path = host.settings.voice_asr_provider_file
        if local and path:
            raise ValueError("choose one installed speech backend, local or external")
        if local:
            from .speech_provider import create_speech_provider
            from .streaming_asr import StreamingRecognizerConfig, checked_model

            config_path = Path(local).resolve(strict=True)
            if config_path.stat().st_size > 65536:
                raise ValueError("local speech configuration exceeds bound")
            config = StreamingRecognizerConfig.model_validate_json(config_path.read_text())
            model, _, _ = checked_model(config)
            app = create_speech_provider(
                config.model_dir,
                self.root / "local-asr",
                backend="zipformer",
                recognizer_config=config.model_dump(),
            )
            self.local_client = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://127.0.0.1",
                trust_env=False,
                timeout=180,
            )
            self.provider = MediaProviderSpec(
                endpoint="http://127.0.0.1",
                model=model,
                modalities=("audio",),
                capabilities=("transcription",),
                simulated=False,
                timeout_seconds=180,
            )
            self.provider_guard = (config_path, _fingerprint(config_path))
        if path:
            config = Path(path).resolve(strict=True)
            if config.stat().st_size > 65536:
                raise ValueError("ASR configuration exceeds bound")
            self.provider = MediaProviderSpec.model_validate_json(config.read_text())
            if (
                self.provider.modalities != ("audio",)
                or "transcription" not in self.provider.capabilities
            ):
                raise ValueError("ASR input requires the transcription audio Provider capability")
            self.provider_guard = (config, _fingerprint(config))
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS paired_voice_inputs (
                id TEXT PRIMARY KEY,principal TEXT NOT NULL,source_ref TEXT NOT NULL,
                sha256 TEXT NOT NULL,received REAL NOT NULL,utterance TEXT NOT NULL)""")

    @property
    def configured(self):
        return self.provider is not None

    def _active(self):
        if self.host.stop.is_set():
            raise Conflict("voice input node is closing")
        if self.provider_guard and _fingerprint(self.provider_guard[0]) != self.provider_guard[1]:
            raise Conflict("ASR binding changed; explicitly reload the node")

    async def _transcribe(self, path, event_id):
        async with self.provider_lock:
            if self.connection is not None:
                if not await self.connection.close():
                    raise Conflict("previous ASR request release is still unconfirmed")
                self.connection = None
            return await self._transcribe_owned(path, event_id)

    async def close(self):
        async with self.provider_lock:
            if self.connection is not None:
                if not await self.connection.close():
                    return False
                self.connection = None
            if self.local_client:
                await self.local_client.aclose()
            return True

    async def _transcribe_owned(self, path, event_id):
        if self.provider is None:
            raise Conflict("audio transcription Provider is not configured")
        self._active()
        process = MediaProviderConnection(
            self.provider,
            self.root / "provider",
            **({"client": self.local_client} if self.local_client else {}),
        )
        self.connection = process
        try:
            await process.reset()
            sha = await asyncio.to_thread(file_hash, path)
            ident = "asr-" + uuid.uuid4().hex
            body = {
                "schema": "harness.transcription/1",
                "request_id": ident,
                "model": self.provider.model,
                "audio_sha256": sha,
                "audio_wav_base64": base64.b64encode(path.read_bytes()).decode(),
            }
            process.active.add(ident)
            process._save()
            self.store.audit(
                None,
                "voice.asr.request",
                {
                    "event_id": event_id,
                    "request_id": ident,
                    "body": body,
                    "recognition_simulated": self.provider.simulated,
                },
            )
            result = TranscriptionResult.model_validate(
                await process._json("POST", "/transcribe", body)
            )
            if (
                result.request_id != ident
                or result.model != self.provider.model
                or result.audio_sha256 != sha
                or result.simulated != self.provider.simulated
            ):
                raise Conflict("ASR response differs from actual audio/model provenance")
            self.store.audit(
                None,
                "voice.asr.response",
                {"event_id": event_id, "request_id": ident, "response": result.model_dump()},
            )
            process.active.remove(ident)
            process._save()
            self._active()
            return result, sha
        finally:
            closed = await process.close()
            if not closed:
                raise Conflict("ASR request cancellation is not confirmed")
            self.connection = None

    def event_owner(self, principal, source_ref, event_id):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM paired_voice_inputs WHERE id=? AND principal=? AND source_ref=?",
                (event_id, principal, source_ref),
            ).fetchone()
        if row is None:
            raise KeyError(event_id)
        return row

    async def ingest(self, request, principal, source_ref, event_id):
        TypeAdapter(Reference).validate_python(event_id)
        if not self.configured:
            raise Conflict("user speech ASR Provider is not installed")
        source = self.host.input_media.source(principal, source_ref)
        if "audio" not in source["spec"]["binding"]["modalities"]:
            raise Conflict("this source has no declared microphone/audio capability")
        if self.root.resolve() != self.root:
            raise Conflict("voice input directory changed")
        self.root.mkdir(exist_ok=True)
        received = self.store.clock()
        temporary = self.root / (uuid.uuid4().hex + ".input")
        wav = temporary.with_suffix(".wav")
        hasher = hashlib.sha256()
        count = 0
        async with self.locks.setdefault(event_id, asyncio.Lock()):
            try:
                async with asyncio.timeout(30):
                    with temporary.open("xb") as output:
                        temporary.chmod(0o600)
                        async for chunk in request.stream():
                            count += len(chunk)
                            if count > 8 * 1024**2:
                                raise HTTPException(413, "one speech input exceeds eight MiB")
                            hasher.update(chunk)
                            output.write(chunk)
                    if not count:
                        raise ValueError("empty voice input")
                    original_sha = hasher.hexdigest()
                    try:
                        old = self.event_owner(principal, source_ref, event_id)
                    except KeyError:
                        old = None
                    with self.store.connect() as db:
                        collision = db.execute(
                            "SELECT 1 FROM paired_voice_inputs WHERE id=?", (event_id,)
                        ).fetchone()
                    if collision and not old:
                        raise Conflict("voice identity belongs to another source")
                    if old:
                        if old["sha256"] != original_sha:
                            raise Conflict("voice event identity has different audio")
                        utterance = VoiceUtterance.model_validate_json(old["utterance"])
                        return self.host.voice.ingest(
                            utterance,
                            principal,
                            adapter="mock-transcript"
                            if utterance.asr["recognition_simulated"]
                            else "file-asr",
                            explicit_activation=True,
                        )
                    cancelled = threading.Event()

                    def active():
                        self._active()
                        if cancelled.is_set():
                            raise Conflict("voice decoding cancelled")

                    def prepare():
                        facts = probe_input(temporary)
                        active()
                        if "audio" not in facts["modalities"] or facts["duration_ms"] > 30000:
                            raise ValueError(
                                "user speech needs an actual audio track at most thirty seconds"
                            )
                        ffmpeg(
                            [
                                "-n",
                                "-i",
                                str(temporary),
                                "-map",
                                "0:a:0",
                                "-vn",
                                "-ac",
                                "1",
                                "-ar",
                                "16000",
                                "-c:a",
                                "pcm_s16le",
                                str(wav),
                            ],
                            check_active=active,
                        )
                        active()

                    work = asyncio.create_task(asyncio.to_thread(prepare))
                    self.host.control_tasks.add(work)
                    try:
                        await asyncio.shield(work)
                    except asyncio.CancelledError:
                        cancelled.set()
                        while not work.done():
                            try:
                                await asyncio.shield(work)
                            except asyncio.CancelledError:
                                continue
                            except Exception:
                                break
                        raise
                    finally:
                        self.host.control_tasks.discard(work)
                result, sha = await self._transcribe(wav, event_id)
                utterance = VoiceUtterance(
                    event_id=event_id,
                    transcript=result.transcript,
                    source_ref="paired-user:" + source_ref,
                    received_at=datetime.fromtimestamp(received, timezone.utc),
                    replayed_at=datetime.fromtimestamp(self.store.clock(), timezone.utc),
                    audio_sha256=sha,
                    asr={
                        "model": result.model,
                        "model_revision": result.model_revision,
                        "recognition_simulated": result.simulated,
                        "source_simulated": source["simulated"],
                        "original_audio_sha256": original_sha,
                        "activation": "paired_push_to_talk",
                    },
                )
                with self.store.connect() as db:
                    db.execute(
                        "INSERT INTO paired_voice_inputs VALUES (?,?,?,?,?,?)",
                        (
                            event_id,
                            principal,
                            source_ref,
                            original_sha,
                            received,
                            canonical(utterance),
                        ),
                    )
                return self.host.voice.ingest(
                    utterance,
                    principal,
                    adapter="mock-transcript" if result.simulated else "file-asr",
                    explicit_activation=True,
                )
            finally:
                temporary.unlink(missing_ok=True)
                # ASR WAV is a derived input. Its actual hash/request is retained.
                wav.unlink(missing_ok=True)


def mount_paired_voice(app, host, principal, device_auth):
    gateway = PairedVoiceGateway(host)
    host.paired_voice = gateway

    @app.post("/harness/capture/{source_ref}/voice/{event_id}")
    async def voice_input(source_ref: str, event_id: str, request: Request):
        device_auth(request, source_ref, "interaction")
        return await gateway.ingest(request, principal, source_ref, event_id)

    @app.get("/harness/capture/{source_ref}/voice/{event_id}")
    async def voice_status(source_ref: str, event_id: str, request: Request):
        device_auth(request, source_ref, "interaction")
        gateway.event_owner(principal, source_ref, event_id)
        return host.voice.status(principal, event_id)

    @app.post("/harness/capture/{source_ref}/voice/{event_id}/delivered")
    async def delivered(source_ref: str, event_id: str, body: VoiceReplyDelivery, request: Request):
        device_auth(request, source_ref, "interaction")
        gateway.event_owner(principal, source_ref, event_id)
        if body.event_id != event_id:
            raise Conflict("voice delivery identity differs")
        return host.voice.acknowledge_delivery(body, principal)
