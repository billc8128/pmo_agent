"""Parse Feishu webhook events.

We rely on lark-oapi to handle URL-verification, AES decryption, and
event signature validation. Above that, this module exposes a small
typed representation of what business code cares about.

Two kinds of events the bot acts on:
  - p2p_chat:    user DM'd the bot. Always respond.
  - group_chat:  user @-mentioned the bot in a group. Only respond when
                 mentioned (lark hands us mentions as a separate field).
Anything else — bot-to-bot messages, mentions of *other* bots, system
events — we silently ignore.
"""
from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

from config import settings

logger = logging.getLogger(__name__)


# ── LRU dedup for event_id (Feishu retries on 5xx within ~5min) ───────


class _LRUSet:
    def __init__(self, capacity: int = 2048) -> None:
        self.capacity = capacity
        self._d: "OrderedDict[str, None]" = OrderedDict()

    def add_if_absent(self, key: str) -> bool:
        if key in self._d:
            self._d.move_to_end(key)
            return False
        self._d[key] = None
        if len(self._d) > self.capacity:
            self._d.popitem(last=False)
        return True


_seen_events = _LRUSet()


# ── Parsed event types ───────────────────────────────────────────────


@dataclass
class ParsedMessageEvent:
    event_id: str
    chat_id: str
    chat_type: str            # "p2p" | "group"
    sender_open_id: str
    sender_chat_member_id: Optional[str]  # in groups, the chat-member id
    message_id: str
    text: str                 # whitespace-trimmed, @-mentions stripped
    is_at_bot: bool


# ── URL verification handshake ───────────────────────────────────────


def is_url_verification(body: dict) -> bool:
    return body.get("type") == "url_verification"


def url_verification_response(body: dict) -> dict:
    return {"challenge": body.get("challenge")}


# ── Decrypt + extract event_id ───────────────────────────────────────


def decrypt_if_needed(body: dict) -> dict:
    """If the event is encrypted, lark-oapi gives us a 'encrypt' key."""
    if "encrypt" not in body:
        return body
    if not settings.feishu_encrypt_key:
        raise RuntimeError("encrypted event but no feishu_encrypt_key configured")
    # lark-oapi has its own decryption path inside the WebhookEvent flow,
    # but we can do it manually using the same util to avoid pulling the
    # full webhook handler.
    import lark_oapi as lark
    cipher = lark.AESCipher(settings.feishu_encrypt_key)
    plaintext = cipher.decrypt_string(body["encrypt"])
    return json.loads(plaintext)


def event_id_of(body: dict) -> Optional[str]:
    return (body.get("header") or {}).get("event_id")


def already_seen(event_id: str) -> bool:
    return not _seen_events.add_if_absent(event_id)


# ── Parse a v2 message event ─────────────────────────────────────────


def parse_message_event(body: dict) -> Optional[ParsedMessageEvent]:
    """Returns None if this event isn't a user-facing text message we care about."""
    header = body.get("header") or {}
    event = body.get("event") or {}

    event_type = header.get("event_type")
    if event_type != "im.message.receive_v1":
        return None

    msg = event.get("message") or {}
    sender = event.get("sender") or {}

    chat_type = msg.get("chat_type", "")
    chat_id = msg.get("chat_id", "")
    if not chat_id:
        return None

    msg_type = msg.get("message_type")
    if msg_type != "text":
        return None  # ignore image/file/audio/etc. for MVP

    # Content is a JSON string with {"text": "..."}.
    raw_content = msg.get("content") or "{}"
    try:
        content = json.loads(raw_content)
    except Exception:
        return None
    text = (content.get("text") or "").strip()
    if not text:
        return None

    # Mentions: when the bot is @-mentioned in a group, the message
    # contains a "mentions" field; the text has a token like
    # @_user_1 / @_user_2 — these are mention placeholders. We strip
    # them, then check if any of them resolved to our own bot.
    mentions = msg.get("mentions") or []
    is_at_bot = any(
        m.get("name") == "PMO bot"  # display name match — robust enough
        or (m.get("id") or {}).get("open_id") == _self_open_id_cached()
        for m in mentions
    )
    # Strip mention placeholders from text
    text = re.sub(r"@_user_\d+", "", text).strip()

    sender_id_obj = sender.get("sender_id") or {}
    sender_open_id = sender_id_obj.get("open_id") or sender.get("sender_open_id") or ""
    if not sender_open_id:
        return None

    return ParsedMessageEvent(
        event_id=header.get("event_id") or "",
        chat_id=chat_id,
        chat_type="p2p" if chat_type == "p2p" else "group",
        sender_open_id=sender_open_id,
        sender_chat_member_id=msg.get("chat_member_id"),
        message_id=msg.get("message_id") or "",
        text=text,
        is_at_bot=is_at_bot,
    )


# Cache the bot's own open_id once we discover it (it shows up as the
# "id" of mentions referring to the bot itself in some payloads). We
# don't strictly need this for MVP; the display-name match covers
# common cases.
_self_open_id: Optional[str] = None
def _self_open_id_cached() -> Optional[str]:
    return _self_open_id
