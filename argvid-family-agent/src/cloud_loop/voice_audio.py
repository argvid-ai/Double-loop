"""Cloud speech synthesis and verified edge download; never a playback receipt."""

import asyncio
import hashlib
import json
import platform
import shutil
import time
import uuid
import wave
import sys
from contextlib import nullcontext
from pathlib import Path

from .contracts import canonical, digest
from .media import ffmpeg
from .store import Conflict

MAX_AUDIO_BYTES = 20 * 1024**2


def audio_facts(path):
    raw = path.read_bytes()
    if len(raw) > MAX_AUDIO_BYTES:
        raise Conflict("voice output exceeds audio budget")
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2 or wav.getframerate() != 16000:
            raise Conflict("voice output must be mono 16-bit 16kHz PCM")
        frames = wav.getnframes()
        if not 0 < frames <= 180 * 16000:
            raise Conflict("voice output has invalid duration")
        if len(wav.readframes(frames)) != frames * 2:
            raise Conflict("voice output is truncated")
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "duration_ms": round(frames / 16),
        "sample_rate": 16000,
        "channels": 1,
        "format": "wav",
    }


async def synthesize_macos(text, voice, folder):
    if platform.system() != "Darwin" or not Path("/usr/bin/say").is_file():
        raise Conflict("configured macOS speech provider is unavailable")
    if not 0 < len(text) <= 800:
        raise Conflict("reply exceeds configured speech length; text remains available")
    folder.mkdir(parents=True)
    script = folder / "text.txt"
    script.write_text(text)
    script.chmod(0o600)
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/say",
        "-v",
        voice,
        "-f",
        str(script),
        "-o",
        str(folder / "speech.aiff"),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(process.wait(), 120)
        if process.returncode:
            raise Conflict("speech provider failed")
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()
    target = folder / "speech.wav"
    conversion = asyncio.create_task(
        asyncio.to_thread(
            ffmpeg,
            [
                "-i",
                str(folder / "speech.aiff"),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(target),
            ],
        )
    )
    try:
        await asyncio.shield(conversion)
    except asyncio.CancelledError:
        await asyncio.shield(conversion)
        raise
    facts = await asyncio.to_thread(audio_facts, target)
    return target, {
        **facts,
        "provider": "macos_say",
        "voice": voice,
        "provider_platform": platform.mac_ver()[0],
        "synthesis_simulated": False,
        "audio_generated": True,
        "played": False,
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


async def synthesize_portable(text, settings, folder):
    if not 0 < len(text) <= 800 or not settings.voice_tts_model_dir:
        raise Conflict("portable speech requires a model and a reply of at most 800 characters")
    folder.mkdir(parents=True)
    target = folder / "speech.wav"
    request = {
        "text": text,
        "model_dir": settings.voice_tts_model_dir,
        "speaker_id": settings.voice_tts_speaker_id,
        "threads": settings.voice_tts_threads,
        "output": str(target),
    }
    process = await asyncio.create_subprocess_exec(
        getattr(settings, "voice_tts_python", None) or sys.executable,
        "-m",
        "cloud_loop.tts_worker",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(json.dumps(request).encode()), 120)
        if process.returncode:
            raise Conflict("portable speech provider failed")
        metadata = json.loads(stdout.decode().splitlines()[-1])
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()
    return target, {
        **metadata,
        **audio_facts(target),
        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
    }


class VoiceAudio:
    def __init__(self, host):
        self.host, self.store = host, host.store
        self.root = self.store.root / "voice-audio"
        self.root.mkdir(exist_ok=True)
        self.lock = asyncio.Lock()
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS voice_audio (
                event_id TEXT PRIMARY KEY,principal TEXT NOT NULL,status TEXT NOT NULL,
                request TEXT,metadata TEXT,error TEXT,next_try REAL NOT NULL DEFAULT 0)""")
            db.execute("UPDATE voice_audio SET status='pending' WHERE status='running'")

    def queue(self, event, reply, *, _db=None):
        if (
            self.host.side != "cloud"
            or not self.host.settings.voice_audio_enabled
            or self.host.settings.voice_tts_backend == "disabled"
        ):
            return False
        if event["channel"] != "voice":
            return False
        with nullcontext(_db) if _db is not None else self.store.connect() as db:
            forwarded = db.execute(
                "SELECT 1 FROM voice_forward_inbox WHERE id=? AND principal=? AND admission=?",
                (event["id"], event["principal"], "accepted"),
            ).fetchone()
            if not forwarded:
                return False
            previous = db.execute("SELECT status,request FROM voice_audio WHERE event_id=? AND principal=?",
                                  (event["id"], event["principal"])).fetchone()
            if reply.get("error") or reply.get("cancelled") or not reply.get("text"):
                if previous and previous["status"] == "requested":
                    db.execute("UPDATE voice_audio SET status='failed',error='cloud_reply_unavailable' WHERE event_id=?",
                               (event["id"],))
                return False
            if not previous:
                db.execute("INSERT INTO voice_audio(event_id,principal,status,error) VALUES (?,?,'failed','voice_reply_tool_not_called')",
                           (event["id"], event["principal"]))
                return False
            if previous["status"] != "requested":
                return previous["status"] in {"pending", "running", "ready"}
            request = {
                "text": reply["text"],
                "reply_digest": digest(reply),
                "voice": self.host.settings.voice_tts_voice,
                "requested_by": "reply_with_voice",
            }
            db.execute(
                "UPDATE voice_audio SET status='pending',request=?,error=NULL WHERE event_id=? AND principal=?",
                (canonical(request), event["id"], event["principal"]),
            )
        return True

    def status(self, principal, event_id):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM voice_audio WHERE event_id=? AND principal=?", (event_id, principal)
            ).fetchone()
        if row is None:
            if self.host.side == "cloud":
                with self.store.connect() as db:
                    event = db.execute(
                        "SELECT status,error FROM harness_events WHERE id=? AND principal=?",
                        (event_id, principal),
                    ).fetchone()
                if event and event["status"] in {"failed", "superseded"}:
                    return {
                        "event_id": event_id,
                        "status": "failed",
                        "error": event["error"] or "cloud_reply_unavailable",
                        "audio_generated": False,
                        "played": False,
                    }
            return {
                "event_id": event_id,
                "status": "pending"
                if self.host.settings.voice_audio_enabled
                and (self.host.side == "edge" or self.host.settings.voice_tts_backend != "disabled")
                else "disabled",
                "audio_generated": False,
                "played": False,
            }
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        path = self.root / (digest([principal, event_id]) + ".wav")
        expired = self.host.media_storage.expired(path)
        return {
            **metadata,
            "event_id": event_id,
            "status": "expired" if expired else row["status"],
            "error": row["error"],
            "played": False,
        }

    def path(self, principal, event_id):
        status = self.status(principal, event_id)
        if status["status"] != "ready":
            raise Conflict("speech audio is not ready")
        path = self.root / (digest([principal, event_id]) + ".wav")
        if audio_facts(path)["sha256"] != status["sha256"]:
            raise Conflict("speech file changed")
        return path

    async def process_one(self):
        if not self.host.settings.voice_audio_enabled:
            return False
        async with self.lock:
            if self.host.side == "edge":
                return await self._receive()
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT * FROM voice_audio WHERE status='pending' ORDER BY rowid LIMIT 1"
                ).fetchone()
                if row:
                    db.execute(
                        "UPDATE voice_audio SET status='running' WHERE event_id=?",
                        (row["event_id"],),
                    )
            if not row:
                return False
            request = json.loads(row["request"])
            started = time.monotonic()
            folder = self.root / ("work-" + uuid.uuid4().hex)
            try:
                with self.host.media_storage.reserve("voice-audio", MAX_AUDIO_BYTES):
                    if self.host.settings.voice_tts_backend == "sherpa_vits":
                        path, metadata = await synthesize_portable(
                            request["text"], self.host.settings, folder
                        )
                    else:
                        path, metadata = await synthesize_macos(
                            request["text"], request["voice"], folder
                        )
                    target = self.root / (digest([row["principal"], row["event_id"]]) + ".wav")
                    path.replace(target)
                    self.host.media_storage.register(target, "voice-audio")
                metadata.update(
                    generation_seconds=time.monotonic() - started,
                    reply_digest=request["reply_digest"],
                    stored_on_edge=False,
                )
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE voice_audio SET status='ready',metadata=?,error=NULL WHERE event_id=?",
                        (canonical(metadata), row["event_id"]),
                    )
                self.store.audit(
                    None, "voice.audio.generated", {"event_id": row["event_id"], **metadata}
                )
            except asyncio.CancelledError:
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE voice_audio SET status='pending' WHERE event_id=?",
                        (row["event_id"],),
                    )
                raise
            except Exception as exc:
                with self.store.connect() as db:
                    db.execute(
                        "UPDATE voice_audio SET status='failed',error=? WHERE event_id=?",
                        (type(exc).__name__, row["event_id"]),
                    )
                self.store.audit(
                    None,
                    "voice.audio.failed",
                    {"event_id": row["event_id"], "error_type": type(exc).__name__},
                )
            finally:
                if folder.exists():
                    shutil.rmtree(folder)
            return True

    async def _receive(self):
        with self.store.connect() as db:
            row = db.execute(
                """SELECT o.* FROM voice_forward_outbox o LEFT JOIN voice_audio a ON a.event_id=o.id
                WHERE o.reply IS NOT NULL AND json_extract(o.body,'$.adapter')!='edge-text'
                AND (a.event_id IS NULL OR (a.status='pending' AND a.next_try<=?)) ORDER BY o.rowid LIMIT 1""",
                (self.store.clock(),),
            ).fetchone()
        if row is None:
            return False
        event_id = row["id"]
        principal = row["principal"]
        destination = self.root / (digest([principal, event_id]) + ".wav")
        temp = destination.with_suffix(".pending")
        try:
            response = await self.host.client.get(
                "/harness/voice/audio/" + event_id + "/status", timeout=3
            )
            response.raise_for_status()
            meta = response.json()
            if meta.get("status") not in {"ready", "failed", "disabled", "expired"}:
                with self.store.connect() as db:
                    db.execute(
                        "INSERT INTO voice_audio(event_id,principal,status,next_try) VALUES (?,?,'pending',?) ON CONFLICT(event_id) DO UPDATE SET next_try=excluded.next_try",
                        (event_id, principal, self.store.clock() + 1),
                    )
                return False
            if meta["status"] != "ready":
                with self.store.connect() as db:
                    db.execute(
                        "INSERT OR REPLACE INTO voice_audio(event_id,principal,status,error) VALUES (?,?,?,?)",
                        (event_id, principal, meta["status"], meta.get("error")),
                    )
                return True
            if (
                meta.get("reply_digest") != digest(json.loads(row["reply"]))
                or meta.get("synthesis_simulated") is not False
            ):
                raise Conflict("speech does not belong to this real cloud reply")
            with self.host.media_storage.reserve("voice-audio", MAX_AUDIO_BYTES):
                size = 0
                async with self.host.client.stream(
                    "GET", "/harness/voice/audio/" + event_id, timeout=15
                ) as response:
                    response.raise_for_status()
                    with temp.open("wb") as output:
                        async for block in response.aiter_bytes():
                            size += len(block)
                            if size > MAX_AUDIO_BYTES:
                                raise Conflict("voice download exceeds budget")
                            output.write(block)
                facts = await asyncio.to_thread(audio_facts, temp)
                if any(meta.get(k) != v for k, v in facts.items()):
                    raise Conflict("downloaded speech differs from advertised bytes")
                temp.replace(destination)
                self.host.media_storage.register(destination, "voice-audio")
            meta.update(stored_on_edge=True, played=False)
            with self.store.connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO voice_audio(event_id,principal,status,metadata) VALUES (?,?,'ready',?)",
                    (event_id, principal, canonical(meta)),
                )
            self.store.audit(None, "voice.audio.received", {"event_id": event_id, **meta})
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            with self.store.connect() as db:
                db.execute(
                    "INSERT INTO voice_audio(event_id,principal,status,error,next_try) VALUES (?,?,'pending',?,?) ON CONFLICT(event_id) DO UPDATE SET status='pending',error=excluded.error,next_try=excluded.next_try",
                    (event_id, principal, type(exc).__name__, self.store.clock() + 5),
                )
            return False
        finally:
            temp.unlink(missing_ok=True)
