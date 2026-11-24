"""One resident CPU recognizer. Audio capture and cloud delivery stay outside it."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import platform
from dataclasses import dataclass
from pathlib import Path

from pydantic import Field, field_validator

from .contracts import Contract, digest
from .voice_contracts import StreamingASRProof


class StreamingRecognizerConfig(Contract):
    model_dir: str
    threads: int = Field(default=2, strict=True, ge=1, le=8)
    keywords: list[str] = Field(default_factory=list, max_length=16)
    silence_seconds: float = Field(default=1.2, gt=0, le=5, allow_inf_nan=False)
    max_segment_seconds: int = Field(default=30, strict=True, ge=5, le=60)

    @field_validator("model_dir")
    @classmethod
    def absolute_model(cls, value):
        if not Path(value).is_absolute():
            raise ValueError("model directory must be an installed absolute path")
        return value

    @field_validator("keywords")
    @classmethod
    def short_keywords(cls, values):
        if any(not v.strip() or len(v) > 32 or any(c in v for c in "\n\r/") for v in values):
            raise ValueError("use short configured keyword hints, not instructions")
        return values


@dataclass(frozen=True)
class RecognizedSegment:
    text: str
    pcm: bytes
    start_frame: int
    end_frame: int
    reason: str


def checked_model(config):
    root = Path(config.model_dir)
    if root.resolve() != root:
        raise ValueError("model directory cannot be redirected")
    manifest = root / "manifest.json"
    if manifest.is_symlink() or manifest.stat().st_size > 65536:
        raise ValueError("invalid asset manifest")
    body = json.loads(manifest.read_text())
    if body.get("schema") != "harness.streaming-asr-assets/1":
        raise ValueError("prepare a streaming ASR asset manifest first")
    paths, fingerprints = {}, {}
    for role in ("encoder", "decoder", "joiner", "tokens", "bpe"):
        spec = body["files"][role]
        path = root / spec["filename"]
        if path.parent != root or path.resolve() != path or not path.is_file():
            raise ValueError("model asset escaped its installed directory")
        before = path.stat()
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        after = path.stat()
        if (
            (before.st_size, before.st_mtime_ns, before.st_ino)
            != (after.st_size, after.st_mtime_ns, after.st_ino)
            or after.st_size != spec["bytes"]
            or hasher.hexdigest() != spec["sha256"]
        ):
            raise ValueError("model asset differs from prepared manifest")
        paths[role] = str(path)
        fingerprints[role] = spec["sha256"]
    # sherpa's contextual-biasing reader expects text, not a SentencePiece .model.
    # Validate before crossing the native API, which otherwise exits the process.
    for line in Path(paths["bpe"]).read_text().splitlines():
        fields = line.split()
        if len(fields) != 2:
            raise ValueError("streaming ASR needs the text bpe.vocab file")
        float(fields[1])
    return body["model"], digest(fingerprints), paths


class StreamingRecognizer:
    sample_rate = 16000

    def __init__(self, config: StreamingRecognizerConfig):
        import sherpa_onnx

        self.config = config
        self.model, self.model_sha256, paths = checked_model(config)
        self.runtime_version = importlib.metadata.version("sherpa-onnx")
        if self.runtime_version != "1.13.8":
            raise ValueError("streaming adapter requires the prepared sherpa-onnx 1.13.8 runtime")
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            **{k: paths[k] for k in ("encoder", "decoder", "joiner", "tokens")},
            num_threads=config.threads,
            provider="cpu",
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=max(2.4, config.silence_seconds),
            rule2_min_trailing_silence=config.silence_seconds,
            rule3_min_utterance_length=config.max_segment_seconds,
            decoding_method="modified_beam_search" if config.keywords else "greedy_search",
            modeling_unit="cjkchar+bpe",
            bpe_vocab=paths["bpe"],
        )
        self.stream = self.recognizer.create_stream(
            hotwords="/".join(config.keywords) if config.keywords else None
        )
        self.pcm = bytearray()
        self.offset = 0
        self.closed = False

    def _decode(self):
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)

    def _segment(self, reason):
        text = self.recognizer.get_result(self.stream)
        end = self.offset + len(self.pcm) // 2
        value = RecognizedSegment(text, bytes(self.pcm), self.offset, end, reason)
        self.offset = end
        self.pcm.clear()
        self.recognizer.reset(self.stream)
        return value

    def feed(self, pcm: bytes):
        import numpy as np

        if self.closed or not pcm or len(pcm) % 2 or len(pcm) > 3200:
            raise ValueError("feed at most 100ms of mono 16kHz signed PCM16 to an open stream")
        self.pcm.extend(pcm)
        self.stream.accept_waveform(
            16000, np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        )
        self._decode()
        if self.recognizer.is_endpoint(self.stream) or (
            len(self.pcm) >= self.config.max_segment_seconds * 32000
        ):
            return self._segment("endpoint")
        return None

    def finish(self):
        import numpy as np

        if self.closed:
            raise ValueError("stream is already finished")
        # Decoder lookahead is synthetic padding, never recorded as captured audio.
        self.stream.accept_waveform(16000, np.zeros(8000, dtype=np.float32))
        self.stream.input_finished()
        self._decode()
        value = self._segment("eof") if self.pcm else None
        self.closed = True
        return value

    def proof(self, segment, kind):
        return StreamingASRProof(
            model=self.model,
            model_sha256=self.model_sha256,
            runtime="sherpa-onnx",
            runtime_version=self.runtime_version,
            processing_host=platform.node(),
            recognition_simulated=False,
            source_kind=kind,
            source_simulated=kind == "file",
            sample_rate=16000,
            start_frame=segment.start_frame,
            end_frame=segment.end_frame,
            keywords=self.config.keywords,
        )
