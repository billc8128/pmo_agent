"""Thin wrapper around lark-oapi — only the operations the bot needs."""
from __future__ import annotations

import json
import logging
from typing import Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageRequest,
    CreateMessageRequestBody,
    PatchMessageRequest,
    PatchMessageRequestBody,
)

from config import settings

logger = logging.getLogger(__name__)


class FeishuClient:
    def __init__(self) -> None:
        self._client: Optional[lark.Client] = None

    @property
    def client(self) -> lark.Client:
        if self._client is None:
            self._client = (
                lark.Client.builder()
                .app_id(settings.feishu_app_id)
                .app_secret(settings.feishu_app_secret)
                .log_level(lark.LogLevel.WARNING)
                .build()
            )
        return self._client

    # ── send a fresh message ────────────────────────────────────────────

    async def send_text_to_chat(self, chat_id: str, text: str) -> Optional[str]:
        """Reply into a chat (group or p2p). Returns the new message_id."""
        body = CreateMessageRequestBody.builder() \
            .receive_id(chat_id) \
            .msg_type("text") \
            .content(json.dumps({"text": text}, ensure_ascii=False)) \
            .build()
        req = CreateMessageRequest.builder() \
            .receive_id_type("chat_id") \
            .request_body(body) \
            .build()
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            logger.warning("feishu send failed: code=%s msg=%s", resp.code, resp.msg)
            return None
        return resp.data.message_id if resp.data else None

    async def reply_text(self, parent_message_id: str, text: str) -> Optional[str]:
        """Reply threaded to a specific message (preserves group context)."""
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
        body = ReplyMessageRequestBody.builder() \
            .msg_type("text") \
            .content(json.dumps({"text": text}, ensure_ascii=False)) \
            .build()
        req = ReplyMessageRequest.builder() \
            .message_id(parent_message_id) \
            .request_body(body) \
            .build()
        resp = self.client.im.v1.message.reply(req)
        if not resp.success():
            logger.warning("feishu reply failed: code=%s msg=%s", resp.code, resp.msg)
            return None
        return resp.data.message_id if resp.data else None

    # ── edit an in-flight "thinking..." placeholder ─────────────────────

    async def patch_text(self, message_id: str, text: str) -> bool:
        body = PatchMessageRequestBody.builder() \
            .content(json.dumps({"text": text}, ensure_ascii=False)) \
            .build()
        req = PatchMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(body) \
            .build()
        resp = self.client.im.v1.message.patch(req)
        if not resp.success():
            logger.warning("feishu patch failed: code=%s msg=%s", resp.code, resp.msg)
            return False
        return True


feishu_client = FeishuClient()
