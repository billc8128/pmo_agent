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
    plaintext = cipher.decrypt_str(body["encrypt"])
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
    # contains a "mentions" field; the text has tokens like
    # @_user_1 / @_user_2 — placeholders for the mentioned users.
    # We strip the placeholders for clean text, and check if any
    # mention resolves to OUR bot.
    #
    # We compare against the cached self open_id (set at startup via
    # set_self_identity). Falling back to a name match isn't reliable —
    # admins can rename the app, and "name" in the mentions payload
    # may also include @-mentions of human users with similar names.
    mentions = msg.get("mentions") or []
    self_oid = _self_open_id_cached()
    self_name = _self_name_cached()
    is_at_bot = False
    for m in mentions:
        m_oid = (m.get("id") or {}).get("open_id")
        m_name = m.get("name")
        if self_oid and m_oid == self_oid:
            is_at_bot = True
            break
        # Fallback: name match — used in groups where the open_id field
        # might be missing or before set_self_identity has run.
        if self_name and m_name == self_name:
            is_at_bot = True
            break
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


# Cache the bot's own identity, populated at startup via the
# /open-apis/bot/v3/info call (see app.py lifespan). Both fields
# can be None if startup lookup failed; the @-mention check handles
# that gracefully (it just won't match).
_self_open_id: Optional[str] = None
_self_name: Optional[str] = None


def set_self_identity(*, open_id: Optional[str], name: Optional[str]) -> None:
    """Called once at app startup with the bot's own info."""
    global _self_open_id, _self_name
    _self_open_id = open_id
    _self_name = name


def _self_open_id_cached() -> Optional[str]:
    return _self_open_id


def _self_name_cached() -> Optional[str]:
    return _self_name
