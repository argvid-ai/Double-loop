"""Capture byte sources share a materialization contract; device guards live above it."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol

from .media import file_hash, make_clip
from .store import Conflict


class CaptureSource(Protocol):
    """A future camera driver returns the same local file + source metadata.

    Drivers do not publish task completion, choose task identity, or bypass
    action guards. No live driver is installed by this module.
    """

    # The runtime supplies a cooperative cancellation/permission check. Drivers
    # must call it during bounded work and stop owned I/O before propagating it.
    def materialize(self, destination: Path, subject_id: str, *, check_active=None) -> dict: ...
    def recover(self, destination: Path, subject_id: str, *, check_active=None) -> dict | None: ...


class PreparedClipSource:
    """Explicit compatibility mode for the existing same-host demo."""

    operation = "copy_prepared_clip"

    def __init__(self, edge_root):
        self.edge_root = edge_root

    def materialize(self, destination, subject_id, *, check_active=None):
        info, fixture = self._source(subject_id, check_active=check_active)
        with fixture.open("rb") as source, destination.open("wb") as output:
            while chunk := source.read(1024 * 1024):
                if check_active:
                    check_active()
                output.write(chunk)
        if check_active:
            check_active()
        return {
            "provenance": info["provenance"],
            "selection": info["selection"],
            "operation": self.operation,
        }

    def _source(self, subject_id, *, check_active=None):
        if check_active:
            check_active()
        fixture = self.edge_root / "fixtures" / "closeup.mp4"
        info = json.loads(fixture.with_suffix(".json").read_text())
        if (
            info["subject_id"] != subject_id
            or file_hash(fixture, check_active=check_active) != info["asset"]["sha256"]
        ):
            raise Conflict("prepared replay source differs from registered subject or checksum")
        return info, fixture

    def recover(self, destination, subject_id, *, check_active=None):
        if not destination.exists():
            return None
        info, _ = self._source(subject_id, check_active=check_active)
        if file_hash(destination, check_active=check_active) != info["asset"]["sha256"]:
            raise Conflict("interrupted copy differs from the verified source")
        return {
            "provenance": info["provenance"],
            "selection": info["selection"],
            "operation": self.operation,
        }


class VideoFileSource:
    """The Mock camera reads original licensed bytes, then actually cuts a clip."""

    operation = "extract_from_original_video"

    def __init__(self, edge_root):
        self.source_root = edge_root.parent / "source"

    def recover(self, destination, subject_id, *, check_active=None):
        if check_active:
            check_active()
        # No receipt binds a loose re-encoded file to its source. Rebuild it;
        # committed CapturePackages are reused before this method is reached.
        return None

    def materialize(self, destination, subject_id, *, check_active=None):
        if check_active:
            check_active()
        original = self.source_root / "cat-track.webm"
        provenance = json.loads((self.source_root / "source.json").read_text())
        selection = json.loads((self.source_root / "selection.json").read_text())
        if (
            selection["subject_id"] != subject_id
            or file_hash(original, check_active=check_active) != provenance["sha256"]
            or selection["source_sha256"] != provenance["sha256"]
        ):
            raise Conflict("original video or reviewed selection changed")
        chosen = selection["closeup"]
        make_clip(
            original,
            destination,
            start_s=chosen["start_s"],
            crop=chosen.get("crop"),
            check_active=check_active,
        )
        return {"provenance": provenance, "selection": chosen, "operation": self.operation}
