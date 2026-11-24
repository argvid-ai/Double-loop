"""Freeze indexed tails only after the task's capture writer has been released.

An unclean host stop can leave a file labelled 'growing' forever. The input
reader must not wait for that dead writer before reaching a later capture.
Published ranges and bytes are retained, but an interrupted tail is not
misrepresented as a completely decoded or cleanly sealed recording.
"""

import json
from pathlib import Path
from types import SimpleNamespace

from .observation_contracts import ObservationRun
from .recorded_video import interrupt_video_recording
from .store import Conflict


def freeze_stopped_capture_tails(media, task_id, root):
    """Caller must hold the capture child lock after confirming writer release."""
    with media.store.connect() as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='observation_tasks'"
        ).fetchone():
            return []
        task = db.execute(
            "SELECT principal,version,generation,config FROM observation_tasks WHERE task_id=?",
            (task_id,),
        ).fetchone()
        if task is None:
            return []
        source_ref = json.loads(task["config"])["source_ref"]
        rows = db.execute(
            """SELECT e.id,e.path,e.body FROM managed_input_entries e
            JOIN raw_recordings r ON r.media_ref=e.id AND r.owned_path=e.path
            WHERE e.principal=? AND e.source_ref=? AND r.state='available'
            AND json_extract(e.body,'$.recording_state')='growing'
            AND json_extract(e.body,'$.source_fingerprint_kind')='recording_identity'""",
            (task["principal"], source_ref),
        ).fetchall()
    root = Path(root)
    run = ObservationRun(
        principal=task["principal"], task_id=task_id,
        version=task["version"], generation=task["generation"],
    )
    recovered = []
    for row in rows:
        body = json.loads(row["body"])
        capture_id = body["segment_id"]
        if not capture_id.startswith("continuous-") or Path(capture_id).name != capture_id:
            continue
        folder = root / capture_id
        # Other tasks can share a source identity. Their folders are not ours.
        if not folder.is_dir():
            continue
        if folder.is_symlink() or folder.resolve().parent != root.resolve():
            raise Conflict("stopped capture folder escaped its task")
        target = Path(row["path"])
        if target.is_symlink() or target.resolve().parent != (media.root / "raw").resolve():
            raise Conflict("stopped capture tail escaped owned storage")
        prefix = capture_id + "-"
        event_id = body["event_id"]
        stem = event_id.removeprefix(prefix)
        if not event_id.startswith(prefix) or not stem.startswith("part-") or Path(stem).name != stem:
            raise Conflict("stopped capture tail identity differs from its folder")
        controller = SimpleNamespace(
            media=media, run=run, capture_id=capture_id,
            spec=SimpleNamespace(binding=SimpleNamespace(source_ref=source_ref)),
        )
        if interrupt_video_recording(
            controller, folder / (stem + ".mkv"), "capture_writer_recovered_after_restart"
        ):
            recovered.append(row["id"])
    return recovered
