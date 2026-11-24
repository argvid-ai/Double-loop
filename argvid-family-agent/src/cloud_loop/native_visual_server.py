"""Task-owned CPU worker for installed BLIP/CLIP assets; no downloads or tools."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import io
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from PIL import Image

from .visual_contracts import VisualRequest, VisualResult


def create_app(model_dir, family, revision, alias, threads):
    import torch
    from transformers import (
        BlipForQuestionAnswering,
        BlipProcessor,
        CLIPModel,
        CLIPProcessor,
    )

    torch.set_num_threads(threads)
    cls, processor_cls = (
        (CLIPModel, CLIPProcessor)
        if family == "clip"
        else (BlipForQuestionAnswering, BlipProcessor)
    )
    processor = processor_cls.from_pretrained(model_dir, local_files_only=True)
    model = cls.from_pretrained(model_dir, local_files_only=True).to("cpu").eval()
    app = FastAPI()
    inference_lock = asyncio.Lock()
    import uuid
    instance_id = uuid.uuid4().hex

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/props")
    async def props():
        return {"model_alias": alias, "family": family, "revision": revision,
                "read_only": True, "instance_id": instance_id,
                "inflight": int(inference_lock.locked())}

    def infer(body):
        started = time.monotonic()
        results = []
        for frame in body.frames:
            data = base64.b64decode(frame.jpeg_base64, validate=True)
            if hashlib.sha256(data).hexdigest() != frame.sha256:
                raise ValueError("frame bytes differ from registered evidence")
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > 4_000_000:
                    raise ValueError("image exceeds inference pixel budget")
                image = image.convert("RGB")
                with torch.inference_mode():
                    if family == "clip":
                        inputs = processor(
                            text=list(body.texts),
                            images=image,
                            return_tensors="pt",
                            padding=True,
                        )
                        if (
                            inputs.input_ids.shape[-1]
                            > model.config.text_config.max_position_embeddings
                        ):
                            raise ValueError("CLIP text exceeds installed tokenizer limit")
                        output = model(**inputs)
                        scores = (output.image_embeds @ output.text_embeds.T)[0].tolist()
                        value = {"similarities": scores}
                    else:
                        inputs = processor(image, body.question, return_tensors="pt")
                        output = model.generate(**inputs, max_new_tokens=12, do_sample=False)
                        value = {"answer": processor.decode(output[0], skip_special_tokens=True)}
            results.append({"sha256": frame.sha256, "sample_at_ms": frame.sample_at_ms, **value})
        return VisualResult(
            request_id=body.request_id,
            model_alias=alias,
            family=family,
            revision=revision,
            simulated=False,
            frames=results,
            inference_seconds=time.monotonic() - started,
        ).model_dump(mode="json")

    @app.post("/observe")
    async def observe(request: Request):
        # CPU work stays in this owned process. Cancellation terminates it via the
        # existing process manager, with OS identity and task fencing on the caller.
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 16 * 1024**2:
                raise HTTPException(413, "native visual request exceeds bound")
        try:
            body = VisualRequest.model_validate_json(raw)
            if (body.model_alias, body.family, body.revision) != (alias, family, revision):
                raise ValueError("request model binding differs")
            if inference_lock.locked():
                raise HTTPException(429, "visual computation still in progress; retry later")
            async with inference_lock:
                work = asyncio.create_task(asyncio.to_thread(infer, body))
                try:
                    return await asyncio.shield(work)
                except asyncio.CancelledError:
                    # A lost client must not make /props claim the CPU is idle.
                    await asyncio.gather(work, return_exceptions=True)
                    raise
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    return app


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model-dir", type=Path, required=True)
    p.add_argument("--family", choices=["clip", "blip_vqa"], required=True)
    p.add_argument("--revision", required=True)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--host", choices=["127.0.0.1"], default="127.0.0.1")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--alias", required=True)
    args = p.parse_args()
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import uvicorn

    app = create_app(args.model_dir, args.family, args.revision, args.alias, args.threads)
    uvicorn.run(app, host=args.host, port=args.port, workers=1, access_log=False)


if __name__ == "__main__":
    main()
