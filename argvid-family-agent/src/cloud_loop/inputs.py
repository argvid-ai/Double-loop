"""One typed input boundary for replay, sensor facts and speech transcripts.

Mock producers create observations, never task outcomes. A future hardware
producer implements InputSource and sends the same InputEvent to InputIngress.
Only sources installed in the host's allowlist may ingest; metadata is not trust.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from .contracts import ID, Contract, Scene, canonical, utcnow
from .store import Conflict


class SceneObservation(Contract):
    kind: Literal["scene"] = "scene"
    subject_id: str = Field(default="pet-cat-mimi", pattern=ID)
    user_present: bool
    subject_visible: bool
    activity: Literal["PLAYING", "UNKNOWN"]


class RobotObservation(Contract):
    kind: Literal["robot"] = "robot"
    safe_to_execute: bool
    viewpoint: Literal["home", "front-mid"] = "home"
    motion_outcome: Literal["succeeded", "rejected", "timeout"] = "succeeded"


class TranscriptObservation(Contract):
    kind: Literal["transcript"] = "transcript"
    text: str = Field(min_length=1, max_length=2000)
    purpose: Literal["user_command", "media_content"] = "user_command"


class InputEvent(Contract):
    schema_version: Literal["cix.input/1.0"] = "cix.input/1.0"
    event_id: str = Field(pattern=ID)
    provider: str = Field(pattern=ID)
    sequence: int = Field(ge=1, le=9_000_000_000_000_000)
    observed_at: datetime
    source_ref: str = Field(min_length=1, max_length=1000)
    simulated: bool
    valid_for_ms: int = Field(default=5000, ge=1, le=3_600_000)
    task_id: str | None = Field(default=None, pattern=ID)
    data: Annotated[
        SceneObservation | RobotObservation | TranscriptObservation, Field(discriminator="kind")
    ]

    @field_validator("observed_at")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("observation requires timezone")
        return value

    @model_validator(mode="after")
    def task_binding(self):
        if isinstance(self.data, TranscriptObservation) and not self.task_id:
            raise ValueError("transcript requires an existing task binding")
        if not isinstance(self.data, TranscriptObservation) and self.task_id:
            raise ValueError("scene and robot observations are node-scoped")
        return self


class InputSource(Protocol):
    """Implementations may read annotated files, cameras or ASR outputs."""

    def observations(self) -> list[InputEvent]: ...


class MockControlRequest(Contract):
    kind: Literal["stage", "transcript", "resume", "motion"]
    stage: Literal["S0", "S1", "S2"] | None = None
    task_id: str | None = Field(default=None, pattern=ID)
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    motion_outcome: Literal["succeeded", "rejected", "timeout"] | None = None

    @model_validator(mode="after")
    def shape(self):
        required = {
            "stage": {"stage"},
            "transcript": {"task_id", "text"},
            "resume": {"task_id"},
            "motion": {"motion_outcome"},
        }[self.kind]
        actual = {
            k
            for k in ("stage", "task_id", "text", "motion_outcome")
            if getattr(self, k) is not None
        }
        if actual != required:
            raise ValueError("unexpected or missing control fields")
        return self


MOCK_SOURCES = {
    "mock-scene": ("scene", True),
    "mock-robot": ("robot", True),
    "mock-transcript": ("transcript", True),
    "mock-media-transcript": ("transcript", True),
    "mock-local-scene": ("scene", True),
    "mock-local-robot": ("robot", True),
}


def mock_stage(
    stage: str, sequence: int, *, viewpoint="home", provider_prefix="mock", valid_for_ms=60000
) -> list[InputEvent]:
    if stage not in {"S0", "S1", "S2"}:
        raise ValueError("unknown replay stage")
    common = dict(
        sequence=sequence,
        observed_at=utcnow(),
        source_ref=f"fixture:cat-replay/{stage}",
        simulated=True,
        valid_for_ms=valid_for_ms,
    )
    return [
        InputEvent(
            event_id=uuid.uuid4().hex,
            provider=f"{provider_prefix}-scene",
            **common,
            data=SceneObservation(
                user_present=stage != "S0", subject_visible=True, activity="PLAYING"
            ),
        ),
        InputEvent(
            event_id=uuid.uuid4().hex,
            provider=f"{provider_prefix}-robot",
            **common,
            data=RobotObservation(safe_to_execute=stage == "S2", viewpoint=viewpoint),
        ),
    ]


def mock_transcript(task_id, text, sequence):
    return InputEvent(
        event_id=uuid.uuid4().hex,
        provider="mock-transcript",
        sequence=sequence,
        observed_at=utcnow(),
        source_ref="fixture:mock-transcript",
        simulated=True,
        task_id=task_id,
        data=TranscriptObservation(text=text),
    )


class InputIngress:
    def __init__(self, store, sources=None):
        self.store = store
        self.sources = dict(MOCK_SOURCES if sources is None else sources)
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS input_events (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, result TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS input_heads (
                    provider TEXT PRIMARY KEY, sequence INTEGER NOT NULL, observed_at TEXT NOT NULL);
            """)

    def ingest(self, event: InputEvent):
        return self.ingest_many([event])[0]

    def ingest_many(self, events: list[InputEvent]):
        # A replay frame may carry scene + device facts. Publish them together
        # so a worker cannot act on a partially updated permission snapshot.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return [self._ingest(db, event) for event in events]

    def _ingest(self, db, event):
        if self.sources.get(event.provider) != (event.data.kind, event.simulated):
            raise Conflict("input provider not installed or provenance mismatch")
        if event.observed_at > utcnow() + timedelta(seconds=30):
            raise Conflict("observation is in the future")
        body = canonical(event)
        old = db.execute(
            "SELECT body,result FROM input_events WHERE id=?", (event.event_id,)
        ).fetchone()
        if old:
            if old["body"] != body:
                raise Conflict("input event identity conflict")
            return {**json.loads(old["result"]), "duplicate": True}
        if event.observed_at + timedelta(milliseconds=event.valid_for_ms) <= utcnow():
            raise Conflict("observation expired")
        if (
            event.provider == "mock-media-transcript"
            and isinstance(event.data, TranscriptObservation)
            and event.data.purpose != "media_content"
        ):
            raise Conflict("media source cannot claim user command authority")
        head = db.execute(
            "SELECT * FROM input_heads WHERE provider=?", (event.provider,)
        ).fetchone()
        if head and (
            event.sequence <= head["sequence"]
            or event.observed_at < datetime.fromisoformat(head["observed_at"])
        ):
            raise Conflict("stale source observation")
        if isinstance(event.data, TranscriptObservation) and event.data.purpose == "media_content":
            # Media text is retained as source data; it never enters the user
            # command router or wakes a task with command authority.
            result = {"accepted": True, "data_only": True, "simulated": event.simulated}
        elif isinstance(event.data, TranscriptObservation):
            result = self._transcript(db, event)
        else:
            result = self._facts(db, event)
        db.execute(
            "INSERT INTO input_events VALUES (?,?,?)", (event.event_id, body, canonical(result))
        )
        db.execute(
            "INSERT OR REPLACE INTO input_heads VALUES (?,?,?)",
            (event.provider, event.sequence, event.observed_at.isoformat()),
        )
        self.store._audit(
            db, event.task_id, f"input.{event.data.kind}", event.model_dump(mode="json")
        )
        return result

    def _facts(self, db, event):
        scene = Scene.model_validate_json(
            db.execute("SELECT value FROM metadata WHERE key='scene'").fetchone()[0]
        )
        before = (
            scene.subject_id,
            scene.user_present,
            scene.subject_visible,
            scene.activity,
            scene.safe_to_execute,
            scene.viewpoint,
        )
        previous = scene.field_sources.get(event.data.kind)
        if previous and event.observed_at < datetime.fromisoformat(previous["observed_at"]):
            raise Conflict("observation older than current field facts")
        if isinstance(event.data, SceneObservation):
            for key in ("subject_id", "user_present", "subject_visible", "activity"):
                setattr(scene, key, getattr(event.data, key))
        else:
            scene.safe_to_execute = event.data.safe_to_execute
            scene.viewpoint = event.data.viewpoint
            db.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('motion_outcome',?)",
                (canonical(event.data.motion_outcome),),
            )
        scene.field_sources[event.data.kind] = {
            "provider": event.provider,
            "observed_at": event.observed_at.isoformat(),
            "sequence": event.sequence,
            "source_ref": event.source_ref,
            "simulated": event.simulated,
            "valid_until": (
                event.observed_at + timedelta(milliseconds=event.valid_for_ms)
            ).isoformat(),
        }
        scene.simulated = (
            any(x["simulated"] for x in scene.field_sources.values())
            or len(scene.field_sources) < 2
        )
        scene.source = "input_projection"
        scene.observed_at = event.observed_at
        after = (
            scene.subject_id,
            scene.user_present,
            scene.subject_visible,
            scene.activity,
            scene.safe_to_execute,
            scene.viewpoint,
        )
        # Refreshing the same facts extends their explicit validity without
        # invalidating an in-flight action every sensor heartbeat. A changed
        # semantic fact or source still advances the execution revision.
        if before != after or previous is None or previous["provider"] != event.provider:
            scene.revision += 1
        scene.stage = "S0" if not scene.user_present else "S2" if scene.safe_to_execute else "S1"
        db.execute("UPDATE metadata SET value=? WHERE key='scene'", (canonical(scene),))
        self.store._audit(db, None, "scene.changed", scene.model_dump(mode="json"))
        for row in db.execute(
            "SELECT id,context FROM tasks WHERE status NOT IN ('completed','unsupported','cancelled','expired','failed')"
        ).fetchall():
            context = json.loads(row["context"])
            # Interactive capture is a program-owned effect of the conversation
            # NanoBot; it must not accidentally start a second legacy agent loop.
            if context.get("owner") in {"conversation", "harness"}:
                continue
            if not context.get("paused"):
                self._wake(
                    db,
                    row["id"],
                    f"input:{event.event_id}:{row['id']}",
                    "scene.changed",
                    {"revision": scene.revision},
                )
        return {"accepted": True, "scene_revision": scene.revision, "simulated": event.simulated}

    @staticmethod
    def _wake(db, task_id, event_id, kind, payload):
        if kind == "scene.changed":
            db.execute(
                "UPDATE events SET status='superseded' WHERE task_id=? AND kind='scene.changed' AND status='queued'",
                (task_id,),
            )
        db.execute(
            "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,'queued',NULL,?)",
            (event_id, task_id, kind, canonical(payload), time.time(), time.time()),
        )

    def _transcript(self, db, event):
        task = db.execute(
            "SELECT status,context FROM tasks WHERE id=?", (event.task_id,)
        ).fetchone()
        if not task:
            raise Conflict("transcript task not found")
        context = json.loads(task["context"])
        if (
            task["status"] in {"completed", "unsupported", "cancelled", "expired"}
            or context["expires"] <= time.time()
        ):
            raise Conflict("transcript task is terminal or expired")
        pause = event.data.text.strip() in {
            "暂停当前拍摄任务",
            "请暂停当前拍摄任务",
            "停止当前拍摄任务",
        }
        if pause:
            context["paused"] = True
            db.execute(
                "UPDATE tasks SET context=?,status='waiting',outcome=NULL WHERE id=?",
                (canonical(context), event.task_id),
            )
            db.execute(
                "UPDATE events SET status='superseded' WHERE task_id=? AND status='queued'",
                (event.task_id,),
            )
            self.store._audit(
                db,
                event.task_id,
                "task.paused",
                {"input_event": event.event_id, "simulated": event.simulated},
            )
        elif not context.get("paused"):
            self._wake(
                db,
                event.task_id,
                f"input:{event.event_id}",
                "voice.transcript",
                {"text": event.data.text, "simulated": event.simulated},
            )
        return {
            "accepted": True,
            "paused": bool(context.get("paused")),
            "simulated": event.simulated,
        }

    def resume(self, task_id):
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = db.execute("SELECT status,context FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise Conflict("task not found")
            context = json.loads(task["context"])
            if (
                task["status"] in {"completed", "unsupported", "cancelled", "expired"}
                or context["expires"] <= time.time()
            ):
                raise Conflict("cannot resume terminal or expired task")
            if not context.get("paused"):
                return {"resumed": False}
            context["paused"] = False
            db.execute("UPDATE tasks SET context=? WHERE id=?", (canonical(context), task_id))
            self._wake(db, task_id, uuid.uuid4().hex, "task.resumed", {})
            self.store._audit(db, task_id, "task.resumed", {})
            return {"resumed": True}
