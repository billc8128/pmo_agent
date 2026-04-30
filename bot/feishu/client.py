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

    # ── identify ourselves ──────────────────────────────────────────────

    async def fetch_self_info(self) -> Optional[dict]:
        """Look up the bot's own identity via /open-apis/bot/v3/info.

        Returns a dict like {"bot_name": "包工头", "open_id": "ou_..."}
        or None on failure. Used at startup to populate the @-mention
        check with the bot's actual open_id, regardless of what the
        admin named the app.

        We hit the HTTP endpoint directly via httpx because lark-oapi's
        Python type stubs for bot.v3 are inconsistent across versions
        (the GetBotInfoRequest module path moved). The token plumbing
        is the only thing the SDK gives us; we reuse it via a tiny
        dance.
        """
        import httpx
        # Use the SDK's tenant_access_token issuer so we don't have to
        # re-implement caching / refresh.
        try:
            token = self.client.config.app_settings.app_secret  # type: ignore[attr-defined]
        except Exception:
            token = None
        # Simpler: hit auth API ourselves.
        try:
            async with httpx.AsyncClient(timeout=10.0) as ac:
                # 1. Get tenant_access_token
                auth_resp = await ac.post(
                    "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                    json={
                        "app_id": settings.feishu_app_id,
                        "app_secret": settings.feishu_app_secret,
                    },
                )
                auth_resp.raise_for_status()
                auth = auth_resp.json()
                if auth.get("code") != 0:
                    logger.warning("auth failed: %s", auth)
                    return None
                tat = auth["tenant_access_token"]
                # 2. Look up our identity
                info_resp = await ac.get(
                    "https://open.feishu.cn/open-apis/bot/v3/info",
                    headers={"Authorization": f"Bearer {tat}"},
                )
                info_resp.raise_for_status()
                info = info_resp.json()
                if info.get("code") != 0:
                    logger.warning("bot info failed: %s", info)
                    return None
                bot = info.get("bot") or {}
                return {
                    "open_id": bot.get("open_id"),
                    "app_name": bot.get("app_name"),
                    "bot_name": bot.get("app_name"),
                }
        except Exception as e:
            logger.warning("fetch_self_info crashed: %s", e)
            return None

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

    async def reply_image(self, parent_message_id: str, image_key: str) -> Optional[str]:
        """Threaded reply with a single image (msg_type=image)."""
        body = ReplyMessageRequestBody.builder() \
            .msg_type("image") \
            .content(json.dumps({"image_key": image_key}, ensure_ascii=False)) \
            .build()
        req = ReplyMessageRequest.builder() \
            .message_id(parent_message_id) \
            .request_body(body) \
            .build()
        resp = self.client.im.v1.message.reply(req)
        if not resp.success():
            logger.warning("feishu reply image failed: code=%s msg=%s", resp.code, resp.msg)
            return None
        return resp.data.message_id if resp.data else None

    async def reply_post(self, parent_message_id: str, post_content: dict) -> Optional[str]:
        """Threaded reply with a Feishu `post` rich-text message.

        post_content is the dict returned by post_format.markdown_to_post.
        """
        body = ReplyMessageRequestBody.builder() \
            .msg_type("post") \
            .content(json.dumps(post_content, ensure_ascii=False)) \
            .build()
        req = ReplyMessageRequest.builder() \
            .message_id(parent_message_id) \
            .request_body(body) \
            .build()
        resp = self.client.im.v1.message.reply(req)
        if not resp.success():
            logger.warning("feishu reply post failed: code=%s msg=%s", resp.code, resp.msg)
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
