"""Reuse NanoBot's Feishu channel transport, not its agent loop."""

from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.feishu.runtime import FeishuChannel
from nanobot.channels.feishu.config import FeishuConfig
from nanobot.channels.feishu.instances import feishu_app_identity_key
from nanobot.config.loader import set_config_path, get_config_path
from .contracts import digest


class MuseFeishuChannel(FeishuChannel):
    def __init__(self, settings, bus, pending):
        super().__init__(settings, bus)
        self.pending_sender = pending
        self.delivery_id = None
        self.sent_ids = []

    def is_allowed(self, sender_id):
        return str(sender_id) in self.config.allow_from

    async def _handle_message(self, sender_id, chat_id, content, **kwargs):
        if not self.is_allowed(sender_id):
            self.pending_sender(str(sender_id))
            return  # No automatic external pairing message or model call.
        return await super()._handle_message(sender_id, chat_id, content, **kwargs)

    async def _on_message(self, data):
        if data.event.message.chat_type != "p2p":
            return  # This first delivery supports owner DMs, not shared group context.
        await super()._on_message(data)

    async def _add_reaction(self, *args, **kwargs):
        return None

    async def transcribe_audio(self, file_path):
        return ""  # Do not silently send audio to an unrelated transcription service.

    @staticmethod
    def _register_optional_event(builder, method_name, handler):
        return builder  # Receive messages only; no extra reactions/read notifications.

    def _send_message_sync(self, receive_id_type, receive_id, msg_type, content):
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

        ident = digest([self.delivery_id, len(self.sent_ids)])[:40]
        body = (
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type(msg_type)
            .content(content)
            .uuid(ident)
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type(receive_id_type)
            .request_body(body)
            .build()
        )
        response = self._client.im.v1.message.create(request)
        if not response.success() or not getattr(response.data, "message_id", None):
            raise RuntimeError(f"Feishu message delivery failed: {response.code}")
        self.sent_ids.append(response.data.message_id)
        return response.data.message_id

    def _upload_file_sync(self, file_path):
        key = super()._upload_file_sync(file_path)
        if not key:
            raise RuntimeError("Feishu file upload failed")
        return key

    def _upload_image_sync(self, file_path):
        key = super()._upload_image_sync(file_path)
        if not key:
            raise RuntimeError("Feishu image upload failed")
        return key


class FeishuTransport:
    def __init__(self, config, folder, pending):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        native = {
            "appId": config.app_id,
            "appSecret": config.app_secret.get_secret_value(),
            "enabled": True,
            "allowFrom": config.allowed_user_ids,
            "streaming": False,
            "topicIsolation": False,
            "replyToMessage": False,
            "identityKey": feishu_app_identity_key(config.app_id, "feishu"),
        }
        existing = get_config_path().resolve()
        path = (
            existing
            if existing.is_relative_to(folder.parent.parent.resolve()) and existing.is_file()
            else folder / "config.json"
        )
        data = json.loads(path.read_text()) if path.exists() else {}
        data.setdefault("channels", {})["feishu"] = native
        path.write_text(json.dumps(data, ensure_ascii=False))
        path.chmod(0o600)
        set_config_path(path)  # Cloud is one process; all channel files stay in this project.
        from loguru import logger

        logger.disable("nanobot")
        # The SDK logs its credential-bearing WebSocket URL at INFO. Status is exposed separately.
        logging.getLogger("Lark").disabled = True
        self.bus = MessageBus()
        self.channel = MuseFeishuChannel(FeishuConfig.model_validate(native), self.bus, pending)
        self.task = None

    async def start(self):
        self.task = asyncio.create_task(self.channel.start())

    @property
    def connected(self):
        conn = getattr(self.channel._ws_client, "_conn", None)
        return conn is not None and (
            getattr(getattr(conn, "state", None), "name", None) == "OPEN"
            or getattr(conn, "closed", None) is False
        )

    async def receive(self):
        return await self.bus.consume_inbound()

    async def send(self, ident, chat_id, text, media):
        if not self.connected or self.channel._client is None:
            raise RuntimeError("Feishu is not connected")
        self.channel.delivery_id, self.channel.sent_ids = ident, []
        await self.channel.send(
            OutboundMessage(channel="feishu", chat_id=chat_id, content=text, media=media)
        )
        if not self.channel.sent_ids:
            raise RuntimeError("Feishu returned no message receipt")
        return {"message_ids": list(self.channel.sent_ids), "media_files": len(media)}

    async def close(self):
        await self.channel.stop()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
