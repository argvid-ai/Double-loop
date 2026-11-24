"""One model-requested spoken reply for the current admitted voice event."""
import json

from pydantic import Field, field_validator

from .contracts import Contract, canonical
from .store import Conflict


class ReplyWithVoice(Contract):
    text: str = Field(min_length=1, max_length=800,
                      description="要实际说给用户听的回答；简短口语，不是已播放的声明。")

    @field_validator("text")
    @classmethod
    def nonempty(cls, text):
        if not text.strip():
            raise ValueError("spoken reply cannot be blank")
        return text.strip()


def request_reply(host, event, args, call_id):
    text = ReplyWithVoice(**args).text
    if host.side != "cloud" or event.get("channel") != "voice" or event["kind"] != "voice":
        raise Conflict("voice reply belongs to the current user voice input only")
    if not host.settings.voice_audio_enabled or host.settings.voice_tts_backend == "disabled":
        raise Conflict("voice synthesis is not configured; answer in text and explain")
    principal, ident = event["principal"], event["id"]
    with host.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        actual = db.execute(
            "SELECT status,session_key FROM harness_events WHERE id=? AND principal=?",
            (ident, principal),
        ).fetchone()
        accepted = db.execute(
            "SELECT 1 FROM voice_forward_inbox WHERE id=? AND principal=? AND admission='accepted'",
            (ident, principal),
        ).fetchone()
        main = host.store.conversations.main(principal, db=db)
        if not actual or actual["status"] != "running" or not accepted or actual["session_key"] != main["id"]:
            raise Conflict("voice reply requires the running admitted main-conversation input")
        old = db.execute("SELECT * FROM voice_audio WHERE event_id=?", (ident,)).fetchone()
        request = {"text": text, "requested_by": "reply_with_voice", "call_id": call_id}
        if old:
            if old["principal"] != principal or old["status"] != "requested":
                raise Conflict("this voice reply is already finalized")
            # Updating a draft before model completion cannot duplicate playback.
            duplicate = json.loads(old["request"])["text"] == text
            if not duplicate:
                db.execute("UPDATE voice_audio SET request=? WHERE event_id=?",
                           (canonical(request), ident))
        else:
            duplicate = False
            db.execute(
                "INSERT INTO voice_audio(event_id,principal,status,request) VALUES (?,?,'requested',?)",
                (ident, principal, canonical(request)),
            )
    return {"event_id": ident, "status": "requested", "text": text, "duplicate": duplicate,
            "audio_generated": False, "played": False,
            "next_step": "本轮结束后合成并交板端播放；现在不要声称已播放。结束本轮即可，不要另写一份不同答复。"}


def prepare_reply(host, event, reply, db):
    """The selected spoken content also becomes the main conversation's reply."""
    if event["channel"] != "voice" or host.side != "cloud":
        return reply
    row = db.execute("SELECT * FROM voice_audio WHERE event_id=? AND principal=?",
                     (event["id"], event["principal"])).fetchone()
    requested = bool(row and row["status"] == "requested")
    if requested and not reply.get("error") and not reply.get("cancelled"):
        return {**reply, "text": json.loads(row["request"])["text"],
                "voice_reply": {"requested": True, "tool": "reply_with_voice"}}
    return {**reply, "voice_reply": {"requested": requested,
                                    "error": "reply_failed" if reply.get("error") else "voice_reply_tool_not_called"}}
