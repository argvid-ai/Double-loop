"""Registered file replay sources and observable playback; no visual inference."""

import json
import shutil
import tempfile
from pathlib import Path

import av

from .contracts import canonical
from .media import file_hash, inspect_video
from .store import Conflict


def list_available_sources(host, principal, source_id=None):
    """Describe both replay files and managed capture sources without opening them."""
    sources = host.sources.list(principal)
    ids = {item["source_id"] for item in sources}
    for registered in host.input_media.list(principal):
        spec = registered["spec"]
        binding = spec["binding"]
        ref = binding["source_ref"]
        if ref in ids:
            continue
        sources.append({
            "source_id": ref, "source_ref": ref, "title": spec["title"],
            "provider": "managed_capture", "source_kind": binding["kind"],
            "simulated": binding["simulated"], "modalities": binding["modalities"],
            "device_ref": binding["device_ref"],
            "read_tools": ["review_observation_period", "list_observation_markers",
                           "attach_observation_media"],
            "note": "Registered source identity, not proof of current recording or available footage.",
        })
    if source_id is not None:
        sources = [item for item in sources if item["source_id"] == source_id]
        if not sources:
            raise KeyError(source_id)
    return {"sources": sources}


class SourceLibrary:
    def __init__(self, store):
        self.store = store
        with store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS media_sources (
                id TEXT PRIMARY KEY, principal TEXT NOT NULL, path TEXT NOT NULL,
                metadata TEXT NOT NULL, playback TEXT NOT NULL, created REAL NOT NULL)""")

    def register(self, principal, args):
        source = Path(args.path).expanduser().resolve()
        if not source.is_file():
            raise ValueError("source file missing")
        facts = inspect_video(source)
        metadata = {
            **args.model_dump(exclude={"path", "source_id"}),
            "source_id": args.source_id,
            "facts": facts,
            "provider": "file_replay",
            "simulated": True,
            "vision_inference": False,
            "registered_at": self.store.clock(),
        }
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM media_sources WHERE id=?", (args.source_id,)).fetchone()
            if row:
                old = json.loads(row["metadata"])
                if row["principal"] != principal or any(
                    old[k] != metadata[k] for k in metadata if k != "registered_at"
                ):
                    raise Conflict(
                        "source identity already registered with different bytes or provenance"
                    )
                if file_hash(Path(row["path"])) != facts["sha256"]:
                    raise Conflict("registered file changed")
                return {**old, "duplicate": True}
        root = self.store.root / "sources"
        root.mkdir(parents=True, exist_ok=True)
        target = root / (args.source_id + "-" + facts["sha256"] + source.suffix.lower())
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as handle:
            temporary = Path(handle.name)
        try:
            shutil.copyfile(source, temporary)
            if file_hash(temporary) != facts["sha256"]:
                raise Conflict("source changed during registration")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT principal,metadata FROM media_sources WHERE id=?", (args.source_id,)
            ).fetchone()
            if row:
                raise Conflict("source registration raced; retry to check identity")
            playback = {
                "position_ms": 0,
                "playing": False,
                "updated": self.store.clock(),
                "source": "file_replay_control",
                "simulated": True,
            }
            db.execute(
                "INSERT INTO media_sources VALUES (?,?,?,?,?,?)",
                (
                    args.source_id,
                    principal,
                    str(target),
                    canonical(metadata),
                    canonical(playback),
                    self.store.clock(),
                ),
            )
            self.store._audit(
                db,
                None,
                "source.registered",
                {
                    "principal": principal,
                    "source_id": args.source_id,
                    "sha256": facts["sha256"],
                    "source_kind": args.source_kind,
                },
            )
        return metadata

    def get(self, principal, source_id, *, include_path=False):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM media_sources WHERE id=? AND principal=?", (source_id, principal)
            ).fetchone()
            if row is None:
                raise KeyError(source_id)
        metadata = json.loads(row["metadata"])
        playback = json.loads(row["playback"])
        if playback["playing"]:
            playback["position_ms"] = min(
                metadata["facts"]["duration_ms"],
                playback["position_ms"]
                + max(0, round((self.store.clock() - playback["updated"]) * 1000)),
            )
            if playback["position_ms"] >= metadata["facts"]["duration_ms"]:
                playback["playing"] = False
        return {**metadata, "playback": playback, **({"path": row["path"]} if include_path else {})}

    def list(self, principal):
        with self.store.connect() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM media_sources WHERE principal=? ORDER BY created", (principal,)
                )
            ]
        return [self.get(principal, ident) for ident in ids]

    def control(self, principal, args):
        source = self.get(principal, args.source_id)
        if args.position_ms > source["facts"]["duration_ms"]:
            raise ValueError("playback position exceeds duration")
        with self.store.connect() as db:
            state = {
                "position_ms": args.position_ms,
                "playing": args.playing,
                "updated": self.store.clock(),
                "source": "file_replay_control",
                "simulated": True,
            }
            db.execute(
                "UPDATE media_sources SET playback=? WHERE id=? AND principal=?",
                (canonical(state), args.source_id, principal),
            )
            self.store._audit(
                db,
                None,
                "source.playback.changed",
                {"principal": principal, "source_id": args.source_id, **state},
            )
        return self.get(principal, args.source_id)

    def frame(self, principal, args):
        source = self.get(principal, args.source_id, include_path=True)
        if args.position_ms >= source["facts"]["duration_ms"]:
            raise ValueError("frame position must precede source end")
        path = Path(source["path"])
        if file_hash(path) != source["facts"]["sha256"]:
            raise Conflict("registered source checksum changed")
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            container.seek(
                int(args.position_ms / 1000 / float(stream.time_base)), stream=stream, backward=True
            )
            chosen = None
            for frame in container.decode(stream):
                if frame.time is None:
                    continue
                chosen = frame
                if frame.time * 1000 >= args.position_ms:
                    break
            if chosen is None:
                raise ValueError("no timestamped frame at requested position")
            destination = (
                self.store.root / "source-frames" / f"{args.source_id}-{args.position_ms}.jpg"
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            picture = chosen.to_image()
            picture.thumbnail((960, 960))
            picture.save(destination)
            return {
                "source_id": args.source_id,
                "requested_position_ms": args.position_ms,
                "frame_time_ms": round(chosen.time * 1000),
                "selection_policy": "first_frame_at_or_after_requested_or_last_frame_at_end",
                "source_sha256": source["facts"]["sha256"],
                "path": str(destination),
                "simulated": True,
                "vision_inference": False,
            }
