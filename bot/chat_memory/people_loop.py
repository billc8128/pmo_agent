from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from chat_memory import people
from config import settings
from db import queries

logger = logging.getLogger(__name__)

_MODEL_NAME = "deterministic_people_memory_v1"


def _day_ago_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()


def _debounce_since_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=settings.people_memory_debounce_minutes)).isoformat()


def _row_person_key(row: dict[str, Any]) -> str:
    return people.person_key(
        profile_id=row.get("sender_user_id"),
        feishu_open_id=row.get("sender_open_id"),
    )


def _display_name_for_messages(messages: list[dict[str, Any]]) -> str:
    for msg in messages:
        if msg.get("sender_display_name"):
            return str(msg["sender_display_name"])
    return "这位成员"


def _upsert_note_for_messages(person_key: str, messages: list[dict[str, Any]]) -> bool:
    existing = queries.get_people_memory(person_key) or {}
    old_note = existing.get("pmo_notes") or ""
    first = messages[0]
    last = messages[-1]
    new_note = people.compose_background_note(
        display_name=_display_name_for_messages(messages),
        existing_note=old_note,
        messages=messages,
    )
    if not new_note or new_note == old_note:
        return False
    payload = {
        **existing,
        "person_key": person_key,
        "profile_id": first.get("sender_user_id") or existing.get("profile_id"),
        "feishu_open_id": first.get("sender_open_id") or existing.get("feishu_open_id"),
        "display_name": _display_name_for_messages(messages) or existing.get("display_name"),
        "handle": existing.get("handle"),
        "pmo_notes": new_note,
        "notes_updated_at": datetime.now(timezone.utc).isoformat(),
        "last_observed_at": last.get("occurred_at"),
        "metadata": {
            **(existing.get("metadata") or {}),
            "last_source": "chat_memory_background_loop",
        },
    }
    queries.upsert_people_memory(payload)
    queries.record_people_memory_update(
        person_key,
        source="background_loop",
        old_note=old_note,
        new_note=new_note,
        model=_MODEL_NAME,
    )
    return True


def run_once(
    *,
    max_chats: int | None = None,
    min_messages: int | None = None,
    daily_cap: int | None = None,
) -> dict[str, Any]:
    max_chats = max_chats if max_chats is not None else settings.people_memory_max_chats_per_run
    min_messages = min_messages if min_messages is not None else settings.people_memory_min_messages
    daily_cap = daily_cap if daily_cap is not None else settings.people_memory_daily_update_cap
    already_used = queries.recent_people_memory_update_count("background_loop", since_iso=_day_ago_iso())
    remaining = max(0, int(daily_cap) - already_used)
    if remaining <= 0:
        return {"chats": 0, "updated_people": 0, "skipped": "daily_cap"}

    updated_people = 0
    chats_seen = 0
    settings_rows = queries.enabled_chat_memory_settings_for_people_loop(limit=int(max_chats))
    for setting in settings_rows:
        chat_id = setting.get("chat_id")
        if not chat_id:
            continue
        chats_seen += 1
        rows = queries.chat_messages_after_cursor(
            chat_id,
            cursor=setting.get("people_loop_cursor"),
            limit=settings.people_memory_max_messages_per_chat,
        )
        if not rows:
            continue
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            if not row.get("sender_open_id") and not row.get("sender_user_id"):
                continue
            try:
                grouped[_row_person_key(row)].append(row)
            except ValueError:
                continue
        for person_key, messages in grouped.items():
            if updated_people >= remaining:
                break
            if len(messages) < int(min_messages):
                continue
            if queries.recent_people_memory_update_for_person(person_key, since_iso=_debounce_since_iso()):
                continue
            if _upsert_note_for_messages(person_key, messages):
                updated_people += 1
        latest = max((str(row.get("occurred_at") or "") for row in rows), default="")
        if latest:
            queries.advance_people_loop_cursor(chat_id, latest)

    logger.info(
        "people_memory_loop_run chats=%d updated_people=%d remaining_cap=%d",
        chats_seen,
        updated_people,
        max(0, remaining - updated_people),
    )
    return {"chats": chats_seen, "updated_people": updated_people}


async def run_forever() -> None:
    if not settings.people_memory_loop_enabled:
        logger.info("people memory loop disabled")
        return
    while True:
        try:
            run_once()
            await asyncio.sleep(settings.people_memory_loop_interval_seconds)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning("people memory loop error: %s", e)
            await asyncio.sleep(settings.people_memory_loop_interval_seconds)
