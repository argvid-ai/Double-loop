"""Owner-authenticated workbench access to the local voice device adapter."""

from pathlib import Path

import httpx
from fastapi import Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import Field

from .voice_device import VoiceDeviceSettings
from .contracts import Contract
from .harness_contracts import WakeConfiguration
from .voice_wake import match_wake


class WakePreview(Contract):
    text: str = Field(min_length=1, max_length=500)
    wake_words: list[str] = Field(min_length=1, max_length=5)


def mount_voice_device_proxy(app, host, node_auth):
    @app.post("/harness/voice/wake-preview", dependencies=[Depends(node_auth)])
    async def preview(body: WakePreview):
        config = WakeConfiguration(wake_words=body.wake_words)
        word, command = match_wake(body.text, config.wake_words)
        return {"matched": word is not None, "wake_word": word, "request_text": command,
                "simulated": True, "submitted": False, "acoustic_wake_verified": False,
                "meaning": "只检查输入文字的唤醒前缀，不读麦克风、不提交主对话、不播放"}

    async def request(method, path, body=None):
        url = host.settings.voice_device_url
        token_file = host.settings.voice_device_token_file
        if host.side != "cloud" or not url or not token_file:
            if path == "/status":
                return {"installed": False, "state": "not_configured"}
            raise HTTPException(409, "板端语音服务尚未配置")
        try:
            token = Path(token_file).read_text().strip()
            async with httpx.AsyncClient(trust_env=False, timeout=8) as client:
                response = await client.request(method, url + path, json=body,
                                                headers={"Authorization": "Bearer " + token})
            if response.status_code >= 500:
                raise HTTPException(503, "语音服务暂不可用，录像不受影响")
            value = response.json()
            if response.status_code >= 400:
                return JSONResponse(value, status_code=response.status_code)
            return {**value, "installed": True}
        except (OSError, httpx.HTTPError, ValueError):
            raise HTTPException(503, "暂时连不上板端语音服务，正在等待恢复；录像不受影响") from None

    @app.get("/harness/voice/device", dependencies=[Depends(node_auth)])
    async def status():
        return await request("GET", "/status")

    @app.put("/harness/voice/device/config", dependencies=[Depends(node_auth)])
    async def configure(body: VoiceDeviceSettings):
        return await request("PUT", "/config", body.model_dump())
