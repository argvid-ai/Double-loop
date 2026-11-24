"""Bounded portable speech worker. Input arrives over stdin, output is PCM WAV."""

import json
import sys
import wave
from pathlib import Path


def generate(request):
    import numpy as np
    import sherpa_onnx

    model = Path(request["model_dir"])
    config = sherpa_onnx.OfflineTtsConfig(
        model=sherpa_onnx.OfflineTtsModelConfig(
            vits=sherpa_onnx.OfflineTtsVitsModelConfig(
                model=str(model / "model.onnx"),
                tokens=str(model / "tokens.txt"),
                lexicon=str(model / "lexicon.txt"),
            ),
            num_threads=request["threads"],
            provider="cpu",
        ),
        rule_fsts=",".join(
            str(model / name)
            for name in ["phone.fst", "date.fst", "number.fst"]
            if (model / name).is_file()
        ),
    )
    if not config.validate():
        raise ValueError("invalid portable speech configuration")
    engine = sherpa_onnx.OfflineTts(config)
    audio = engine.generate(request["text"], sid=request["speaker_id"], speed=1.0)
    samples = np.asarray(audio.samples, dtype=np.float32)
    if not 0 < len(samples) <= 180 * audio.sample_rate:
        raise ValueError("speech duration outside the supported range")
    if audio.sample_rate != 16000:
        positions = (
            np.arange(round(len(samples) * 16000 / audio.sample_rate)) * audio.sample_rate / 16000
        )
        samples = np.interp(positions, np.arange(len(samples)), samples)
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(request["output"], "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(pcm.tobytes())
    return {
        "provider": "sherpa_vits",
        "speaker_id": request["speaker_id"],
        "native_sample_rate": audio.sample_rate,
        "synthesis_simulated": False,
        "audio_generated": True,
        "played": False,
    }


if __name__ == "__main__":
    request = json.loads(sys.stdin.buffer.read(16000))
    print(json.dumps(generate(request)))
