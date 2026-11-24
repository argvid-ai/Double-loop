"""Board playback, with real device discovery and no fake delivery receipts."""

import asyncio
import hashlib
import json
import tempfile
from pathlib import Path

from .contracts import digest
from .voice_audio import MAX_AUDIO_BYTES, audio_facts


async def outputs():
    """Read PipeWire; do not change profiles, defaults, volume or pairing."""
    process = await asyncio.create_subprocess_exec(
        "pw-dump", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    try:
        raw, _ = await asyncio.wait_for(process.communicate(), 5)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if process.returncode:
        raise RuntimeError("audio_discovery_failed")
    devices = []
    for item in json.loads(raw):
        props = item.get("info", {}).get("props", {})
        name = props.get("node.name", "")
        if props.get("media.class") != "Audio/Sink":
            continue
        if not props.get("device.id") or name in {"auto_null", "dummy", ""}:
            continue
        devices.append({
            "name": name,
            "label": props.get("node.description") or props.get("node.nick") or name,
        })
    return devices


def choose_output(devices, selected):
    if selected:
        return next((d for d in devices if d["name"] == selected), None)
    # Never guess between physical rooms or several speakers.
    return devices[0] if len(devices) == 1 else None


async def play_file(path, device, volume):
    """Process success means submitted playback finished, not human hearing."""
    process = await asyncio.create_subprocess_exec(
        "pw-play", "--target", device["name"], "--volume", str(volume), str(path),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(process.wait(), 190)
        if process.returncode:
            raise RuntimeError("speaker_playback_failed")
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()


async def verified_audio(client, event_id, reply, folder):
    response = await client.get(f"/harness/voice/audio/{event_id}/status", timeout=5)
    response.raise_for_status()
    facts = response.json()
    if facts["status"] != "ready":
        return None, facts
    if facts.get("reply_digest") != digest(reply):
        raise ValueError("audio_reply_mismatch")
    target = folder / "reply.wav"
    size = 0
    async with client.stream("GET", f"/harness/voice/audio/{event_id}", timeout=20) as stream:
        stream.raise_for_status()
        with target.open("xb") as output:
            target.chmod(0o600)
            async for block in stream.aiter_bytes(65536):
                size += len(block)
                if size > MAX_AUDIO_BYTES:
                    raise ValueError("audio_too_large")
                output.write(block)
    actual = await asyncio.to_thread(audio_facts, target)
    if any(actual[k] != facts[k] for k in ("sha256", "bytes", "duration_ms")):
        raise ValueError("audio_fingerprint_mismatch")
    if facts.get("text_sha256") != hashlib.sha256(reply["text"].encode()).hexdigest():
        raise ValueError("audio_text_mismatch")
    return target, facts


class BoardPlayback:
    def __init__(self, runtime, *, discover=outputs, player=play_file):
        self.runtime, self.store = runtime, runtime.store
        self.discover, self.player = discover, player
        self.devices = []
        self.selected = None
        self.probe_error = None
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS board_voice_replies (
                id TEXT PRIMARY KEY, first_seen REAL NOT NULL, status TEXT NOT NULL,
                reply_digest TEXT NOT NULL, error TEXT, finished REAL)""")
            # A process may have died after output but before saving its receipt.
            # Do not replay potentially spoken words on service restart.
            db.execute("""UPDATE board_voice_replies SET status='uncertain',
                error='playback_interrupted' WHERE status='playing'""")

    async def probe(self):
        try:
            self.devices = await self.discover()
            self.selected = choose_output(self.devices, self.runtime.preferences()["output_device"])
            self.probe_error = None
        except (OSError, ValueError, RuntimeError, TimeoutError):
            self.devices, self.selected = [], None
            self.probe_error = "audio_discovery_unavailable"

    def mark(self, ident, status, error=None):
        with self.store.connect() as db:
            db.execute(
                "UPDATE board_voice_replies SET status=?,error=?,finished=? WHERE id=?",
                (status, error, self.store.clock(), ident),
            )

    async def tick(self):
        from .harness_contracts import VoiceReplyDelivery

        with self.store.connect() as db:
            rows = db.execute("""SELECT o.id,o.reply,o.body FROM voice_forward_outbox o
                LEFT JOIN board_voice_replies p ON p.id=o.id
                WHERE o.status IN ('reply_ready','delivered')
                AND (p.id IS NULL OR p.status IN ('waiting','retry','played'))
                ORDER BY o.rowid LIMIT 8""").fetchall()
        for row in rows:
            reply = json.loads(row["reply"])
            with self.store.connect() as db:
                db.execute("INSERT OR IGNORE INTO board_voice_replies VALUES (?,?,'waiting',?,NULL,NULL)",
                           (row["id"], self.store.clock(), digest(reply)))
                saved = dict(db.execute("SELECT * FROM board_voice_replies WHERE id=?", (row["id"],)).fetchone())
            delivery = VoiceReplyDelivery(event_id=row["id"], reply_digest=digest(reply))
            if saved["status"] == "played":
                self.runtime.voice.acknowledge_delivery(delivery, self.runtime.config.principal)
                self.mark(row["id"], "delivered")
                continue
            body = json.loads(row["body"])
            if body["adapter"] == "mock-transcript" or body["utterance"].get("asr", {}).get("source_simulated"):
                self.mark(row["id"], "simulation", "test_audio_not_sent_to_speaker")
                continue
            if self.store.clock() - saved["first_seen"] > self.runtime.config.reply_max_age_seconds:
                self.mark(row["id"], "expired", "reply_too_old_to_play")
                continue
            if not self.runtime.preferences()["enabled"] or self.selected is None:
                continue
            try:
                with tempfile.TemporaryDirectory(prefix="reply-", dir=self.runtime.cache) as folder:
                    path, audio = await verified_audio(self.runtime.client, row["id"], reply, Path(folder))
                    if path is None:
                        if audio["status"] in {"failed", "expired", "disabled"}:
                            self.mark(row["id"], "failed", "speech_generation_unavailable")
                        continue
                    if self.store.clock() - saved["first_seen"] > self.runtime.config.reply_max_age_seconds:
                        self.mark(row["id"], "expired", "reply_too_old_to_play")
                        continue
                    self.runtime.speaking.set()
                    self.mark(row["id"], "playing")
                    try:
                        await self.player(path, self.selected, self.runtime.preferences()["volume"])
                    finally:
                        self.runtime.listen_after = self.store.clock() + 1
                        self.runtime.speaking.clear()
                    self.mark(row["id"], "played")
                    self.runtime.voice.acknowledge_delivery(delivery, self.runtime.config.principal)
                    self.mark(row["id"], "delivered")
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                # A partially spoken reply must never start over automatically.
                current = self.item(row["id"])
                self.mark(row["id"], "uncertain" if current["status"] in {"playing", "played"} else "retry",
                          type(exc).__name__)

    def item(self, ident):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM board_voice_replies WHERE id=?", (ident,)).fetchone()
        return dict(row) if row else None
