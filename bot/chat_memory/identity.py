from __future__ import annotations

import logging
from typing import Any

from chat_memory import people
from db import queries

logger = logging.getLogger(__name__)


def _merge_notes(profile_note: str, feishu_note: str) -> str:
    profile_note = people.sanitize_note(profile_note)
    feishu_note = people.sanitize_note(feishu_note)
    if profile_note and feishu_note and profile_note != feishu_note:
        return people.sanitize_note(f"{profile_note}\n补充自绑定前飞书上下文：{feishu_note}")
    return profile_note or feishu_note


def merge_people_memory_identity(
    *,
    profile_id: str,
    feishu_open_id: str,
    display_name: str | None = None,
    handle: str | None = None,
) -> dict[str, Any] | None:
    if not profile_id or not feishu_open_id:
        return None
    profile_key = people.person_key(profile_id=profile_id)
    feishu_key = people.person_key(feishu_open_id=feishu_open_id)
    profile_row = queries.get_people_memory(profile_key)
    feishu_row = queries.get_people_memory(feishu_key)
    if not profile_row and not feishu_row:
        queries.backfill_chat_messages_sender_user_id(feishu_open_id, profile_id)
        return None

    old_note = (profile_row or feishu_row or {}).get("pmo_notes") or ""
    merged_note = _merge_notes(
        (profile_row or {}).get("pmo_notes") or "",
        (feishu_row or {}).get("pmo_notes") or "",
    )
    base = dict(profile_row or feishu_row or {})
    metadata = {
        **((feishu_row or {}).get("metadata") or {}),
        **((profile_row or {}).get("metadata") or {}),
    }
    merge_sources = list(metadata.get("merge_sources") or [])
    if feishu_key not in merge_sources:
        merge_sources.append(feishu_key)
    metadata["merge_sources"] = merge_sources
    payload = {
        **base,
        "person_key": profile_key,
        "profile_id": profile_id,
        "feishu_open_id": feishu_open_id,
        "display_name": display_name or base.get("display_name"),
        "handle": handle or base.get("handle"),
        "pmo_notes": merged_note,
        "metadata": metadata,
    }

    if feishu_row and feishu_key != profile_key:
        queries.delete_people_memory(feishu_key)
    merged = queries.upsert_people_memory(payload)
    queries.backfill_chat_messages_sender_user_id(feishu_open_id, profile_id)
    if merged_note != old_note or feishu_row:
        queries.record_people_memory_update(
            profile_key,
            source="identity_merge",
            old_note=old_note,
            new_note=merged_note,
            model="deterministic_identity_merge_v1",
        )
    logger.info(
        "people_memory_identity_merged profile=%s feishu_open_id=%s had_profile=%s had_feishu=%s",
        profile_id,
        feishu_open_id,
        bool(profile_row),
        bool(feishu_row),
    )
    return merged
