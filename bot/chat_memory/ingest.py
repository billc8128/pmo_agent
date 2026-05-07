from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from db import queries
from external.redaction import redact_text_with_categories
from feishu.events import ParsedMessageEvent, ParsedMessageMutationEvent

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 30.0
_enabled_cache: dict[str, tuple[bool, float]] = {}
_METADATA_ONLY_MESSAGE_TYPES = {"file", "share_chat", "link", "unknown"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_set(chat_id: str, enabled: bool) -> None:
    _enabled_cache[chat_id] = (enabled, time.monotonic() + _CACHE_TTL_SECONDS)


def _cache_get(chat_id: str) -> bool | None:
    cached = _enabled_cache.get(chat_id)
    if cached is None:
        return None
    enabled, expires_at = cached
    if expires_at < time.monotonic():
        _enabled_cache.pop(chat_id, None)
        return None
    return enabled


def invalidate_chat_memory_cache(chat_id: str | None = None) -> None:
    if chat_id is None:
        _enabled_cache.clear()
        return
    _enabled_cache.pop(chat_id, None)


def memory_enabled_hint(chat_id: str) -> bool:
    """Fast ACK-path hint. False can mean disabled or unknown."""
    return _cache_get(chat_id) is True


def should_schedule_storage(chat_id: str) -> bool:
    """Return false only for a fresh known-disabled chat.

    On cache miss we schedule a background task that performs the authoritative
    DB check. This keeps webhook ACK fast without permanently missing newly
    enabled chats.
    """
    cached = _cache_get(chat_id)
    return cached is not False


def _redact_for_message_type(text: str, message_type: str) -> tuple[str, dict[str, int]]:
    if message_type in _METADATA_ONLY_MESSAGE_TYPES:
        return "", {}
    text_redacted, categories = redact_text_with_categories(text)
    if not text_redacted.strip():
        text_redacted = "[REDACTED]"
    return text_redacted, categories


def _redacted_payload(
    *,
    text_redacted: str,
    categories: dict[str, int],
    content_metadata: dict[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "text": text_redacted,
        "redaction_count": sum(categories.values()),
        "redaction_categories": categories,
    }
    if content_metadata:
        payload["content_metadata"] = content_metadata
    return payload


async def store_message_if_enabled(ev: ParsedMessageEvent) -> bool:
    if ev.chat_type != "group":
        return False
    if ev.sender_is_bot:
        logger.info(
            "chat_memory_ingest_skipped chat=%s message=%s reason=bot_sender",
            ev.chat_id,
            ev.message_id,
        )
        return False

    enabled = queries.is_chat_memory_enabled(ev.chat_id)
    _cache_set(ev.chat_id, enabled)
    if not enabled:
        logger.info(
            "chat_memory_ingest_skipped chat=%s message=%s reason=disabled",
            ev.chat_id,
            ev.message_id,
        )
        return False

    text_redacted, categories = _redact_for_message_type(ev.text, ev.message_type)
    redacted_payload = _redacted_payload(
        text_redacted=text_redacted,
        categories=categories,
        content_metadata=ev.content_metadata,
    )

    row = {
        "feishu_message_id": ev.message_id,
        "chat_id": ev.chat_id,
        "chat_type": ev.chat_type,
        "sender_open_id": ev.sender_open_id,
        "sender_chat_member_id": ev.sender_chat_member_id,
        "sender_display_name": ev.sender_display_name,
        "message_type": ev.message_type,
        "text_redacted": text_redacted,
        "redacted_payload": redacted_payload,
        "content_metadata": ev.content_metadata,
        "parent_message_id": ev.parent_message_id or None,
        "root_message_id": ev.root_message_id or None,
        "mentions": ev.mentions,
        "is_at_bot": ev.is_at_bot,
        "sender_is_bot": ev.sender_is_bot,
        "occurred_at": ev.occurred_at or _now_iso(),
    }
    queries.insert_chat_message(row)
    logger.info(
        "chat_memory_ingested chat=%s message=%s type=%s redactions=%d categories=%s",
        ev.chat_id,
        ev.message_id,
        ev.message_type,
        sum(categories.values()),
        sorted(categories.keys()),
    )
    return True


async def apply_message_mutation(ev: ParsedMessageMutationEvent) -> bool:
    if ev.action == "recall":
        ok = queries.mark_chat_message_deleted(ev.message_id)
        logger.info(
            "chat_memory_message_recalled chat=%s message=%s applied=%s",
            ev.chat_id,
            ev.message_id,
            bool(ok),
        )
        return bool(ok)
    if ev.action == "edit":
        text_redacted, categories = _redact_for_message_type(ev.text, ev.message_type)
        payload = _redacted_payload(
            text_redacted=text_redacted,
            categories=categories,
            content_metadata=ev.content_metadata,
        )
        ok = queries.update_chat_message_text(
            ev.message_id,
            text_redacted=text_redacted,
            redacted_payload=payload,
            edited_at=ev.occurred_at or _now_iso(),
        )
        logger.info(
            "chat_memory_message_edited chat=%s message=%s applied=%s redactions=%d categories=%s",
            ev.chat_id,
            ev.message_id,
            bool(ok),
            sum(categories.values()),
            sorted(categories.keys()),
        )
        return bool(ok)
    logger.info(
        "chat_memory_message_mutation_skipped chat=%s message=%s action=%s reason=unsupported",
        ev.chat_id,
        ev.message_id,
        ev.action,
    )
    return False
