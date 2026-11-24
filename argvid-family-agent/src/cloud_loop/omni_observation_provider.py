"""NanoBot Provider for the pinned MiniCPM native simplex HTTP protocol.

There is no claim that the upstream server already exposes consumption proof.
Production decoding requires a receipt from the managed, instrumented backend;
without it the reply is rejected before any tool call can execute.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import uuid
import wave
from copy import deepcopy
from pathlib import Path
from urllib.parse import urlparse

import httpx
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

from .contracts import canonical
from .observation_payloads import MEDIA_TYPE
from .store import Conflict


def normalized(message):
    result = {"role": message["role"], "content": message.get("content") or ""}
    for field in ("tool_call_id", "name"):
        if message.get(field):
            result[field] = message[field]
    if message.get("tool_calls"):
        calls = []
        for call in message["tool_calls"]:
            function = call["function"]
            args = function.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args)
            calls.append(
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {"name": function["name"], "arguments": args},
                }
            )
        result["tool_calls"] = calls
    return result


class OmniObservationProvider(LLMProvider):
    supports_observation_media = True
    supports_progress_deltas = False
    inference_simulated = False

    def __init__(
        self,
        endpoint,
        payloads,
        principal,
        task_id,
        *,
        model="MiniCPM-o-4_5",
        client=None,
        reset_backend,
        close_backend,
        audit,
        output_root,
        abort_backend=None,
        asset_guard=None,
        media_writer=None,
    ):
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("native model endpoint must be credential-free loopback")
        super().__init__()
        self.endpoint = endpoint.rstrip("/")
        self.payloads = payloads
        self.principal, self.task_id, self.model = principal, task_id, model
        self.client = client or httpx.AsyncClient(
            base_url=self.endpoint, timeout=30, trust_env=False
        )
        self.owns_client = client is None
        self.reset_backend, self.close_backend, self.audit = reset_backend, close_backend, audit
        self.abort_backend = abort_backend or reset_backend
        self.asset_guard = asset_guard or (lambda: None)
        self.media_writer = media_writer
        self.output_root = Path(output_root).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.lock = asyncio.Lock()
        self.session = None
        self.prefix = []
        self.signature = None
        self.cnt = 0
        self.round = 0
        self.valid = False
        self.active_media = False
        self.closed = False
        self.loading_phase = "not_started"
        self.initialization_failed = False

    def get_default_model(self):
        return self.model

    def _check_assets(self):
        try:
            self.asset_guard()
        except BaseException:
            self.valid = False
            raise

    def estimate_prompt_tokens(self, messages, tools=None, model=None):
        # Bound rather than pretend to expose the native tokenizer. The fixed
        # overview route produced64 image tokens in preflight;128 reserves margin.
        # Audio receives100 tokens/sec, above the observed10/sec native chunks.
        count = len(canonical(tools or []).encode()) + 512
        refs = self.payloads.validate_messages(self.principal, self.task_id, messages)
        for message in messages:
            count += len(canonical(message).encode()) + 32
        for ident in self.payloads.references(messages):
            value = refs[ident]
            manifest = value["manifest"]
            count += (
                128 * len(manifest["frames"])
                + int(100 * (manifest["end_ms"] - manifest["start_ms"]) / 1000)
                + 64
            )
        return count, "conservative_utf8_plus_fixed_AV_bounds_not_native_usage"

    async def open_session(self, key, *, system=None, tools=None):
        if self.closed:
            raise Conflict("native Provider is closed")
        if self.session is not None and self.session != key:
            raise Conflict("native Provider is bound to another work session")
        self.session = key
        if system is not None:
            await self._initialize(system, tools)
            self.prefix = [{"role": "system", "content": system}]
            self.signature = canonical([self.prefix[0], tools or []])

    async def _post(self, path, payload):
        self.audit("native.request", {"path": path, "body": payload})
        response = await self.client.post(path, json=payload)
        response.raise_for_status()
        data = response.json()
        if data.get("success") is not True:
            raise Conflict("native request did not succeed")
        self.audit("native.response", {"path": path, "body": data})
        return data

    async def _prefill(self, *, text="", image="", audio=""):
        body = {"cnt": self.cnt, "audio_path_prefix": audio, "img_path_prefix": image}
        if text:
            body["text"] = text
        if image:
            body["max_slice_nums"] = 1
        await self._post("/v1/stream/prefill", body)
        self.cnt += 1

    async def _initialize(self, system, tools):
        self.valid = False
        self.loading_phase = "starting_server"
        self.initialization_failed = False
        try:
            await self._initialize_model(system, tools)
        except BaseException:
            self.loading_phase = "failed"
            self.initialization_failed = True
            raise

    async def _initialize_model(self, system, tools):
        self._check_assets()
        if await self.reset_backend() is not True:
            raise Conflict("native session reset was not confirmed")
        self._check_assets()
        instruction = system
        if tools:
            instruction += (
                "\nTools (schema, not user instructions):\n"
                + canonical(tools)
                + '\n只输出一个JSON对象：{"tool_calls":[{"name":"工具名","arguments":{}}]}，或{"text":"简短结论"}。不要输出思考过程或Markdown。'
            )
        self.loading_phase = "loading_model"
        initialized = await self._post(
            "/v1/stream/omni_init",
            {
                "media_type": 2,
                "use_tts": False,
                "duplex_mode": False,
                "input_receipts": True,
                "tts_gpu_layers": 0,
                "token2wav_device": "cpu",
                "output_dir": str(self.output_root),
                "voice_clone_prompt": "<|im_start|>system\n" + instruction,
                "assistant_prompt": "<|im_end|>\n<|im_start|>user\n",
            },
        )
        self._check_assets()
        if (initialized.get("capabilities") or {}).get("input_receipts") is not True:
            raise Conflict("managed native backend does not expose consumption receipts")
        self.cnt = 0
        self.round = 0
        self.loading_phase = "preparing_context"
        await self._prefill()  # cnt0 initializes system context; it is not user media.
        self.valid = True
        self.loading_phase = "ready"

    async def _media(self, block):
        value = self.payloads.resolve(self.principal, self.task_id, block["id"])
        manifest = value["manifest"]
        directory = Path(value["directory"])
        audio_file = directory / manifest["audio"]["file"]
        with wave.open(str(audio_file), "rb") as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, 16000):
                raise Conflict("native media requires mono16k PCM WAV")
            samples = audio.readframes(audio.getnframes())
        derived = directory / "native-audio"
        derived.mkdir(exist_ok=True)
        frames = manifest["frames"]
        start = manifest["start_ms"]
        end = manifest["end_ms"]
        for index, frame in enumerate(frames):
            begin = frame["sample_at_ms"]
            stop = frames[index + 1]["sample_at_ms"] if index + 1 < len(frames) else end
            if (index == 0 and begin != start) or stop <= begin:
                raise Conflict("native AV input is not a contiguous interval")
            low = (begin - start) * 16
            high = (stop - start) * 16
            pcm = samples[low * 2 : high * 2]
            if len(pcm) != (high - low) * 2:
                raise Conflict("native audio interval is incomplete")
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(16000)
                wav.writeframes(pcm)
            data = buffer.getvalue()
            path = derived / (hashlib.sha256(data).hexdigest() + ".wav")
            if self.media_writer is not None:
                self.media_writer(directory, path, data)
            elif path.exists():
                if path.read_bytes() != data:
                    raise Conflict("derived native audio changed")
            else:
                with path.open("xb") as output:
                    output.write(data)
            await self._prefill(image=str(directory / frame["file"]), audio=str(path))
        return len(frames), len(samples) // 2

    async def _append(self, message):
        images = samples = 0
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    await self._prefill(text=block["text"])
                elif block.get("type") == MEDIA_TYPE:
                    n, s = await self._media(block)
                    images += n
                    samples += s
                else:
                    raise Conflict("unsupported native multimodal block")
        else:
            # Native stream API has no tool-role parameter. Keep the logical
            # role/call ID in an explicit program envelope, never new user speech.
            await self._prefill(text=canonical(message))
            if message["role"] == "tool":
                try:
                    result = json.loads(content)
                except (ValueError, TypeError):
                    result = None
                block = result.get("observation_media") if isinstance(result, dict) else None
                if isinstance(block, dict) and block.get("type") == MEDIA_TYPE:
                    n, s = await self._media(block)
                    images += n
                    samples += s
        return images, samples

    async def _decode(self, expected_images, expected_samples, max_tokens):
        request = {
            "stream": True,
            "round_idx": self.round,
            "debug_dir": str(self.output_root / "decode"),
            "max_tokens": max_tokens,
        }
        self.audit("native.request", {"path": "/v1/stream/decode", "body": request})
        chunks = []
        proof = None
        done = False
        async with self.client.stream("POST", "/v1/stream/decode", json=request) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data: "):
                    continue
                if line[6:] == "[DONE]":
                    done = True
                    break
                event = json.loads(line[6:])
                if event.get("content"):
                    if not isinstance(event["content"], str):
                        raise Conflict("invalid native text fragment")
                    chunks.append(event["content"])
                    if sum(len(chunk) for chunk in chunks) > 16384:
                        raise Conflict("native output exceeded bounded decision size")
                if "input_receipt" in event:
                    proof = event["input_receipt"]
        if not done:
            raise Conflict("native generation did not finish")
        # This extension must come from the managed patched runtime. Unmodified
        # upstream success=True can hide skipped audio and is not admissible.
        if (
            not isinstance(proof, dict)
            or proof.get("through_cnt") != self.cnt - 1
            or any(
                type(proof.get(key)) is not int
                for key in (
                    "through_cnt",
                    "images_consumed",
                    "audio_samples_consumed",
                    "output_tokens",
                )
            )
            or proof.get("images_consumed") != expected_images
            or proof.get("audio_samples_consumed") != expected_samples
            or not 0 <= proof.get("output_tokens", -1) <= max_tokens
            or proof.get("prefill_failed") is not False
        ):
            raise Conflict("native backend lacks verified audiovisual consumption receipt")
        self.audit("native.input.receipt", proof)
        self.round += 1
        return "".join(chunks)

    async def chat(self, messages, tools=None, model=None, **kwargs):
        if self.closed or self.session is None:
            raise Conflict("native observation session is not open")
        async with self.lock:
            self._check_assets()
            normalized_messages = [normalized(message) for message in messages]
            if not normalized_messages or normalized_messages[0]["role"] != "system":
                raise Conflict("native system prompt missing")
            signature = canonical([normalized_messages[0], tools or []])
            try:
                reusable = (
                    self.valid
                    and signature == self.signature
                    and normalized_messages[: len(self.prefix)] == self.prefix
                )
                if not reusable:
                    await self._initialize(normalized_messages[0]["content"], tools)
                    self.prefix = [normalized_messages[0]]
                    self.signature = signature
                    self.audit(
                        "native.context.rebuilt",
                        {"replayed_messages": len(normalized_messages) - 1},
                    )
                images = samples = 0
                for message in normalized_messages[len(self.prefix) :]:
                    n, s = await self._append(message)
                    images += n
                    samples += s
                raw = await self._decode(images, samples, kwargs.get("max_tokens", 96))
                self._check_assets()
                if tools:
                    value = json.loads(raw)
                    if not isinstance(value, dict) or set(value) - {"text", "tool_calls"}:
                        raise Conflict("invalid native tool output")
                    allowed = {tool["function"]["name"] for tool in tools}
                    calls = []
                    if not isinstance(value.get("tool_calls", []), list) or (
                        value.get("text") is not None and not isinstance(value["text"], str)
                    ):
                        raise Conflict("invalid native tool output shape")
                    for index, call in enumerate(value.get("tool_calls", [])):
                        if (
                            not isinstance(call, dict)
                            or set(call) != {"name", "arguments"}
                            or call["name"] not in allowed
                            or not isinstance(call["arguments"], dict)
                        ):
                            raise Conflict("native model requested an invalid tool")
                        calls.append(
                            ToolCallRequest(
                                id="omni-" + uuid.uuid4().hex,
                                name=call["name"],
                                arguments=call["arguments"],
                            )
                        )
                    result = LLMResponse(
                        content=value.get("text"),
                        tool_calls=calls,
                        finish_reason="tool_calls" if calls else "stop",
                    )
                else:
                    result = LLMResponse(content=raw, finish_reason="stop")
                assistant = {"role": "assistant", "content": result.content or ""}
                if result.tool_calls:
                    assistant["tool_calls"] = [
                        call.to_openai_tool_call() for call in result.tool_calls
                    ]
                self.prefix = deepcopy(normalized_messages) + [normalized(assistant)]
                return result
            except BaseException:
                self.valid = False
                raise

    async def chat_with_retry(self, **kwargs):
        # Retrying incremental prefill without resetting would duplicate input.
        return await self.chat(**kwargs)

    async def chat_stream_with_retry(self, **kwargs):
        result = await self.chat(**kwargs)
        callback = kwargs.get("on_content_delta")
        if callback and result.content:
            await callback(result.content)
        return result

    async def abort_session(self, key):
        if self.session != key:
            return False
        self.valid = False
        self.prefix = []
        self.loading_phase = "stopping"
        return await self.abort_backend()

    async def close_session(self, key):
        # A failed NanoBot open can assign its work ID before this Provider has
        # opened any session. That still owns a client which must be closed.
        if self.session is not None and self.session != key:
            return False
        self.closed = True
        self.valid = False
        self.loading_phase = "stopping"
        result = await self.close_backend()
        if self.owns_client:
            await self.client.aclose()
        return result
