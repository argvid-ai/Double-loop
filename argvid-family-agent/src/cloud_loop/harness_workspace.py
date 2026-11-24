"""Update shipped workspace files while preserving locally edited versions."""

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .harness_autonomy import save_text


def sync_managed_files(workspace, sources):
    workspace = Path(workspace)
    manifest = workspace / ".muse-managed-files.json"
    previous = json.loads(manifest.read_text()) if manifest.exists() else {}
    installed = dict(previous)
    conflicts = []
    for relative, source in sorted(sources.items()):
        content = Path(source).read_bytes()
        mode = Path(source).stat().st_mode & 0o777
        wanted = hashlib.sha256(content).hexdigest()
        target = workspace / relative
        current = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
        if current not in (None, wanted, previous.get(relative)):
            candidate = workspace / ".muse-updates" / relative
            save_file(candidate, content, mode)
            conflicts.append({"path": relative, "candidate": str(candidate)})
            continue
        if current != wanted:
            save_file(target, content, mode)
        installed[relative] = wanted
    save_text(manifest, json.dumps(installed, ensure_ascii=False, indent=2))
    return {"managed_files": len(installed), "conflicts": conflicts}


def save_file(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write(content)
    try:
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
