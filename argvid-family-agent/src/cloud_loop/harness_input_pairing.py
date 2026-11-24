"""Source-scoped device keys; plaintext is written only to a private local file."""

from __future__ import annotations

import hashlib
import secrets

from pydantic import TypeAdapter

from .observation_contracts import Reference
from .store import Conflict


class InputPairing:
    def __init__(self, media):
        self.media, self.store = media, media.store
        self.root = self.store.root.resolve() / "input-pairing"
        with self.store.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS input_pairing_keys (
                principal TEXT NOT NULL,source_ref TEXT NOT NULL,purpose TEXT NOT NULL,
                hash TEXT NOT NULL,PRIMARY KEY(principal,source_ref,purpose))""")

    def issue(self, principal, source_ref, *, purpose="observation", rotate=False):
        TypeAdapter(Reference).validate_python(source_ref)
        self.media.source(principal, source_ref)
        if purpose not in {"observation", "interaction"}:
            raise ValueError("unknown capture key purpose")
        if self.root.resolve() != self.root:
            raise Conflict("pairing key directory changed")
        self.root.mkdir(exist_ok=True)
        self.root.chmod(0o700)
        path = self.root / (source_ref + "." + purpose + ".key")
        if path.is_symlink():
            raise Conflict("pairing key file cannot be a symlink")
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT hash FROM input_pairing_keys WHERE principal=? AND source_ref=? AND purpose=?",
                (principal, source_ref, purpose),
            ).fetchone()
            if old and not rotate:
                if (
                    not path.is_file()
                    or hashlib.sha256(path.read_bytes().strip()).hexdigest() != old["hash"]
                ):
                    raise Conflict("pairing key is unavailable; explicitly rotate this source key")
                return {
                    "key_file": str(path),
                    "source_ref": source_ref,
                    "purpose": purpose,
                    "rotated": False,
                }
            token = secrets.token_urlsafe(32)
            temporary = path.with_suffix(".new")
            try:
                with temporary.open("x") as output:
                    temporary.chmod(0o600)
                    output.write(token + "\n")
                temporary.replace(path)
                db.execute(
                    "INSERT OR REPLACE INTO input_pairing_keys VALUES (?,?,?,?)",
                    (principal, source_ref, purpose, hashlib.sha256(token.encode()).hexdigest()),
                )
            finally:
                temporary.unlink(missing_ok=True)
        return {
            "key_file": str(path),
            "source_ref": source_ref,
            "purpose": purpose,
            "rotated": bool(old),
        }

    def authenticate(self, principal, source_ref, header, *, purpose=None):
        if not header.startswith("Capture ") or len(header) > 300:
            return None
        candidate = hashlib.sha256(header[8:].encode()).hexdigest()
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT hash,purpose FROM input_pairing_keys WHERE principal=? AND source_ref=?",
                (principal, source_ref),
            ).fetchall()
        for row in rows:
            if secrets.compare_digest(row["hash"], candidate) and (
                purpose is None or row["purpose"] == purpose
            ):
                return row["purpose"]
        return None
