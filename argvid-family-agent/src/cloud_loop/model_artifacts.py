"""Explicit, checksum-verified model preparation. No automatic fallback/download."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path


def read_manifest(path):
    manifest = json.loads(Path(path).read_text())
    a = manifest["artifact"]
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", a["repo"])
        or not re.fullmatch(r"[0-9a-f]{40}", a["revision"])
        or not re.fullmatch(r"[A-Za-z0-9_.-]+\.gguf", a["filename"])
        or not re.fullmatch(r"[0-9a-f]{64}", a["sha256"])
        or not isinstance(a["size_bytes"], int)
        or a["size_bytes"] <= 0
    ):
        raise ValueError("invalid pinned model manifest")
    return manifest


def verify_file(path, artifact):
    if path.stat().st_size != artifact["size_bytes"]:
        raise ValueError("model file size mismatch")
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    if h.hexdigest() != artifact["sha256"]:
        raise ValueError("model checksum mismatch")
    return h.hexdigest()


def resources(directory):
    directory.mkdir(parents=True, exist_ok=True)
    info = {
        "hostname": platform.node(),
        "machine": platform.machine(),
        "system": platform.system(),
        "cpu_count": os.cpu_count(),
        "disk_free_bytes": shutil.disk_usage(directory).free,
    }
    if Path("/proc/meminfo").exists():
        mem = {
            k: int(v.strip().split()[0]) * 1024
            for k, v in (
                line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines()
            )
        }
        info.update(memory_total_bytes=mem["MemTotal"], memory_available_bytes=mem["MemAvailable"])
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["inspect", "download", "verify", "serve"])
    parser.add_argument("--manifest", type=Path, default=Path("examples/o6n-model-source.json"))
    parser.add_argument("--directory", type=Path, default=Path(".state/o6n/models"))
    parser.add_argument("--server", type=Path, help="path to the pinned llama-server build")
    args = parser.parse_args()
    manifest = read_manifest(args.manifest)
    artifact = manifest["artifact"]
    facts = resources(args.directory)
    model = args.directory / artifact["filename"]
    if args.action == "inspect":
        print(
            json.dumps(
                {"resources": facts, "model_present": model.exists(), "candidate": manifest},
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    if args.action in {"download", "serve"}:
        if facts["system"] != "Linux" or facts["machine"] != "aarch64":
            raise RuntimeError("this deployment command requires the ARM64 Linux board")
        # This conservative admission check is not a model memory benchmark.
        if facts.get("memory_available_bytes", 0) < artifact["size_bytes"] + 4 * 1024**3:
            raise RuntimeError(
                "available memory is below weights + 4 GiB initial reserve; reselect with measured evidence"
            )
    if args.action == "download" and not model.exists():
        if facts["disk_free_bytes"] < artifact["size_bytes"] + 2 * 1024**3:
            raise RuntimeError("insufficient free disk for model and working files")
        url = f"https://huggingface.co/{artifact['repo']}/resolve/{artifact['revision']}/{artifact['filename']}"
        temporary = model.with_suffix(".partial")
        with urllib.request.urlopen(url, timeout=60) as source, temporary.open("wb") as target:
            total = 0
            while chunk := source.read(4 * 1024 * 1024):
                total += len(chunk)
                if total > artifact["size_bytes"]:
                    raise ValueError("download exceeded published size")
                target.write(chunk)
        verify_file(temporary, artifact)
        temporary.replace(model)
    checksum = verify_file(model, artifact)
    evidence = {
        "manifest": manifest,
        "actual_sha256": checksum,
        "resources": facts,
        "path": str(model.resolve()),
        "verified_at": __import__("datetime")
        .datetime.now(__import__("datetime").timezone.utc)
        .isoformat(),
        "claim": "file integrity verified; successful inference not implied",
    }
    model.with_suffix(".verified.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2)
    )
    if args.action == "serve":
        if args.server is None or not args.server.is_file():
            raise RuntimeError("provide the pinned llama-server executable with --server")
        command = [
            str(args.server.resolve()),
            "--model",
            str(model.resolve()),
            "--alias",
            manifest["runtime_alias"],
            "--host",
            "127.0.0.1",
            "--port",
            "18080",
            "--ctx-size",
            "8192",
            "--parallel",
            "1",
            "--n-gpu-layers",
            "0",
            "--jinja",
        ]
        version = subprocess.run(
            [str(args.server.resolve()), "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        model.with_suffix(".launch.json").write_text(
            json.dumps(
                {
                    "command": command,
                    "version": (version.stdout + version.stderr)[:4000],
                    "sha256": checksum,
                    "model_host": facts["hostname"],
                    "note": "launch record, not proof of successful model response",
                },
                indent=2,
            )
        )
        subprocess.run(command, check=True)
    else:
        print(json.dumps({"verified": True, "file": str(model), "sha256": checksum}, indent=2))


if __name__ == "__main__":
    main()
