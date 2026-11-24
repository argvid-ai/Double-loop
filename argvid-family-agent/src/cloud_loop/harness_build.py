"""Runtime source identity, including untracked Python and frontend assets."""

from hashlib import sha256
from pathlib import Path

from .contracts import digest


def source_identity():
    root = Path(__file__).parent
    manifest = {
        str(p.relative_to(root)): sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
        and (
            p.suffix in {".py", ".html", ".css", ".js", ".mjs", ".md"}
            or (
                (p.is_relative_to(root / "data") or p.is_relative_to(root / "pi"))
                and p.suffix in {".json", ".csv"}
            )
        )
        and "__pycache__" not in p.parts
    }
    return {"digest": digest(manifest), "files": manifest}
