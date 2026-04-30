"""Thin wrapper around lark-oapi — only the operations the bot needs."""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    Emoji,
    PatchMessageRequest,
    PatchMessageRequestBody,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
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

    # ── reactions: lightweight "I see you" acks ─────────────────────────

    async def add_reaction(self, message_id: str, emoji_type: str = "Get") -> bool:
        """Add an emoji reaction to a user's message.

        Default "Get" — the yellow emoji with "GET" written on it —
        reads as "received" / "got it". Note the emoji_type is
        case-sensitive: Feishu wants "Get", not "GET" (which returns
        code 231001 'reaction type is invalid').
        """
        body = CreateMessageReactionRequestBody.builder() \
            .reaction_type(Emoji.builder().emoji_type(emoji_type).build()) \
            .build()
        req = CreateMessageReactionRequest.builder() \
            .message_id(message_id) \
            .request_body(body) \
            .build()
        resp = self.client.im.v1.message_reaction.create(req)
        if not resp.success():
            logger.warning(
                "feishu reaction failed (%s on %s): code=%s msg=%s",
                emoji_type, message_id, resp.code, resp.msg,
            )
            return False
        return True

    # ── send messages ────────────────────────────────────────────────────

    async def reply_text(self, parent_message_id: str, text: str) -> Optional[str]:
        """Threaded text reply — used as a fallback when card sending fails."""
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
            logger.warning("feishu reply text failed: code=%s msg=%s", resp.code, resp.msg)
            return None
        return resp.data.message_id if resp.data else None

    async def reply_card(self, parent_message_id: str, card: dict) -> Optional[str]:
        """Threaded card reply. card is the full card schema dict.

        Returns the new message_id, which is what `patch_card` uses to
        update this card in place as the agent makes progress.
        """
        body = ReplyMessageRequestBody.builder() \
            .msg_type("interactive") \
            .content(json.dumps(card, ensure_ascii=False)) \
            .build()
        req = ReplyMessageRequest.builder() \
            .message_id(parent_message_id) \
            .request_body(body) \
            .build()
        resp = self.client.im.v1.message.reply(req)
        if not resp.success():
            logger.warning("feishu reply card failed: code=%s msg=%s", resp.code, resp.msg)
            return None
        return resp.data.message_id if resp.data else None

    # ── update an in-flight card ─────────────────────────────────────────

    async def patch_card(self, message_id: str, card: dict) -> bool:
        """Replace the contents of an existing card message.

        Feishu rate-limits PatchMessage to roughly 5/sec per app; the
        agent runner throttles updates to 1/sec to stay well clear.
        """
        body = PatchMessageRequestBody.builder() \
            .content(json.dumps(card, ensure_ascii=False)) \
            .build()
        req = PatchMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(body) \
            .build()
        resp = self.client.im.v1.message.patch(req)
        if not resp.success():
            logger.warning("feishu patch card failed: code=%s msg=%s", resp.code, resp.msg)
            return False
        return True

    async def patch_text(self, message_id: str, text: str) -> bool:
        """Patch a plain-text message. Kept for backwards compat."""
        body = PatchMessageRequestBody.builder() \
            .content(json.dumps({"text": text}, ensure_ascii=False)) \
            .build()
        req = PatchMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(body) \
            .build()
        resp = self.client.im.v1.message.patch(req)
        if not resp.success():
            logger.warning("feishu patch text failed: code=%s msg=%s", resp.code, resp.msg)
            return False
        return True


feishu_client = FeishuClient()
