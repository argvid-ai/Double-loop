"""Bounded transcription service using an already prepared speech backend.

No implicit downloads and no microphone access. Each request owns an isolated
worker; cancelling a request terminates that worker before acknowledgement.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import threading
import time
import uuid
import wave
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from pydantic import Field

from .contracts import Contract
from .observation_contracts import Reference


class TranscriptionInput(Contract):
    schema_version: str = Field(alias="schema", pattern=r"^harness\.transcription/1$")
    request_id: Reference
    model: str = Field(min_length=1, max_length=160)
    audio_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    audio_wav_base64: str = Field(min_length=4, max_length=1400000)


class CancelInput(Contract):
    request_id: Reference


def create_speech_provider(
    model_dir, root, *, model=None, model_revision=None, backend="whisper", recognizer_config=None
):
    model_dir = Path(model_dir).resolve(strict=True)
    if backend == "zipformer":
        from .streaming_asr import StreamingRecognizerConfig, checked_model

        recognizer_config = StreamingRecognizerConfig.model_validate(
            recognizer_config or {"model_dir": str(model_dir)}
        )
        prepared_model, prepared_hash, _ = checked_model(recognizer_config)
        if model not in {None, prepared_model} or importlib.util.find_spec("sherpa_onnx") is None:
            raise ValueError("prepared streaming model/runtime binding differs")
        model, model_revision = prepared_model, prepared_hash
    elif backend == "whisper":
        model = model or "mlx-community/whisper-small-mlx"
        if not model_dir.is_dir() or not (model_dir / "config.json").is_file():
            raise ValueError("prepare local Whisper weights first; no download is performed")
        if importlib.util.find_spec("mlx_whisper") is None:
            raise ValueError("install the optional voice-mac runtime before starting ASR")
    else:
        raise ValueError("unknown installed speech backend")
    root = Path(root).absolute()
    if root.resolve() != root:
        raise ValueError("speech storage cannot be redirected")
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    app = FastAPI(title="Local prepared speech transcription")
    active = {}
    seen = {}
    gate = asyncio.Lock()

    @app.exception_handler(ValueError)
    @app.exception_handler(wave.Error)
    @app.exception_handler(EOFError)
    async def invalid(request, error):
        from fastapi.responses import JSONResponse

        return JSONResponse({"error": "invalid transcription request"}, status_code=422)

    @app.exception_handler(TimeoutError)
    async def timed_out(request, error):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            {"error": "transcription timed out; owned worker stopped"}, status_code=408
        )

    @app.get("/health")
    async def health():
        return {
            "model": model,
            "modalities": ["audio"],
            "capabilities": ["transcription"],
            "simulated": False,
            "ready": True,
            "cancellation_supported": True,
            "weights_loaded": False,
            "loading_policy": "one_owned_worker_per_request",
        }

    async def stop(process):
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except TimeoutError:
                process.kill()
                await process.wait()

    @app.post("/cancel")
    async def cancel(body: CancelInput):
        process = active.get(body.request_id)
        if process is not None:
            await stop(process)
        return {
            "request_id": body.request_id,
            "state": "cancelled"
            if process
            else "completed"
            if body.request_id in seen
            else "not_found",
        }

    @app.post("/transcribe")
    async def transcribe(request: Request):
        raw = bytearray()
        async with asyncio.timeout(30):
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > 1500000:
                    raise HTTPException(413, "speech request exceeds bound")
        body = TranscriptionInput.model_validate_json(raw)
        if body.model != model:
            raise HTTPException(409, "ASR model binding differs")
        data = base64.b64decode(body.audio_wav_base64, validate=True)
        if hashlib.sha256(data).hexdigest() != body.audio_sha256:
            raise HTTPException(422, "audio checksum differs")
        # Never wait behind another request beyond its admission time.
        if gate.locked():
            raise HTTPException(409, "ASR worker is occupied")
        async with gate:
            if body.request_id in seen:
                raise HTTPException(409, "ASR request identity was already used")
            used = sum(p.stat().st_size for p in root.rglob("*") if p.is_file())
            if used + 8 * 1024**2 > 512 * 1024**2 or shutil.disk_usage(root).free < 64 * 1024**2:
                raise HTTPException(
                    409, "protected speech storage is full; operator cleanup required"
                )
            folder = root / uuid.uuid4().hex
            folder.mkdir(mode=0o700)
            audio = folder / "input.wav"
            audio.write_bytes(data)
            with wave.open(str(audio), "rb") as sound:
                if (
                    sound.getnchannels() != 1
                    or sound.getframerate() != 16000
                    or sound.getsampwidth() != 2
                    or not 0 < sound.getnframes() <= 480000
                ):
                    raise HTTPException(422, "use at most thirty seconds of mono 16kHz PCM WAV")
            job = {
                "audio": str(audio),
                "model_dir": str(model_dir),
                "model": model,
                "revision": model_revision,
                "output": str(folder / "result.json"),
                "parent": os.getpid(),
                "backend": backend,
                "recognizer_config": recognizer_config.model_dump() if recognizer_config else None,
            }
            config = folder / "job.json"
            config.write_text(json.dumps(job))
            config.chmod(0o600)
            process = None
            try:
                # Shield creation so cancellation cannot lose ownership of a new worker.
                with (folder / "worker.log").open("wb") as log:
                    spawning = asyncio.create_task(
                        asyncio.create_subprocess_exec(
                            sys.executable,
                            "-m",
                            "cloud_loop.speech_provider",
                            "--worker",
                            str(config),
                            stdout=log,
                            stderr=log,
                        )
                    )
                    try:
                        process = await asyncio.shield(spawning)
                    except asyncio.CancelledError:
                        process = await spawning
                        await stop(process)
                        raise
                    active[body.request_id] = process
                    async with asyncio.timeout(180):
                        await process.wait()
                if process.returncode != 0:
                    raise HTTPException(
                        409, "local ASR worker did not finish; inspect protected node evidence"
                    )
                result = json.loads((folder / "result.json").read_text())
                if result["utterance"]["audio_sha256"] != body.audio_sha256:
                    raise HTTPException(409, "ASR input provenance differs")
                return {
                    "request_id": body.request_id,
                    "model": model,
                    "audio_sha256": body.audio_sha256,
                    "transcript": result["utterance"]["transcript"],
                    "simulated": False,
                    "model_revision": model_revision,
                }
            finally:
                if process is not None:
                    await stop(process)
                active.pop(body.request_id, None)
                seen[body.request_id] = time.monotonic()
                while len(seen) > 256:
                    seen.pop(next(iter(seen)))
                # Keep accepted raw audio, actual model provenance and failure logs locally.

    return app


def worker(config):
    job = json.loads(Path(config).read_text())

    def parent_watch():
        while True:
            if os.getppid() != job["parent"]:
                os._exit(75)
            time.sleep(0.2)

    threading.Thread(target=parent_watch, daemon=True).start()
    if job.get("backend", "whisper") == "zipformer":
        from .streaming_asr import StreamingRecognizer, StreamingRecognizerConfig

        engine = StreamingRecognizer(
            StreamingRecognizerConfig.model_validate(job["recognizer_config"])
        )
        if engine.model != job["model"] or engine.model_sha256 != job["revision"]:
            raise ValueError("speech model changed after provider binding")
        segments = []
        with wave.open(job["audio"], "rb") as audio:
            while pcm := audio.readframes(1600):
                item = engine.feed(pcm)
                if item is not None:
                    segments.append(item)
        item = engine.finish()
        if item is not None:
            segments.append(item)
        result = {
            "utterance": {
                "audio_sha256": hashlib.sha256(Path(job["audio"]).read_bytes()).hexdigest(),
                "transcript": "".join(item.text for item in segments),
            },
            "model_sha256": engine.model_sha256,
            "backend": "zipformer",
            "recognition_simulated": False,
            "segments": [
                {"text": item.text, "start_frame": item.start_frame, "end_frame": item.end_frame}
                for item in segments
            ],
        }
    else:
        from .harness_asr import transcribe_file

        result = transcribe_file(
            job["audio"],
            model=job["model"],
            model_dir=job["model_dir"],
            model_revision=job["revision"],
            wake_word="",
        )
    output = Path(job["output"])
    output.write_text(json.dumps(result, ensure_ascii=False))
    output.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--backend", choices=["whisper", "zipformer"], default="whisper")
    parser.add_argument("--model-revision")
    parser.add_argument("--port", type=int, default=8975)
    args = parser.parse_args()
    if args.worker:
        return worker(args.worker)
    if not args.model_dir or not args.root or not 1024 <= args.port <= 65535:
        parser.error("provide prepared model directory, evidence root and loopback port")
    import uvicorn

    uvicorn.run(
        create_speech_provider(
            args.model_dir,
            args.root,
            model=args.model,
            model_revision=args.model_revision,
            backend=args.backend,
        ),
        host="127.0.0.1",
        port=args.port,
        workers=1,
    )


if __name__ == "__main__":
    main()
