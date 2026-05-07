"""Parse Feishu webhook events.

We rely on lark-oapi to handle URL-verification, AES decryption, and
event signature validation. Above that, this module exposes a small
typed representation of what business code cares about.

The parser preserves text, rich text, and safe shared-resource metadata for
chat memory. Only conversational message types are allowed to invoke the agent:
  - p2p_chat:    user DM'd the bot with text/rich text.
  - group_chat:  user @-mentioned the bot with text/rich text.
Non-conversational group messages can still be stored when chat memory is
enabled, but they must not be treated as user questions.
"""
from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

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
    parent_message_id: str
    text: str                 # whitespace-trimmed, @-mentions stripped
    is_at_bot: bool
    occurred_at: Optional[str] = None
    sender_display_name: Optional[str] = None
    root_message_id: str = ""
    mentions: list[dict[str, Any]] = field(default_factory=list)
    message_type: str = "text"
    content_metadata: dict[str, Any] = field(default_factory=dict)
    is_conversational: bool = False
    bot_identity_ready: bool = True
    sender_is_bot: bool = False


@dataclass
class ParsedMessageMutationEvent:
    event_id: str
    chat_id: str
    chat_type: str
    message_id: str
    action: str  # "recall" | "edit"
    text: str = ""
    occurred_at: Optional[str] = None
    message_type: str = "text"
    content_metadata: dict[str, Any] = field(default_factory=dict)


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
    """Returns None if this event isn't a user-facing message we care about."""
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

    msg_type = msg.get("message_type") or "text"
    raw_content = msg.get("content") or "{}"
    try:
        content = json.loads(raw_content)
    except Exception:
        return None

    text, content_metadata = _extract_message_text_and_metadata(msg_type, content)
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
    raw_mentions = msg.get("mentions") or []
    mentions = _normalize_mentions(raw_mentions)
    self_oid = _self_open_id_cached()
    bot_identity_ready = chat_type != "group" or bool(self_oid)
    is_at_bot = False
    if bot_identity_ready:
        for m in mentions:
            m_oid = m.get("open_id")
            if self_oid and m_oid == self_oid:
                is_at_bot = True
                break
    else:
        logger.warning("group message parsed before bot self open_id was ready: chat=%s", chat_id)
    # Strip mention placeholders from text. Do this even when identity is
    # unavailable so any stored memory does not preserve Feishu placeholder
    # tokens.
    text = re.sub(r"@_user_\d+", "", text)
    text = re.sub(r"\s+", " ", text).strip()

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
        parent_message_id=msg.get("parent_id") or "",
        text=text,
        is_at_bot=is_at_bot,
        occurred_at=_parse_feishu_time(msg.get("create_time") or event.get("create_time")),
        sender_display_name=_sender_display_name(sender),
        root_message_id=msg.get("root_id") or msg.get("parent_id") or "",
        mentions=mentions,
        message_type=msg_type,
        content_metadata=content_metadata,
        is_conversational=msg_type in {"text", "post"},
        bot_identity_ready=bot_identity_ready,
        sender_is_bot=bool(self_oid and sender_open_id == self_oid),
    )


def parse_message_mutation_event(body: dict) -> Optional[ParsedMessageMutationEvent]:
    header = body.get("header") or {}
    event = body.get("event") or {}
    event_type = header.get("event_type")
    action = _mutation_action(event_type)
    if action is None:
        return None

    msg = event.get("message") or {}
    chat_id = msg.get("chat_id", "")
    message_id = msg.get("message_id") or msg.get("old_message_id") or ""
    if not chat_id or not message_id:
        return None
    msg_type = msg.get("message_type") or "text"
    text = ""
    content_metadata: dict[str, Any] = {}
    if action == "edit":
        raw_content = msg.get("content") or "{}"
        try:
            content = json.loads(raw_content)
        except Exception:
            return None
        text, content_metadata = _extract_message_text_and_metadata(msg_type, content)
        text = re.sub(r"@_user_\d+", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return None
    return ParsedMessageMutationEvent(
        event_id=header.get("event_id") or "",
        chat_id=chat_id,
        chat_type="p2p" if msg.get("chat_type") == "p2p" else "group",
        message_id=message_id,
        action=action,
        text=text,
        occurred_at=_parse_feishu_time(msg.get("create_time") or event.get("create_time")),
        message_type=msg_type,
        content_metadata=content_metadata,
    )


def _mutation_action(event_type: Any) -> Optional[str]:
    if event_type in {"im.message.recalled_v1", "im.message.recall_v1"}:
        return "recall"
    if event_type in {
        "im.message.updated_v1",
        "im.message.message_updated_v1",
        "im.message.edited_v1",
    }:
        return "edit"
    return None


def _parse_feishu_time(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        raw = int(str(value))
    except (TypeError, ValueError):
        return None
    # Feishu message create_time is milliseconds since epoch. Be permissive
    # for second-based fixtures.
    if raw > 10_000_000_000:
        seconds = raw / 1000
    else:
        seconds = raw
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _sender_display_name(sender: dict[str, Any]) -> Optional[str]:
    for key in ("sender_name", "name", "display_name"):
        value = sender.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _normalize_mentions(raw_mentions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for mention in raw_mentions:
        mention_id = mention.get("id") or {}
        item = {
            "open_id": mention_id.get("open_id"),
            "user_id": mention_id.get("user_id"),
            "union_id": mention_id.get("union_id"),
            "name": mention.get("name"),
            "key": mention.get("key"),
        }
        out.append({k: v for k, v in item.items() if v})
    return out


def _extract_message_text_and_metadata(msg_type: str, content: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if msg_type == "text":
        return (content.get("text") or "").strip(), {}
    if msg_type == "post":
        return _extract_post_text_and_metadata(content)
    if msg_type == "file":
        file_name = str(content.get("file_name") or content.get("name") or "").strip()
        if not file_name:
            return "", {}
        metadata: dict[str, Any] = {"file_name": file_name}
        if isinstance(content.get("size"), int):
            metadata["size"] = content["size"]
        return f"Shared file: {file_name}", metadata
    if msg_type == "share_chat":
        title = str(content.get("title") or content.get("chat_name") or "Shared chat").strip()
        metadata = {
            k: v
            for k, v in {
                "title": title,
                "chat_id": content.get("chat_id"),
            }.items()
            if v
        }
        return f"Shared chat: {title}", metadata
    if msg_type == "share_user":
        name = str(content.get("name") or content.get("user_name") or "Shared user").strip()
        return f"Shared user: {name}", {"name": name}
    if msg_type == "link":
        title = str(content.get("title") or content.get("name") or "Shared link").strip()
        url = str(content.get("url") or content.get("href") or content.get("link") or "").strip()
        metadata = {k: v for k, v in {"title": title, "url": url}.items() if v}
        return f"Shared link: {title}", metadata
    return "", {}


def _extract_post_text_and_metadata(content: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    parts: list[str] = []
    links: list[dict[str, str]] = []
    title = content.get("title")
    if isinstance(title, str) and title.strip():
        parts.append(title.strip())

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        tag = node.get("tag")
        text = node.get("text") or node.get("name")
        if isinstance(text, str) and text.strip():
            parts.append(text.strip())
        if tag == "a" and isinstance(node.get("href"), str):
            links.append({"text": str(text or node["href"]), "href": node["href"]})
        for key in ("content", "elements"):
            if key in node:
                visit(node[key])

    visit(content.get("content") or [])
    metadata: dict[str, Any] = {}
    if links:
        metadata["links"] = links
    return " ".join(parts).strip(), metadata


# Cache the bot's own identity, populated at startup via the
# /open-apis/bot/v3/info call (see app.py lifespan). Both fields
# can be None if startup lookup failed; group webhook handling treats
# missing open_id as "not ready" so we do not store or answer with a
# biased @-mention decision.
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
