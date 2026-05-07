"""Typed query helpers backing the agent's MCP tools.

Each function returns plain Python data structures (lists of dicts),
ready to JSON-encode back to the LLM. Errors raise — the tool wrapper
turns them into tool error messages the LLM can react to.
"""
from __future__ import annotations

import json
import hashlib
import re
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .client import sb, sb_admin


@dataclass
class Notification:
    id: int
    event_id: int
    subscription_id: str
    status: str
    decided_payload_version: int
    delivery_kind: str | None = None
    delivery_target: str | None = None
    suppressed_by: str | None = None
    claimed_at: str | None = None
    claim_id: str | None = None
    rendered_text: str | None = None
    feishu_msg_id: str | None = None
    decided_at: str | None = None
    sent_at: str | None = None
    error: str | None = None
    payload_snapshot: dict[str, Any] | None = None
    investigation_job_id: int | None = None
    mention_open_id: str | None = None


@dataclass
class Subscription:
    id: str
    scope_kind: str
    scope_id: str
    description: str
    enabled: bool
    created_by: str | None = None
    chat_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    archived_at: str | None = None
    target_kind: str | None = None
    target_id: str | None = None
    target_user_open_id: str | None = None
    consent_anchor: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class InvestigationJob:
    id: int
    subscription_id: str
    status: str
    seed_event_ids: list[int] = field(default_factory=list)
    initial_focus: str | None = None
    decider_reason: str | None = None
    investigator_decision: dict[str, Any] | None = None
    notification_id: int | None = None
    claim_id: str | None = None
    claimed_at: str | None = None
    attempt_count: int = 0
    last_error: str | None = None
    last_error_at: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    opened_at: str | None = None
    updated_at: str | None = None
    closed_at: str | None = None
    error: str | None = None


@dataclass
class ClaimedBundle:
    notification: Notification
    notif_payload_snapshot: dict[str, Any]
    notif_payload_version: int
    subscription: Subscription


@dataclass
class InvestigatableJobBundle:
    job: InvestigationJob
    subscription: Subscription
    events: list[dict[str, Any]]
    recent_notifications_for_subscription: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ExternalIdentity:
    id: str
    profile_id: str
    provider: str
    external_login: str
    external_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass
class ExternalRepo:
    id: str
    provider: str
    repo_full_name: str
    project_root: str
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


def _dataclass_from_row(cls, row: dict[str, Any]):
    allowed = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in row.items() if k in allowed})


def _jsonb_row(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return json.loads(value)
    raise TypeError(f"expected jsonb dict/string, got {type(value).__name__}")


def _rpc_returned_id(data: Any) -> bool:
    if data is None:
        return False
    if isinstance(data, list):
        return bool(data)
    return bool(data)


def _rpc_scalar(data: Any) -> Any:
    if isinstance(data, list):
        if not data:
            return None
        first = data[0]
        if isinstance(first, dict) and len(first) == 1:
            return next(iter(first.values()))
        return first
    return data


def lookup_profile(handle: str) -> Optional[dict[str, Any]]:
    """Find a profile by handle. Returns None if no such handle.

    The handle is treated case-insensitively (handles are stored
    lowercase per the migration's CHECK).
    """
    h = handle.strip().lstrip("@").lower()
    res = (
        sb()
        .table("profiles")
        .select("id, handle, display_name, created_at")
        .eq("handle", h)
        .maybe_single()
        .execute()
    )
    return res.data if res and res.data else None


def lookup_profile_by_handle_or_display(value: str) -> Optional[dict[str, Any]]:
    prof = lookup_profile(value)
    if prof:
        return prof
    rows = (
        sb()
        .table("profiles")
        .select("id, handle, display_name, created_at")
        .ilike("display_name", value.strip())
        .limit(2)
        .execute()
        .data
        or []
    )
    return rows[0] if len(rows) == 1 else None


def lookup_by_feishu_open_id(open_id: str) -> Optional[dict[str, Any]]:
    """Resolve a Feishu open_id to the linked pmo_agent profile.

    Returns the joined profile row (id, handle, display_name) or None
    if the user hasn't bound their Feishu account yet.

    The bot uses this to answer "我做了啥" without asking who you are.
    """
    if not open_id:
        return None
    # feishu_links is RLS-restricted to row owners; the bot reads via
    # service role to look up arbitrary open_ids.
    res = (
        sb_admin()
        .table("feishu_links")
        .select("user_id, feishu_name, feishu_email, feishu_mobile, profiles!inner(handle, display_name)")
        .eq("feishu_open_id", open_id)
        .maybe_single()
        .execute()
    )
    if not res or not res.data:
        return None
    row = res.data
    profile = row.get("profiles") or {}
    return {
        "user_id": row["user_id"],
        "handle": profile.get("handle"),
        "display_name": profile.get("display_name"),
        "feishu_name": row.get("feishu_name"),
        "feishu_mobile": row.get("feishu_mobile"),
    }


def lookup_feishu_link_by_user_id(user_id: str) -> Optional[dict[str, Any]]:
    if not user_id:
        return None
    res = (
        sb_admin()
        .table("feishu_links")
        .select("user_id, feishu_open_id, feishu_name, feishu_email, feishu_mobile, profiles!inner(handle, display_name)")
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if not res or not res.data:
        return None
    return _feishu_link_row_to_person(res.data)


def lookup_feishu_link_by_email(email: str) -> Optional[dict[str, Any]]:
    if not email:
        return None
    res = (
        sb_admin()
        .table("feishu_links")
        .select("user_id, feishu_open_id, feishu_name, feishu_email, feishu_mobile, profiles!inner(handle, display_name)")
        .ilike("feishu_email", email.strip())
        .maybe_single()
        .execute()
    )
    if not res or not res.data:
        return None
    return _feishu_link_row_to_person(res.data)


def lookup_feishu_link_by_phone(phone: str) -> Optional[dict[str, Any]]:
    variants = _phone_variants(phone)
    if not variants:
        return None
    res = (
        sb_admin()
        .table("feishu_links")
        .select("user_id, feishu_open_id, feishu_name, feishu_email, feishu_mobile, profiles!inner(handle, display_name)")
        .in_("feishu_mobile", variants)
        .maybe_single()
        .execute()
    )
    if not res or not res.data:
        return None
    return _feishu_link_row_to_person(res.data)


def _phone_variants(phone: str) -> list[str]:
    raw = (phone or "").strip()
    if not raw:
        return []
    normalized = raw.lstrip("+").replace("-", "").replace(" ", "")
    variants = {raw, normalized, f"+{normalized}"}
    if normalized.startswith("86") and len(normalized) > 2:
        bare = normalized[2:]
        variants.add(bare)
        variants.add(f"+{bare}")
    elif len(normalized) == 11 and normalized.startswith("1"):
        variants.add(f"86{normalized}")
        variants.add(f"+86{normalized}")
    return sorted(v for v in variants if v)


def _feishu_link_row_to_person(row: dict[str, Any]) -> dict[str, Any]:
    profile = row.get("profiles") or {}
    return {
        "user_id": row.get("user_id"),
        "handle": profile.get("handle"),
        "display_name": profile.get("display_name") or row.get("feishu_name"),
        "open_id": row.get("feishu_open_id"),
        "email": row.get("feishu_email"),
        "mobile": row.get("feishu_mobile"),
        "source": "profiles",
    }


def list_profiles() -> list[dict[str, Any]]:
    """All profiles, oldest first. Used when the user asks 'who's here'."""
    res = (
        sb()
        .table("profiles")
        .select("id, handle, display_name, created_at")
        .order("created_at", desc=False)
        .execute()
    )
    return res.data or []


def link_external_identity(
    profile_id: str,
    provider: str,
    external_login: str,
    external_id: str | None = None,
) -> dict[str, Any]:
    provider = provider.strip().lower()
    login = external_login.strip().lower()
    existing = (
        sb_admin()
        .table("external_identities")
        .select("*")
        .eq("provider", provider)
        .eq("external_login", login)
        .maybe_single()
        .execute()
        .data
    )
    if existing and str(existing.get("profile_id")) != str(profile_id):
        raise ValueError(f"{provider} identity {login} is already linked to another profile")
    row = {
        "profile_id": profile_id,
        "provider": provider,
        "external_login": login,
        "external_id": str(external_id) if external_id is not None else None,
        "updated_at": _utc_now_iso(),
    }
    res = (
        sb_admin()
        .table("external_identities")
        .upsert(row, on_conflict="provider,external_login")
        .select("*")
        .execute()
    )
    return (res.data or [row])[0]


def unlink_external_identity(profile_id: str, provider: str) -> bool:
    res = (
        sb_admin()
        .table("external_identities")
        .delete()
        .eq("profile_id", profile_id)
        .eq("provider", provider.strip().lower())
        .execute()
    )
    return bool(res.data)


def lookup_profile_by_external_login(provider: str, external_login: str, external_id: str | None = None) -> str | None:
    provider = provider.strip().lower()
    if external_id:
        res = (
            sb_admin()
            .table("external_identities")
            .select("profile_id")
            .eq("provider", provider)
            .eq("external_id", str(external_id))
            .maybe_single()
            .execute()
        )
        row = getattr(res, "data", None)
        if row:
            return row.get("profile_id")
    login = (external_login or "").strip().lower()
    if not login:
        return None
    res = (
        sb_admin()
        .table("external_identities")
        .select("profile_id")
        .eq("provider", provider)
        .eq("external_login", login)
        .maybe_single()
        .execute()
    )
    row = getattr(res, "data", None)
    return row.get("profile_id") if row else None


def external_identities_for_profile(profile_id: str) -> list[dict[str, Any]]:
    return (
        sb_admin()
        .table("external_identities")
        .select("*")
        .eq("profile_id", profile_id)
        .order("provider")
        .execute()
        .data
        or []
    )


def lookup_project_root_for_repo(provider: str, repo_full_name: str) -> str | None:
    row = (
        sb_admin()
        .table("external_repos")
        .select("project_root")
        .eq("provider", provider.strip().lower())
        .eq("repo_full_name", repo_full_name.strip().lower())
        .maybe_single()
        .execute()
        .data
    )
    return row.get("project_root") if row else None


def register_external_repo(
    provider: str,
    repo_full_name: str,
    project_root: str,
    created_by: str | None = None,
) -> dict[str, Any]:
    row = {
        "provider": provider.strip().lower(),
        "repo_full_name": repo_full_name.strip().lower(),
        "project_root": project_root,
        "created_by": created_by,
        "updated_at": _utc_now_iso(),
    }
    res = (
        sb_admin()
        .table("external_repos")
        .upsert(row, on_conflict="provider,repo_full_name")
        .select("*")
        .execute()
    )
    return (res.data or [row])[0]


def external_repos_for_project_root(project_root: str) -> list[dict[str, Any]]:
    return (
        sb_admin()
        .table("external_repos")
        .select("*")
        .eq("project_root", project_root)
        .order("provider")
        .execute()
        .data
        or []
    )


def list_external_repos(query: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit or 20), 100))
    res = (
        sb_admin()
        .table("external_repos")
        .select("provider,repo_full_name,project_root,created_by,created_at,updated_at")
        .order("provider")
        .order("repo_full_name")
        .limit(500)
        .execute()
    )
    rows = getattr(res, "data", None) or []
    needle = (query or "").strip().lower()
    if needle:
        rows = [
            row
            for row in rows
            if needle in str(row.get("repo_full_name") or "").lower()
            or needle in str(row.get("project_root") or "").lower()
        ]
    return rows[:limit]


def chat_memory_enabled_state(chat_id: str) -> bool | None:
    if not chat_id:
        return None
    row = (
        sb_admin()
        .table("chat_memory_settings")
        .select("enabled")
        .eq("chat_id", chat_id)
        .maybe_single()
        .execute()
        .data
    )
    if row is None:
        return None
    return bool(row.get("enabled"))


def is_chat_memory_enabled(chat_id: str) -> bool:
    return chat_memory_enabled_state(chat_id) is True


def _insert_chat_memory_history(
    *,
    chat_id: str,
    action: str,
    user_id: str | None,
    open_id: str | None,
    old_value: dict[str, Any] | None = None,
    new_value: dict[str, Any] | None = None,
) -> None:
    sb_admin().table("chat_memory_settings_history").insert(
        {
            "chat_id": chat_id,
            "action": action,
            "actor_user_id": user_id,
            "actor_open_id": open_id,
            "old_value": old_value,
            "new_value": new_value,
        }
    ).execute()


def enable_chat_memory(
    chat_id: str,
    *,
    user_id: str | None,
    open_id: str | None,
    retention_days: int = 90,
) -> dict[str, Any]:
    retention = max(1, min(int(retention_days or 90), 730))
    now = _utc_now_iso()
    current = chat_memory_status(chat_id)
    if current and current.get("enabled") is True:
        current_retention = int(current.get("retention_days") or 90)
        cleanup_payload: dict[str, Any] = {}
        if current.get("disabled_at") is not None:
            cleanup_payload["disabled_at"] = None
        if current.get("disabled_by_user_id") is not None:
            cleanup_payload["disabled_by_user_id"] = None
        if current.get("disabled_by_open_id") is not None:
            cleanup_payload["disabled_by_open_id"] = None
        if current_retention == retention and not cleanup_payload:
            return current
        payload = {
            **cleanup_payload,
            "retention_days": retention,
            "updated_at": now,
        }
        res = (
            sb_admin()
            .table("chat_memory_settings")
            .update(payload)
            .eq("chat_id", chat_id)
            .execute()
        )
        row = (res.data or [{**current, **payload}])[0]
        if current_retention != retention:
            _insert_chat_memory_history(
                chat_id=chat_id,
                action="retention_change",
                user_id=user_id,
                open_id=open_id,
                old_value={"retention_days": current_retention},
                new_value={"retention_days": retention},
            )
        return row

    payload = {
        "chat_id": chat_id,
        "enabled": True,
        "enabled_at": now,
        "enabled_by_user_id": user_id,
        "enabled_by_open_id": open_id,
        "disabled_at": None,
        "disabled_by_user_id": None,
        "disabled_by_open_id": None,
        "retention_days": retention,
        "updated_at": now,
    }
    res = (
        sb_admin()
        .table("chat_memory_settings")
        .upsert(payload, on_conflict="chat_id")
        .execute()
    )
    row = (res.data or [payload])[0]
    _insert_chat_memory_history(
        chat_id=chat_id,
        action="enable",
        user_id=user_id,
        open_id=open_id,
        old_value={"enabled": bool(current.get("enabled"))} if current else None,
        new_value={"enabled": True, "retention_days": retention},
    )
    return row


def disable_chat_memory(
    chat_id: str,
    *,
    user_id: str | None,
    open_id: str | None,
) -> dict[str, Any]:
    now = _utc_now_iso()
    current = chat_memory_status(chat_id)
    if current and current.get("enabled") is False:
        cleanup_payload: dict[str, Any] = {}
        if current.get("enabled_at") is not None:
            cleanup_payload["enabled_at"] = None
        if current.get("enabled_by_user_id") is not None:
            cleanup_payload["enabled_by_user_id"] = None
        if current.get("enabled_by_open_id") is not None:
            cleanup_payload["enabled_by_open_id"] = None
        if not cleanup_payload:
            return current
        cleanup_payload["updated_at"] = now
        res = (
            sb_admin()
            .table("chat_memory_settings")
            .update(cleanup_payload)
            .eq("chat_id", chat_id)
            .execute()
        )
        return (res.data or [{**current, **cleanup_payload}])[0]

    payload = {
        "chat_id": chat_id,
        "enabled": False,
        "enabled_at": None,
        "enabled_by_user_id": None,
        "enabled_by_open_id": None,
        "disabled_at": now,
        "disabled_by_user_id": user_id,
        "disabled_by_open_id": open_id,
        "updated_at": now,
    }
    if current:
        res = (
            sb_admin()
            .table("chat_memory_settings")
            .update(payload)
            .eq("chat_id", chat_id)
            .execute()
        )
    else:
        res = (
            sb_admin()
            .table("chat_memory_settings")
            .upsert(payload, on_conflict="chat_id")
            .execute()
        )
    row = (res.data or [payload])[0]
    _insert_chat_memory_history(
        chat_id=chat_id,
        action="disable",
        user_id=user_id,
        open_id=open_id,
        old_value={"enabled": bool(current.get("enabled"))} if current else None,
        new_value={"enabled": False},
    )
    return row


def chat_memory_status(chat_id: str) -> dict[str, Any] | None:
    row = (
        sb_admin()
        .table("chat_memory_settings")
        .select("*")
        .eq("chat_id", chat_id)
        .maybe_single()
        .execute()
        .data
    )
    return row or None


def insert_chat_message(row: dict[str, Any]) -> dict[str, Any] | None:
    payload = dict(row)
    message_type = payload.get("message_type") or "text"
    if not payload.get("text_redacted") and message_type in {"text", "post"}:
        payload["text_redacted"] = "[REDACTED]"
    elif payload.get("text_redacted") is None:
        payload["text_redacted"] = ""
    res = (
        sb_admin()
        .table("chat_messages")
        .upsert(payload, on_conflict="feishu_message_id")
        .execute()
    )
    return (res.data or [payload])[0] if res is not None else payload


def mark_chat_message_deleted(message_id: str) -> bool:
    res = (
        sb_admin()
        .table("chat_messages")
        .update({"deleted_at": _utc_now_iso()})
        .eq("feishu_message_id", message_id)
        .execute()
    )
    return bool(res and res.data)


def update_chat_message_text(
    message_id: str,
    *,
    text_redacted: str,
    redacted_payload: dict[str, Any],
    edited_at: str,
) -> bool:
    payload = {
        "text_redacted": text_redacted or "[REDACTED]",
        "redacted_payload": redacted_payload,
        "edited_at": edited_at,
    }
    res = (
        sb_admin()
        .table("chat_messages")
        .update(payload)
        .eq("feishu_message_id", message_id)
        .execute()
    )
    return bool(res and res.data)


def _chat_message_public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "message_id": row.get("feishu_message_id"),
        "sent_at": row.get("occurred_at"),
        "sender": row.get("sender_display_name") or row.get("sender_open_id"),
        "text": row.get("text_redacted") or "",
        "is_at_bot": bool(row.get("is_at_bot")),
        "message_type": row.get("message_type") or "text",
        "content_metadata": row.get("content_metadata") or {},
    }


def _chat_message_rows_for_chat(
    chat_id: str,
    *,
    since: str | None = None,
    until: str | None = None,
    limit: int = 1000,
    order_desc: bool = False,
) -> list[dict[str, Any]]:
    q = (
        sb_admin()
        .table("chat_messages")
        .select("*")
        .eq("chat_id", chat_id)
        .is_("deleted_at", None)
        .eq("sender_is_bot", False)
        .order("occurred_at", desc=order_desc)
        .limit(max(1, min(int(limit or 1000), 1000)))
    )
    if since:
        q = q.gte("occurred_at", since)
    if until:
        q = q.lte("occurred_at", until)
    return q.execute().data or []


def _default_chat_search_since_iso() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()


def _chat_metadata_value_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(_chat_metadata_value_text(v) for v in value.values() if v is not None)
    if isinstance(value, list):
        return " ".join(_chat_metadata_value_text(v) for v in value if v is not None)
    return str(value)


def _chat_searchable_text(row: dict[str, Any]) -> str:
    metadata = row.get("content_metadata") or {}
    return " ".join(
        part
        for part in [
            str(row.get("text_redacted") or ""),
            _chat_metadata_value_text(metadata) if metadata else "",
            str(row.get("sender_display_name") or ""),
        ]
        if part
    ).lower()


def _chat_window_from_rows(
    rows_asc: list[dict[str, Any]],
    *,
    anchor_message_id: str,
    before: int,
    after: int,
) -> list[dict[str, Any]]:
    idx = next(
        (i for i, row in enumerate(rows_asc) if row.get("feishu_message_id") == anchor_message_id),
        None,
    )
    if idx is None:
        return []
    start = max(0, idx - max(0, before))
    end = min(len(rows_asc), idx + max(0, after) + 1)
    return [_chat_message_public_row(row) for row in rows_asc[start:end]]


def search_chat_messages_with_context(
    chat_id: str,
    *,
    query: str | None = None,
    anchor_message_id: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 8,
    before: int = 8,
    after: int = 8,
) -> list[dict[str, Any]]:
    capped_limit = max(1, min(int(limit or 8), 10))
    capped_before = max(0, min(int(before or 0), 10))
    capped_after = max(0, min(int(after or 0), 10))
    effective_since = since
    if not anchor_message_id and not effective_since:
        effective_since = _default_chat_search_since_iso()
    rows_asc = _chat_message_rows_for_chat(
        chat_id,
        since=effective_since,
        until=until,
        limit=1000,
        order_desc=False,
    )

    if anchor_message_id:
        anchor = next((row for row in rows_asc if row.get("feishu_message_id") == anchor_message_id), None)
        if not anchor:
            return []
        return [
            {
                "hit": _chat_message_public_row(anchor),
                "context": _chat_window_from_rows(
                    rows_asc,
                    anchor_message_id=anchor_message_id,
                    before=capped_before,
                    after=capped_after,
                ),
            }
        ]

    needle = (query or "").strip().lower()
    if not needle:
        return []
    matches = [
        row
        for row in rows_asc
        if needle in _chat_searchable_text(row)
    ]
    matches.sort(key=lambda row: str(row.get("occurred_at") or ""), reverse=True)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in matches:
        message_id = row.get("feishu_message_id") or ""
        if not message_id or message_id in seen:
            continue
        seen.add(message_id)
        out.append(
            {
                "hit": _chat_message_public_row(row),
                "context": _chat_window_from_rows(
                    rows_asc,
                    anchor_message_id=message_id,
                    before=capped_before,
                    after=capped_after,
                ),
            }
        )
        if len(out) >= capped_limit:
            break
    return out


def get_recent_chat_messages(
    chat_id: str,
    *,
    since: str | None = None,
    until: str | None = None,
    limit: int = 80,
    sender: str | None = None,
) -> list[dict[str, Any]]:
    capped = max(1, min(int(limit or 80), 120))
    q = (
        sb_admin()
        .table("chat_messages")
        .select("*")
        .eq("chat_id", chat_id)
        .is_("deleted_at", None)
        .eq("sender_is_bot", False)
        .order("occurred_at", desc=True)
        .limit(capped)
    )
    if since:
        q = q.gte("occurred_at", since)
    if until:
        q = q.lte("occurred_at", until)
    if sender:
        q = q.eq("sender_open_id", sender)
    rows = q.execute().data or []
    return [_chat_message_public_row(row) for row in rows]


_PEOPLE_QUERY_TOKEN_RE = re.compile(r"[a-zA-Z0-9_./:-]{2,}|[\u4e00-\u9fff]{2,}")


def _note_matches_query(row: dict[str, Any], query: str | None) -> bool:
    needle = (query or "").strip().lower()
    if not needle:
        return True
    haystack = " ".join(
        str(part or "")
        for part in [
            row.get("display_name"),
            row.get("handle"),
            row.get("pmo_notes"),
            row.get("person_key"),
        ]
    ).lower()
    if needle in haystack:
        return True
    terms = [m.group(0).lower() for m in _PEOPLE_QUERY_TOKEN_RE.finditer(needle)]
    return bool(terms) and all(term in haystack for term in terms[:6])


def people_memory_for_chat(
    chat_id: str,
    *,
    query: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return internal people-memory rows for people observed in one chat.

    This helper intentionally returns pmo_notes for server-side summarizers.
    User-facing tool wrappers must never return those raw notes directly.
    """
    capped = max(1, min(int(limit or 20), 50))
    observed = (
        sb_admin()
        .table("chat_messages")
        .select("sender_open_id,sender_user_id,sender_display_name,occurred_at")
        .eq("chat_id", chat_id)
        .is_("deleted_at", None)
        .eq("sender_is_bot", False)
        .order("occurred_at", desc=True)
        .limit(1000)
        .execute()
        .data
        or []
    )
    profile_ids = {row.get("sender_user_id") for row in observed if row.get("sender_user_id")}
    open_ids = {row.get("sender_open_id") for row in observed if row.get("sender_open_id")}
    allowed_keys = {f"profile:{pid}" for pid in profile_ids} | {f"feishu:{oid}" for oid in open_ids}
    if not allowed_keys and not open_ids and not profile_ids:
        return []
    rows: list[dict[str, Any]] = []
    if allowed_keys:
        rows.extend(
            sb_admin()
            .table("people_memory")
            .select("*")
            .in_("person_key", list(allowed_keys))
            .order("last_observed_at", desc=True)
            .limit(500)
            .execute()
            .data
            or []
        )
    # Be tolerant of older rows whose person_key did not get normalized
    # but whose identity columns are already populated.
    known = {row.get("person_key") for row in rows}
    if profile_ids:
        for row in (
            sb_admin()
            .table("people_memory")
            .select("*")
            .in_("profile_id", list(profile_ids))
            .order("last_observed_at", desc=True)
            .limit(500)
            .execute()
            .data
            or []
        ):
            if row.get("person_key") not in known:
                rows.append(row)
                known.add(row.get("person_key"))
    if open_ids:
        for row in (
            sb_admin()
            .table("people_memory")
            .select("*")
            .in_("feishu_open_id", list(open_ids))
            .order("last_observed_at", desc=True)
            .limit(500)
            .execute()
            .data
            or []
        ):
            if row.get("person_key") not in known:
                rows.append(row)
                known.add(row.get("person_key"))
    rows.sort(key=lambda row: str(row.get("last_observed_at") or ""), reverse=True)
    matched = [row for row in rows if _note_matches_query(row, query)]
    return matched[:capped]


def get_people_memory(person_key: str) -> dict[str, Any] | None:
    if not person_key:
        return None
    return (
        sb_admin()
        .table("people_memory")
        .select("*")
        .eq("person_key", person_key)
        .maybe_single()
        .execute()
        .data
        or None
    )


def upsert_people_memory(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["updated_at"] = _utc_now_iso()
    res = (
        sb_admin()
        .table("people_memory")
        .upsert(payload, on_conflict="person_key")
        .execute()
    )
    return (res.data or [payload])[0]


def delete_people_memory(person_key: str) -> bool:
    res = (
        sb_admin()
        .table("people_memory")
        .delete()
        .eq("person_key", person_key)
        .execute()
    )
    return bool(res and res.data)


def _note_hash(note: str | None) -> str:
    return hashlib.sha256((note or "").encode("utf-8")).hexdigest()


def record_people_memory_update(
    person_key: str,
    *,
    source: str,
    old_note: str | None,
    new_note: str | None,
    model: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> dict[str, Any]:
    payload = {
        "person_key": person_key,
        "update_source": source,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "old_note_hash": _note_hash(old_note),
        "new_note_hash": _note_hash(new_note),
    }
    res = sb_admin().table("people_memory_updates").insert(payload).execute()
    return (res.data or [payload])[0]


def recent_people_memory_update_count(source: str, *, since_iso: str) -> int:
    rows = (
        sb_admin()
        .table("people_memory_updates")
        .select("id")
        .eq("update_source", source)
        .gte("created_at", since_iso)
        .limit(10000)
        .execute()
        .data
        or []
    )
    return len(rows)


def recent_people_memory_update_for_person(person_key: str, *, since_iso: str) -> bool:
    rows = (
        sb_admin()
        .table("people_memory_updates")
        .select("id")
        .eq("person_key", person_key)
        .gte("created_at", since_iso)
        .limit(1)
        .execute()
        .data
        or []
    )
    return bool(rows)


def enabled_chat_memory_settings_for_people_loop(limit: int = 10) -> list[dict[str, Any]]:
    return (
        sb_admin()
        .table("chat_memory_settings")
        .select("*")
        .eq("enabled", True)
        .order("updated_at", desc=True)
        .limit(max(1, min(int(limit or 10), 100)))
        .execute()
        .data
        or []
    )


def chat_messages_after_cursor(
    chat_id: str,
    *,
    cursor: str | None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    q = (
        sb_admin()
        .table("chat_messages")
        .select("*")
        .eq("chat_id", chat_id)
        .is_("deleted_at", None)
        .eq("sender_is_bot", False)
        .order("occurred_at", desc=False)
        .limit(max(1, min(int(limit or 500), 1000)))
    )
    if cursor:
        q = q.gt("occurred_at", cursor)
    return q.execute().data or []


def advance_people_loop_cursor(chat_id: str, cursor: str) -> None:
    (
        sb_admin()
        .table("chat_memory_settings")
        .update({"people_loop_cursor": cursor, "updated_at": _utc_now_iso()})
        .eq("chat_id", chat_id)
        .execute()
    )


def backfill_chat_messages_sender_user_id(feishu_open_id: str, profile_id: str) -> int:
    if not feishu_open_id or not profile_id:
        return 0
    res = (
        sb_admin()
        .table("chat_messages")
        .update({"sender_user_id": profile_id})
        .eq("sender_open_id", feishu_open_id)
        .execute()
    )
    return len(res.data or [])


def recent_turns(
    user_id: str,
    *,
    since_iso: Optional[str] = None,
    until_iso: Optional[str] = None,
    project_root: Optional[str] = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Turns for one user, newest-first, optionally filtered by date / project.

    project_root matches the canonical project_root column. Older rows
    without that column populated fall back to the legacy path heuristic.
    """
    fetch_limit = 1000 if project_root else limit
    q = (
        sb()
        .table("turns")
        .select(
            "id, agent, agent_session_id, project_path, project_root, turn_index, "
            "user_message, agent_summary, device_label, "
            "user_message_at, agent_response_at"
        )
        .eq("user_id", user_id)
        .filter("agent_response_full", "not.is", "null")
        .neq("agent_response_full", "")
        .order("user_message_at", desc=True)
        .limit(fetch_limit)
    )
    if since_iso:
        q = q.gte("user_message_at", since_iso)
    if until_iso:
        q = q.lte("user_message_at", until_iso)

    res = q.execute()
    rows = res.data or []
    if project_root:
        rows = [r for r in rows if project_root_for_row(r) == project_root][:limit]
    return rows


def project_overview(user_id: str) -> list[dict[str, Any]]:
    """Cached LLM summaries per (user_id, project_root). Newest first."""
    res = (
        sb()
        .table("project_summaries")
        .select("project_root, summary, turn_count, last_turn_at, generated_at")
        .eq("user_id", user_id)
        .order("last_turn_at", desc=True)
        .execute()
    )
    return res.data or []


def turn_counts_by_window(
    user_id: str,
    *,
    days: int = 7,
) -> dict[str, Any]:
    """Aggregate counts for 'How busy was X this week' style answers.

    Returns:
        {
          "since": ISO,
          "until": ISO,
          "total_turns": int,
          "by_project": [{"project_root": ..., "n": ...}, ...],
          "by_day": [{"day": "YYYY-MM-DD", "n": ...}, ...],
        }
    """
    until = datetime.now(timezone.utc)
    since = until - timedelta(days=days)
    rows = recent_turns(
        user_id,
        since_iso=since.isoformat(),
        until_iso=until.isoformat(),
        limit=1000,
    )

    by_project: dict[str, int] = {}
    by_day: dict[str, int] = {}
    for r in rows:
        root = project_root_for_row(r)
        by_project[root] = by_project.get(root, 0) + 1

        day = r["user_message_at"][:10]  # YYYY-MM-DD prefix
        by_day[day] = by_day.get(day, 0) + 1

    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "total_turns": len(rows),
        "by_project": sorted(
            [{"project_root": k, "n": v} for k, v in by_project.items()],
            key=lambda x: -x["n"],
        ),
        "by_day": sorted(
            [{"day": k, "n": v} for k, v in by_day.items()],
            key=lambda x: x["day"],
            reverse=True,
        ),
    }


def project_root_for_row(row: dict[str, Any]) -> str:
    """Return canonical project_root with legacy fallback for old rows."""
    root = row.get("project_root")
    if isinstance(root, str) and root:
        return root
    return legacy_project_root_from_path(row.get("project_path"))


def legacy_project_root_from_path(path: Any) -> str:
    if not isinstance(path, str) or not path:
        return "(unknown)"
    parts = path.lstrip("/").split("/")
    return "/" + "/".join(parts[:4]) if len(parts) > 4 else path


# ── bot_workspace ─────────────────────────────────────────────────────


def get_bot_workspace() -> Optional[dict[str, Any]]:
    res = (
        sb_admin()
        .table("bot_workspace")
        .select("*")
        .eq("id", 1)
        .maybe_single()
        .execute()
    )
    return res.data if res and res.data else None


def upsert_bot_workspace(
    *,
    calendar_id: str,
    base_app_token: str,
    action_items_table_id: str,
    meetings_table_id: str,
    docs_folder_token: str,
) -> None:
    sb_admin().table("bot_workspace").upsert(
        {
            "id": 1,
            "calendar_id": calendar_id,
            "base_app_token": base_app_token,
            "action_items_table_id": action_items_table_id,
            "meetings_table_id": meetings_table_id,
            "docs_folder_token": docs_folder_token,
        }
    ).execute()


# ── bot_actions ───────────────────────────────────────────────────────


class _Sentinel:
    pass


LastIsInFlight = _Sentinel()
LastWasUnreachable = _Sentinel()


class BotActionInsertConflict(Exception):
    def __init__(self, existing_row: dict[str, Any] | None = None, raw_error: Any = None):
        super().__init__("bot action insert conflict")
        self.existing_row = existing_row
        self.raw_error = raw_error


class MessageActionConflict(BotActionInsertConflict):
    pass


class LogicalKeyConflict(BotActionInsertConflict):
    pass


_CONSTRAINT_RE = re.compile(r'unique constraint "([^"]+)"')
_STUCK_PENDING_THRESHOLD = timedelta(minutes=5)
_SUCCESS_LOCK_TTL = timedelta(seconds=60)
_BOOTSTRAP_LOCK_MESSAGE_ID = "__bootstrap_lock__"
_BOOTSTRAP_LOCK_ACTION_TYPE = "bootstrap_workspace_lock"


def _extract_constraint_name(error_message: str) -> str | None:
    match = _CONSTRAINT_RE.search(error_message)
    return match.group(1) if match else None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _execute_data(request: Any) -> Any:
    res = request.execute()
    return res.data if res is not None else None


def _has_artifact_handle(row: dict[str, Any]) -> bool:
    if row.get("target_id"):
        return True
    result = row.get("result") or {}
    return bool(result.get("import_ticket") or result.get("source_file_token"))


def _lazy_gc_stuck_pending(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("status") != "pending":
        return row
    age_source = row.get("updated_at") or row.get("created_at")
    if not age_source:
        return row
    age = datetime.now(timezone.utc) - datetime.fromisoformat(
        str(age_source).replace("Z", "+00:00")
    )
    if age < _STUCK_PENDING_THRESHOLD:
        return row
    has_handle = _has_artifact_handle(row)
    kind = "partial_success" if has_handle else "stuck_pending"
    new_result = {**(row.get("result") or {}), "reconciliation_kind": kind}
    res = (
        sb_admin()
        .table("bot_actions")
        .update(
            {
                "status": "reconciled_unknown",
                "error": "reconciled: pending too long",
                "result": new_result,
                "logical_key_locked": has_handle,
                "updated_at": _utc_now_iso(),
            }
        )
        .eq("id", row["id"])
        .eq("status", "pending")
        .execute()
    )
    if res and res.data:
        return res.data[0]
    return (
        _execute_data(
            sb_admin()
            .table("bot_actions")
            .select("*")
            .eq("id", row["id"])
            .maybe_single()
        )
        or row
    )


def _unlock_aged_success(row: dict[str, Any]) -> dict[str, Any] | None:
    if row.get("status") != "success" or not row.get("logical_key_locked"):
        return row
    age_source = row.get("created_at") or row.get("updated_at")
    if not age_source:
        return row
    age = datetime.now(timezone.utc) - datetime.fromisoformat(
        str(age_source).replace("Z", "+00:00")
    )
    if age <= _SUCCESS_LOCK_TTL:
        return row
    res = (
        sb_admin()
        .table("bot_actions")
        .update({"logical_key_locked": False, "updated_at": _utc_now_iso()})
        .eq("id", row["id"])
        .eq("logical_key_locked", True)
        .execute()
    )
    if res and res.data:
        return None
    current = (
        _execute_data(
            sb_admin()
            .table("bot_actions")
            .select("*")
            .eq("id", row["id"])
            .maybe_single()
        )
    )
    if not current or not current.get("logical_key_locked"):
        return None
    return current


def insert_bot_action_pending(
    *,
    message_id: str,
    chat_id: str,
    sender_open_id: str,
    action_type: str,
    args: dict[str, Any],
    logical_key: str,
    target_id: str | None = None,
    target_kind: str | None = None,
    result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "message_id": message_id,
        "chat_id": chat_id,
        "sender_open_id": sender_open_id,
        "action_type": action_type,
        "logical_key": logical_key,
        "status": "pending",
        "logical_key_locked": True,
        "args": args,
        "target_id": target_id,
        "target_kind": target_kind,
        "result": result or {},
    }
    try:
        res = sb_admin().table("bot_actions").insert(payload).execute()
        return res.data[0] if isinstance(res.data, list) else res.data
    except Exception as e:
        msg = str(getattr(e, "message", e))
        constraint = _extract_constraint_name(msg)
        if constraint == "bot_actions_message_action_uniq":
            existing = get_bot_action(message_id, action_type)
            raise MessageActionConflict(existing_row=existing, raw_error=e)
        if constraint == "bot_actions_logical_locked_uniq":
            existing = get_locked_by_logical_key(logical_key)
            raise LogicalKeyConflict(existing_row=existing, raw_error=e)
        raise BotActionInsertConflict(raw_error=e)


def acquire_bootstrap_lock() -> dict[str, Any] | None:
    try:
        return insert_bot_action_pending(
            message_id=_BOOTSTRAP_LOCK_MESSAGE_ID,
            chat_id="bootstrap",
            sender_open_id="bootstrap",
            action_type=_BOOTSTRAP_LOCK_ACTION_TYPE,
            args={},
            logical_key=_BOOTSTRAP_LOCK_ACTION_TYPE,
            target_kind="workspace_bootstrap",
            result={},
        )
    except BotActionInsertConflict:
        return None


def release_bootstrap_lock(lock_id: str) -> None:
    (
        sb_admin()
        .table("bot_actions")
        .delete()
        .eq("id", lock_id)
        .eq("action_type", _BOOTSTRAP_LOCK_ACTION_TYPE)
        .execute()
    )


def get_bot_action(message_id: str, action_type: str) -> dict[str, Any] | None:
    row = (
        _execute_data(
            sb_admin()
            .table("bot_actions")
            .select("*")
            .eq("message_id", message_id)
            .eq("action_type", action_type)
            .maybe_single()
        )
    )
    return _lazy_gc_stuck_pending(row) if row else None


def get_locked_by_logical_key(logical_key: str) -> dict[str, Any] | None:
    row = (
        _execute_data(
            sb_admin()
            .table("bot_actions")
            .select("*")
            .eq("logical_key", logical_key)
            .eq("logical_key_locked", True)
            .maybe_single()
        )
    )
    if not row:
        return None
    row = _lazy_gc_stuck_pending(row)
    if not row.get("logical_key_locked"):
        return None
    return _unlock_aged_success(row)


def update_for_retry(action_id: str) -> dict[str, Any] | None:
    row = (
        _execute_data(
            sb_admin()
            .table("bot_actions")
            .select("attempt_count")
            .eq("id", action_id)
            .maybe_single()
        )
        or {}
    )
    attempt_count = int(row.get("attempt_count") or 1) + 1
    res = (
        sb_admin()
        .table("bot_actions")
        .update({"status": "pending", "attempt_count": attempt_count, "updated_at": _utc_now_iso()})
        .eq("id", action_id)
        .execute()
    )
    return res.data[0] if res and res.data else None


def mark_bot_action_undone(action_id: str) -> dict[str, Any] | None:
    res = (
        sb_admin()
        .table("bot_actions")
        .update({"status": "undone", "logical_key_locked": False, "updated_at": _utc_now_iso()})
        .eq("id", action_id)
        .eq("status", "pending")
        .execute()
    )
    return res.data[0] if res and res.data else None


def record_bot_action_target_pending(
    action_id: str,
    *,
    target_id: str | None = None,
    target_kind: str | None = None,
    result_patch: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    current = (
        _execute_data(sb_admin().table("bot_actions").select("result").eq("id", action_id).maybe_single())
        or {}
    )
    result = {**(current.get("result") or {}), **(result_patch or {})}
    payload = {"result": result, "updated_at": _utc_now_iso()}
    if target_id is not None:
        payload["target_id"] = target_id
    if target_kind is not None:
        payload["target_kind"] = target_kind
    res = (
        sb_admin()
        .table("bot_actions")
        .update(payload)
        .eq("id", action_id)
        .eq("status", "pending")
        .execute()
    )
    return res.data[0] if res and res.data else None


def mark_bot_action_success(action_id: str, result_patch: dict[str, Any] | None = None) -> dict[str, Any] | None:
    current = (
        _execute_data(sb_admin().table("bot_actions").select("result").eq("id", action_id).maybe_single())
        or {}
    )
    result = {**(current.get("result") or {}), **(result_patch or {})}
    res = (
        sb_admin()
        .table("bot_actions")
        .update({"status": "success", "result": result, "updated_at": _utc_now_iso()})
        .eq("id", action_id)
        .eq("status", "pending")
        .execute()
    )
    return res.data[0] if res and res.data else None


def mark_bot_action_failed(action_id: str, error: str) -> None:
    sb_admin().table("bot_actions").update(
        {"status": "failed", "error": error, "logical_key_locked": False, "updated_at": _utc_now_iso()}
    ).eq("id", action_id).eq("status", "pending").execute()


def mark_bot_action_reconciled_unknown(
    action_id: str, *, reconciliation_kind: str, error: str | None = None, keep_lock: bool = True
) -> None:
    row = _execute_data(sb_admin().table("bot_actions").select("result").eq("id", action_id).maybe_single()) or {}
    result = {**(row.get("result") or {}), "reconciliation_kind": reconciliation_kind}
    sb_admin().table("bot_actions").update(
        {
            "status": "reconciled_unknown",
            "result": result,
            "error": error,
            "logical_key_locked": keep_lock,
            "updated_at": _utc_now_iso(),
        }
    ).eq("id", action_id).eq("status", "pending").execute()


def retire_source_action(action_id: str) -> None:
    sb_admin().table("bot_actions").update(
        {"status": "undone", "logical_key_locked": False, "updated_at": _utc_now_iso()}
    ).eq("id", action_id).in_("status", ["success", "reconciled_unknown", "pending"]).execute()


def record_undo_audit(
    source_row: dict[str, Any],
    *,
    result_patch: dict[str, Any] | None = None,
    status: str = "success",
    error: str | None = None,
) -> None:
    insert_bot_action_pending(
        message_id=f"undo:{source_row['id']}",
        chat_id=source_row["chat_id"],
        sender_open_id=source_row["sender_open_id"],
        action_type="undo_last_action",
        args={"source_action_id": source_row["id"]},
        logical_key=f"undo:{source_row['id']}",
        target_id=source_row["id"],
        target_kind="bot_action_undo",
        result={"source_action_type": source_row.get("action_type"), **(result_patch or {})},
    )
    row = get_bot_action(f"undo:{source_row['id']}", "undo_last_action")
    if row:
        if status == "success":
            mark_bot_action_success(row["id"])
        elif status == "reconciled_unknown":
            mark_bot_action_reconciled_unknown(
                row["id"],
                reconciliation_kind="partial_success",
                error=error,
                keep_lock=False,
            )
        else:
            mark_bot_action_failed(row["id"], error or status)


def get_bot_action_by_target(
    *, chat_id: str | None = None, sender_open_id: str | None = None,
    target_id: str, target_kind: str,
    action_type_in: list[str] | None = None,
    status_in: list[str] | None = None,
) -> dict[str, Any] | None:
    q = sb_admin().table("bot_actions").select("*").eq("target_id", target_id).eq("target_kind", target_kind)
    if chat_id:
        q = q.eq("chat_id", chat_id)
    if sender_open_id:
        q = q.eq("sender_open_id", sender_open_id)
    if action_type_in:
        q = q.in_("action_type", action_type_in)
    if status_in:
        q = q.in_("status", status_in)
    row = _execute_data(q.order("created_at", desc=True).limit(1).maybe_single())
    return row


def last_meeting_action_for_sender_in_chat(chat_id: str, sender_open_id: str) -> dict[str, Any] | None:
    rows = (
        sb_admin()
        .table("bot_actions")
        .select("*")
        .eq("chat_id", chat_id)
        .eq("sender_open_id", sender_open_id)
        .eq("target_kind", "calendar_event")
        .in_("action_type", ["schedule_meeting", "restore_schedule_meeting"])
        .in_("status", ["success", "reconciled_unknown"])
        .order("created_at", desc=True)
        .limit(10)
        .execute()
        .data
        or []
    )
    for row in rows:
        row = _lazy_gc_stuck_pending(row)
        if row.get("target_id"):
            return row
    return None


def bot_known_events_for_attendee(chat_id: str, attendee_open_id: str) -> list[dict[str, Any]]:
    rows = (
        sb_admin()
        .table("bot_actions")
        .select("*")
        .eq("chat_id", chat_id)
        .eq("target_kind", "calendar_event")
        .in_("action_type", ["schedule_meeting", "restore_schedule_meeting"])
        .in_("status", ["success", "reconciled_unknown"])
        .order("created_at", desc=True)
        .limit(100)
        .execute()
        .data
        or []
    )
    events: list[dict[str, Any]] = []
    for row in rows:
        row = _lazy_gc_stuck_pending(row)
        result = row.get("result") or {}
        if not row.get("target_id") or attendee_open_id not in (result.get("attendees") or []):
            continue
        events.append({
            "action_id": row.get("id"),
            "event_id": row.get("target_id"),
            "title": result.get("title") or result.get("summary") or (row.get("args") or {}).get("title"),
            "start_time": result.get("start_time") or (row.get("args") or {}).get("start_time"),
            "end_time": result.get("end_time"),
            "link": result.get("link"),
            "source": "bot_actions",
            "status": row.get("status"),
        })
    return events


def is_doc_authored_by_bot(doc_token: str) -> bool:
    return bool(get_bot_action_by_target(
        target_id=doc_token,
        target_kind="docx",
        action_type_in=["create_doc", "create_meeting_doc"],
        status_in=["success", "reconciled_unknown"],
    ))


def last_bot_action_for_sender_in_chat(chat_id: str, sender_open_id: str):
    rows = (
        sb_admin()
        .table("bot_actions")
        .select("*")
        .eq("chat_id", chat_id)
        .eq("sender_open_id", sender_open_id)
        .order("created_at", desc=True)
        .limit(10)
        .execute()
        .data
        or []
    )
    for candidate in rows:
        if candidate.get("action_type") == "undo_last_action":
            continue
        first = _lazy_gc_stuck_pending(candidate)
        status = first.get("status")
        result = first.get("result") or {}
        if status == "pending":
            return LastIsInFlight
        if status in {"failed", "undone"}:
            return LastWasUnreachable
        if status == "reconciled_unknown" and result.get("reconciliation_kind") == "stuck_pending":
            return LastWasUnreachable
        if status in {"success", "reconciled_unknown"} and (
            first.get("target_id") or result.get("import_ticket") or result.get("source_file_token")
        ):
            return first
        return LastWasUnreachable
    return None


# ── proactive notifications ─────────────────────────────────────────────


def lookup_profile_by_user_id(user_id: str) -> Optional[dict[str, Any]]:
    if not user_id:
        return None
    row = _execute_data(
        sb().table("profiles").select("id, handle, display_name, created_at").eq("id", user_id).maybe_single()
    )
    return row or None


def fetch_events_needing_decision(limit: int = 100) -> list[dict[str, Any]]:
    rows = (
        sb_admin()
        .table("events_needing_decision")
        .select("*")
        .order("ingested_at", desc=False)
        .limit(limit)
        .execute()
        .data
        or []
    )
    return rows


def mark_event_processed(event_id: int, payload_version: int) -> None:
    (
        sb_admin()
        .table("events")
        .update({"processed_at": _utc_now_iso(), "processed_version": payload_version})
        .eq("id", event_id)
        .execute()
    )


def fetch_all_enabled_subscriptions() -> list[dict[str, Any]]:
    return (
        sb_admin()
        .table("subscriptions")
        .select("*")
        .eq("enabled", True)
        .is_("archived_at", "null")
        .order("created_at", desc=True)
        .limit(10000)
        .execute()
        .data
        or []
    )


_REPO_IDENTIFIER_RE = re.compile(r"^[a-z][a-z0-9_+.-]*:[^/\s]+/[^/\s]+$")
_MAX_EXTERNAL_RESOURCE_CONTENT_BYTES = 1024 * 1024


def _project_last_segment(project_root: str | None) -> str:
    if not project_root:
        return ""
    root = project_root.strip().lower()
    if not root or root.endswith("/"):
        return ""
    if _REPO_IDENTIFIER_RE.match(root):
        return root
    return root.rsplit("/", 1)[-1].lower()


def _project_tokens_for_event_row(row: dict[str, Any]) -> set[str]:
    payload = row.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        token
        for token in [
            _project_last_segment(row.get("project_root")),
            _project_last_segment(payload.get("project_path")),
            _project_last_segment(payload.get("project_root")),
        ]
        if token
    }


def distinct_project_root_tokens() -> list[str]:
    rows = (
        sb_admin()
        .table("events")
        .select("project_root,payload")
        .limit(10000)
        .execute()
        .data
        or []
    )
    return sorted(
        {
            token
            for row in rows
            for token in _project_tokens_for_event_row(row)
            if token
        }
    )


def lookup_external_resource(provider: str, resource_kind: str, resource_key: str) -> dict[str, Any] | None:
    row = _execute_data(
        sb_admin()
        .table("external_resource_cache")
        .select("*")
        .eq("provider", provider)
        .eq("resource_kind", resource_kind)
        .eq("resource_key", resource_key)
        .gt("expires_at", _utc_now_iso())
        .maybe_single()
    )
    return row or None


def write_external_resource(
    provider: str,
    resource_kind: str,
    resource_key: str,
    content: dict[str, Any],
    ttl_seconds: int = 86400,
) -> None:
    content_bytes = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(content_bytes) > _MAX_EXTERNAL_RESOURCE_CONTENT_BYTES:
        raise ValueError("external resource content too large")
    now = _utc_now_iso()
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
    (
        sb_admin()
        .table("external_resource_cache")
        .delete()
        .lt("expires_at", now)
        .execute()
    )
    (
        sb_admin()
        .table("external_resource_cache")
        .upsert(
            {
                "provider": provider,
                "resource_kind": resource_kind,
                "resource_key": resource_key,
                "content": content,
                "fetched_at": now,
                "expires_at": expires_at,
            },
            on_conflict="provider,resource_kind,resource_key",
        )
        .execute()
    )


def archive_external_delivery(
    *,
    provider: str,
    delivery_id: str,
    event_type: str,
    raw_body: dict[str, Any],
    raw_headers: dict[str, Any] | None = None,
) -> int:
    res = (
        sb_admin()
        .table("external_webhook_deliveries")
        .upsert(
            {
                "provider": provider,
                "delivery_id": delivery_id,
                "event_type": event_type,
                "raw_body": raw_body,
                "raw_headers": raw_headers or {},
                "received_at": _utc_now_iso(),
            },
            on_conflict="provider,delivery_id",
            ignore_duplicates=True,
        )
        .select("id")
        .execute()
    )
    row = (res.data or [None])[0]
    if not row:
        row = (
            sb_admin()
            .table("external_webhook_deliveries")
            .select("id")
            .eq("provider", provider)
            .eq("delivery_id", delivery_id)
            .maybe_single()
            .execute()
            .data
        )
    return int(row["id"])


def link_archive_to_event(archive_id: int, event_id: int) -> None:
    (
        sb_admin()
        .table("external_webhook_deliveries")
        .update({"event_id": event_id})
        .eq("id", archive_id)
        .is_("event_id", "null")
        .execute()
    )


def mark_archive_ignored(archive_id: int, reason: str) -> None:
    (
        sb_admin()
        .table("external_webhook_deliveries")
        .update({"ignored_reason": reason, "ignored_at": _utc_now_iso()})
        .eq("id", archive_id)
        .is_("event_id", "null")
        .execute()
    )


def upsert_event(
    *,
    source: str,
    source_id: str,
    user_id: str | None,
    project_root: str | None,
    occurred_at: str | None,
    payload: dict[str, Any],
    payload_fingerprint: str | None = None,
) -> int | None:
    data = (
        sb_admin()
        .rpc(
            "upsert_external_event",
            {
                "p_source": source,
                "p_source_id": source_id,
                "p_user_id": user_id,
                "p_project_root": project_root,
                "p_occurred_at": occurred_at or _utc_now_iso(),
                "p_payload": payload,
                "p_payload_fingerprint": payload_fingerprint,
            },
        )
        .execute()
        .data
    )
    value = _rpc_scalar(data)
    return int(value) if value is not None else None


def get_event(event_id: int) -> dict[str, Any] | None:
    row = _execute_data(
        sb_admin()
        .table("events")
        .select("*")
        .eq("id", event_id)
        .maybe_single()
    )
    return row or None


def fetch_subscriptions_for_scope(scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
    return (
        sb_admin()
        .table("subscriptions")
        .select("*")
        .eq("scope_kind", scope_kind)
        .eq("scope_id", scope_id)
        .eq("enabled", True)
        .is_("archived_at", "null")
        .order("created_at", desc=True)
        .execute()
        .data
        or []
    )


def get_subscription(subscription_id: str) -> Optional[Subscription]:
    row = _execute_data(
        sb_admin()
        .table("subscriptions")
        .select("*")
        .eq("id", subscription_id)
        .maybe_single()
    )
    return _dataclass_from_row(Subscription, row) if row else None


def index_subscription_metadata(subscription_id: str) -> None:
    (
        sb_admin()
        .rpc("index_subscription_metadata", {"p_subscription_id": subscription_id})
        .execute()
    )


def get_notification(event_id: int, subscription_id: str) -> Optional[dict[str, Any]]:
    row = _execute_data(
        sb_admin()
        .table("notifications")
        .select("*")
        .eq("event_id", event_id)
        .eq("subscription_id", subscription_id)
        .maybe_single()
    )
    return row or None


def _decision_value(decision: Any, name: str, default: Any = None) -> Any:
    if isinstance(decision, dict):
        return decision.get(name, default)
    return getattr(decision, name, default)


def write_decision_log(
    *,
    event_id: int,
    subscription_id: str,
    payload_version: int,
    judge_input: dict[str, Any],
    judge_output: dict[str, Any],
    model: str,
    latency_ms: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    investigation_job_id: int | None = None,
) -> dict[str, Any] | None:
    res = (
        sb_admin()
        .table("decision_logs")
        .insert(
            {
                "event_id": event_id,
                "subscription_id": subscription_id,
                "payload_version": payload_version,
                "judge_input": judge_input,
                "judge_output": judge_output,
                "model": model,
                "latency_ms": latency_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "investigation_job_id": investigation_job_id,
            }
        )
        .execute()
    )
    return res.data[0] if res and res.data else None


def upsert_notification_row(
    *,
    event_id: int,
    subscription_id: str,
    decision: Any,
    decided_payload_version: int,
    payload_snapshot: dict[str, Any],
    delivery_kind: str | None = None,
    delivery_target: str | None = None,
) -> str:
    send = bool(_decision_value(decision, "send", False))
    suppressed_by = _decision_value(decision, "suppressed_by")
    if send:
        status = "pending"
        suppressed_by = None
    else:
        status = "suppressed"
        suppressed_by = suppressed_by or "mismatch"
    data = (
        sb_admin()
        .rpc(
            "upsert_notification_row",
            {
                "p_event_id": event_id,
                "p_subscription_id": subscription_id,
                "p_status": status,
                "p_suppressed_by": suppressed_by,
                "p_delivery_kind": delivery_kind,
                "p_delivery_target": delivery_target,
                "p_decided_payload_version": decided_payload_version,
                "p_payload_snapshot": payload_snapshot,
            },
        )
        .execute()
        .data
    )
    return data or "noop"


def claim_pending_notifications(claim_id: str, limit: int = 20) -> list[ClaimedBundle]:
    rows = (
        sb_admin()
        .rpc(
            "claim_pending_notifications",
            {"p_claim_id": claim_id, "p_limit": limit},
        )
        .execute()
        .data
        or []
    )
    bundles: list[ClaimedBundle] = []
    for row in rows:
        notification = _dataclass_from_row(Notification, _jsonb_row(row.get("notification")))
        subscription = _dataclass_from_row(Subscription, _jsonb_row(row.get("subscription")))
        bundles.append(
            ClaimedBundle(
                notification=notification,
                notif_payload_snapshot=_jsonb_row(row.get("notif_payload_snapshot")),
                notif_payload_version=int(row.get("notif_payload_version") or notification.decided_payload_version),
                subscription=subscription,
            )
        )
    return bundles


def append_to_or_open_investigation_job(
    subscription_id: str,
    event_id: int,
    initial_focus: str,
    decider_reason: str,
    *,
    window_minutes: int = 30,
) -> int | None:
    data = (
        sb_admin()
        .rpc(
            "append_to_or_open_investigation_job",
            {
                "p_subscription_id": subscription_id,
                "p_event_id": event_id,
                "p_initial_focus": initial_focus,
                "p_decider_reason": decider_reason,
                "p_window_minutes": window_minutes,
            },
        )
        .execute()
        .data
    )
    value = _rpc_scalar(data)
    return int(value) if value is not None else None


def claim_investigatable_jobs(
    claim_id: str,
    limit: int = 5,
    *,
    window_minutes: int = 30,
) -> list[InvestigatableJobBundle]:
    rows = (
        sb_admin()
        .rpc(
            "claim_investigatable_jobs",
            {
                "p_claim_id": claim_id,
                "p_limit": limit,
                "p_window_minutes": window_minutes,
            },
        )
        .execute()
        .data
        or []
    )
    bundles: list[InvestigatableJobBundle] = []
    for row in rows:
        job = _dataclass_from_row(InvestigationJob, _jsonb_row(row.get("investigation_job")))
        subscription = _dataclass_from_row(Subscription, _jsonb_row(row.get("subscription")))
        events = row.get("event_payloads") or []
        if isinstance(events, str):
            events = json.loads(events)
        bundles.append(
            InvestigatableJobBundle(
                job=job,
                subscription=subscription,
                events=list(events),
                recent_notifications_for_subscription=recent_notifications_for_subscription(subscription.id),
            )
        )
    return bundles


def create_notification_for_investigation_job(
    *,
    job_id: int,
    claim_id: str,
    event_id: int,
    subscription_id: str,
    decided_payload_version: int,
    payload_snapshot: dict[str, Any],
    delivery_kind: str | None,
    delivery_target: str | None,
    mention_open_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> int | None:
    data = (
        sb_admin()
        .rpc(
            "create_notification_for_investigation_job",
            {
                "p_job_id": job_id,
                "p_claim_id": claim_id,
                "p_event_id": event_id,
                "p_subscription_id": subscription_id,
                "p_decided_payload_version": decided_payload_version,
                "p_payload_snapshot": payload_snapshot,
                "p_delivery_kind": delivery_kind,
                "p_delivery_target": delivery_target,
                "p_mention_open_id": mention_open_id,
                "p_input_tokens": input_tokens,
                "p_output_tokens": output_tokens,
            },
        )
        .execute()
        .data
    )
    value = _rpc_scalar(data)
    return int(value) if value is not None else None


def mark_job_suppressed_if_claimed(
    job_id: int,
    claim_id: str,
    brief: dict[str, Any],
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> bool:
    data = (
        sb_admin()
        .rpc(
            "mark_job_suppressed_if_claimed",
            {
                "p_id": job_id,
                "p_claim_id": claim_id,
                "p_brief": brief,
                "p_input_tokens": input_tokens,
                "p_output_tokens": output_tokens,
            },
        )
        .execute()
        .data
    )
    return _rpc_returned_id(data)


def mark_job_failed_if_claimed(job_id: int, claim_id: str, error: str) -> bool:
    data = (
        sb_admin()
        .rpc(
            "mark_job_failed_if_claimed",
            {"p_id": job_id, "p_claim_id": claim_id, "p_error": error[:2000]},
        )
        .execute()
        .data
    )
    return _rpc_returned_id(data)


def release_job_claim(job_id: int, claim_id: str) -> bool:
    data = (
        sb_admin()
        .rpc("release_job_claim", {"p_id": job_id, "p_claim_id": claim_id})
        .execute()
        .data
    )
    return _rpc_returned_id(data)


def reap_stale_job_claims(stale_after_minutes: int = 10) -> int:
    data = (
        sb_admin()
        .rpc("reap_stale_job_claims", {"p_stale_after_minutes": stale_after_minutes})
        .execute()
        .data
    )
    return int(_rpc_scalar(data) or 0)


def bump_investigation_parse_failure(job_id: int, claim_id: str, error: str) -> int:
    data = (
        sb_admin()
        .rpc(
            "bump_investigation_parse_failure",
            {"p_id": job_id, "p_claim_id": claim_id, "p_error": error[:500]},
        )
        .execute()
        .data
    )
    return int(_rpc_scalar(data) or 0)


def release_claim(notification_id: int, claim_id: str) -> bool:
    data = (
        sb_admin()
        .rpc("release_claim", {"p_id": notification_id, "p_claim_id": claim_id})
        .execute()
        .data
    )
    return _rpc_returned_id(data)


def mark_sent_if_claimed(
    notification_id: int,
    claim_id: str,
    *,
    msg_id: str,
    rendered_text: str,
) -> bool:
    data = (
        sb_admin()
        .rpc(
            "mark_sent_if_claimed",
            {
                "p_id": notification_id,
                "p_claim_id": claim_id,
                "p_msg_id": msg_id,
                "p_rendered_text": rendered_text,
            },
        )
        .execute()
        .data
    )
    return _rpc_returned_id(data)


def mark_failed_if_claimed(notification_id: int, claim_id: str, error: str) -> bool:
    data = (
        sb_admin()
        .rpc(
            "mark_failed_if_claimed",
            {"p_id": notification_id, "p_claim_id": claim_id, "p_error": error[:2000]},
        )
        .execute()
        .data
    )
    return _rpc_returned_id(data)


def mark_suppressed_if_claimed(
    notification_id: int,
    claim_id: str,
    suppressed_by: str,
    *,
    error: str | None = None,
) -> bool:
    data = (
        sb_admin()
        .rpc(
            "mark_suppressed_if_claimed",
            {
                "p_id": notification_id,
                "p_claim_id": claim_id,
                "p_suppressed_by": suppressed_by,
                "p_error": (error or "")[:2000] if error else None,
            },
        )
        .execute()
        .data
    )
    return _rpc_returned_id(data)


def reap_stale_claims(stale_after_minutes: int = 5) -> int:
    data = (
        sb_admin()
        .rpc("reap_stale_claims", {"p_stale_after_minutes": stale_after_minutes})
        .execute()
        .data
    )
    return int(data or 0)


def recent_notifications_for_scope(
    scope_kind: str,
    scope_id: str,
    since_minutes: int = 30,
) -> list[dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(minutes=since_minutes)
    rows = (
        sb_admin()
        .table("notifications")
        .select(
            "id, event_id, subscription_id, status, suppressed_by, rendered_text, "
            "decided_at, sent_at, payload_snapshot, delivery_kind, delivery_target, "
            "subscriptions!inner(scope_kind, scope_id, description)"
        )
        .eq("subscriptions.scope_kind", scope_kind)
        .eq("subscriptions.scope_id", scope_id)
        .gte("decided_at", since.isoformat())
        .order("decided_at", desc=True)
        .limit(100)
        .execute()
        .data
        or []
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload_snapshot") or {}
        out.append(
            {
                "id": row.get("id"),
                "event_id": row.get("event_id"),
                "subscription_id": row.get("subscription_id"),
                "status": row.get("status"),
                "suppressed_by": row.get("suppressed_by"),
                "decided_at": row.get("decided_at"),
                "subject_summary": (
                    row.get("rendered_text")
                    or payload.get("agent_summary")
                    or payload.get("user_message")
                    or ""
                )[:240],
                "project_root": payload.get("project_root"),
            }
        )
    return out


def recent_notifications_for_subscription(
    subscription_id: str,
    *,
    since_hours: int = 72,
    limit: int = 20,
) -> list[dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    rows = (
        sb_admin()
        .table("notifications")
        .select("id, event_id, status, suppressed_by, rendered_text, decided_at, sent_at, payload_snapshot")
        .eq("subscription_id", subscription_id)
        .gte("decided_at", since.isoformat())
        .order("decided_at", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )
    return rows


def daily_sent_count_for_scope(scope_kind: str, scope_id: str, since_local_midnight: str) -> int:
    result = (
        sb_admin()
        .table("notifications")
        .select("id, subscriptions!inner(scope_kind, scope_id)", count="exact")
        .eq("subscriptions.scope_kind", scope_kind)
        .eq("subscriptions.scope_id", scope_id)
        .eq("status", "sent")
        .gte("sent_at", since_local_midnight)
        .execute()
    )
    if getattr(result, "count", None) is not None:
        return int(result.count or 0)
    return len(result.data or [])


def lookup_notification_by_feishu_msg_id(msg_id: str) -> Optional[dict[str, Any]]:
    if not msg_id:
        return None
    row = _execute_data(
        sb_admin()
        .table("notifications")
        .select("*, subscriptions(*), events(*)")
        .eq("feishu_msg_id", msg_id)
        .maybe_single()
    )
    return row or None


def fetch_notifications_for_event_subscription_pairs(
    pairs: set[tuple[int, str]],
) -> dict[tuple[int, str], dict[str, Any]]:
    if not pairs:
        return {}
    event_ids = sorted({event_id for event_id, _ in pairs})
    subscription_ids = sorted({subscription_id for _, subscription_id in pairs})
    rows = (
        sb_admin()
        .table("notifications")
        .select("id, event_id, subscription_id, status, suppressed_by, feishu_msg_id, decided_payload_version")
        .in_("event_id", event_ids)
        .in_("subscription_id", subscription_ids)
        .execute()
        .data
        or []
    )
    return {
        (int(row["event_id"]), str(row["subscription_id"])): row
        for row in rows
        if (int(row.get("event_id")), str(row.get("subscription_id"))) in pairs
    }


def fetch_investigation_jobs_by_ids(job_ids: set[int]) -> dict[int, dict[str, Any]]:
    if not job_ids:
        return {}
    rows = (
        sb_admin()
        .table("investigation_jobs")
        .select(
            "id, subscription_id, status, seed_event_ids, initial_focus, decider_reason, "
            "investigator_decision, notification_id, attempt_count, last_error, "
            "opened_at, updated_at, closed_at, error"
        )
        .in_("id", sorted(job_ids))
        .execute()
        .data
        or []
    )
    return {int(row["id"]): row for row in rows}


def add_subscription(
    *,
    scope_kind: str,
    scope_id: str,
    description: str,
    created_by: str,
    chat_id: str | None = None,
    target_kind: str | None = None,
    target_id: str | None = None,
    target_user_open_id: str | None = None,
    consent_anchor: str | None = None,
) -> dict[str, Any]:
    payload = {
        "scope_kind": scope_kind,
        "scope_id": scope_id,
        "description": description,
        "created_by": created_by,
        "chat_id": chat_id,
    }
    if target_kind:
        payload["target_kind"] = target_kind
    if target_id:
        payload["target_id"] = target_id
    if target_user_open_id:
        payload["target_user_open_id"] = target_user_open_id
    if consent_anchor:
        payload["consent_anchor"] = consent_anchor
    res = (
        sb_admin()
        .table("subscriptions")
        .insert(payload)
        .execute()
    )
    row = res.data[0] if res and res.data else {}
    if row.get("id"):
        index_subscription_metadata(str(row["id"]))
    return row


def list_subscriptions(scope_kind: str, scope_id: str) -> list[dict[str, Any]]:
    return fetch_subscriptions_for_scope(scope_kind, scope_id)


def update_subscription(
    subscription_id: str,
    scope_kind: str,
    scope_id: str,
    **fields_to_update: Any,
) -> Optional[dict[str, Any]]:
    nullable_fields = {"target_user_open_id", "consent_anchor"}
    allowed_fields = {
        "description",
        "enabled",
        "archived_at",
        "target_kind",
        "target_id",
        "target_user_open_id",
        "consent_anchor",
    }
    payload = {
        k: v
        for k, v in fields_to_update.items()
        if k in allowed_fields and (v is not None or k in nullable_fields)
    }
    if not payload:
        return get_subscription_in_scope(subscription_id, scope_kind, scope_id)
    payload["updated_at"] = _utc_now_iso()
    res = (
        sb_admin()
        .table("subscriptions")
        .update(payload)
        .eq("id", subscription_id)
        .eq("scope_kind", scope_kind)
        .eq("scope_id", scope_id)
        .execute()
    )
    row = res.data[0] if res and res.data else None
    if row and "description" in payload:
        index_subscription_metadata(str(row["id"]))
    return row


def get_subscription_in_scope(
    subscription_id: str,
    scope_kind: str,
    scope_id: str,
) -> Optional[dict[str, Any]]:
    row = _execute_data(
        sb_admin()
        .table("subscriptions")
        .select("*")
        .eq("id", subscription_id)
        .eq("scope_kind", scope_kind)
        .eq("scope_id", scope_id)
        .maybe_single()
    )
    return row or None


def remove_subscription(subscription_id: str, scope_kind: str, scope_id: str) -> Optional[dict[str, Any]]:
    return update_subscription(
        subscription_id,
        scope_kind,
        scope_id,
        enabled=False,
        archived_at=_utc_now_iso(),
    )


def disable_subscription(subscription_id: str, reason: str | None = None) -> Optional[dict[str, Any]]:
    metadata_patch = {"disabled_reason": reason, "disabled_at": _utc_now_iso()} if reason else None
    payload: dict[str, Any] = {
        "enabled": False,
        "updated_at": _utc_now_iso(),
    }
    if metadata_patch:
        current = get_subscription(subscription_id)
        existing = current.metadata if current and isinstance(current.metadata, dict) else {}
        payload["metadata"] = {**existing, **metadata_patch}
    res = (
        sb_admin()
        .table("subscriptions")
        .update(payload)
        .eq("id", subscription_id)
        .execute()
    )
    return res.data[0] if res and res.data else None


def get_active_target_consent(target_user_id: str, source_user_id: str) -> Optional[dict[str, Any]]:
    if not target_user_id or not source_user_id:
        return None
    row = _execute_data(
        sb_admin()
        .table("target_consents")
        .select("*")
        .eq("target_user_id", target_user_id)
        .eq("source_user_id", source_user_id)
        .is_("revoked_at", "null")
        .maybe_single()
    )
    return row or None


def get_target_consent(consent_id: str) -> Optional[dict[str, Any]]:
    if not consent_id:
        return None
    row = _execute_data(
        sb_admin()
        .table("target_consents")
        .select("*")
        .eq("id", consent_id)
        .maybe_single()
    )
    return row or None


def add_target_consent(target_user_id: str, source_user_id: str) -> Optional[dict[str, Any]]:
    data = (
        sb_admin()
        .rpc(
            "add_target_consent",
            {
                "p_target_user_id": target_user_id,
                "p_source_user_id": source_user_id,
            },
        )
        .execute()
        .data
    )
    if isinstance(data, list):
        return data[0] if data else None
    return data or None


def revoke_target_consent(target_user_id: str, source_user_id: str) -> Optional[dict[str, Any]]:
    data = (
        sb_admin()
        .rpc(
            "revoke_target_consent",
            {
                "p_target_user_id": target_user_id,
                "p_source_user_id": source_user_id,
            },
        )
        .execute()
        .data
    )
    if isinstance(data, list):
        return data[0] if data else None
    return data or None


def list_target_consents_for_user(user_id: str) -> dict[str, list[dict[str, Any]]]:
    if not user_id:
        return {"granted_by_me": [], "granted_to_me": []}
    granted_by_me = (
        sb_admin()
        .table("target_consents")
        .select("*, source:source_user_id(handle, display_name)")
        .eq("target_user_id", user_id)
        .is_("revoked_at", "null")
        .order("granted_at", desc=True)
        .execute()
        .data
        or []
    )
    granted_to_me = (
        sb_admin()
        .table("target_consents")
        .select("*, target:target_user_id(handle, display_name)")
        .eq("source_user_id", user_id)
        .is_("revoked_at", "null")
        .order("granted_at", desc=True)
        .execute()
        .data
        or []
    )
    return {"granted_by_me": granted_by_me, "granted_to_me": granted_to_me}


def open_pending_target_consent(
    *,
    source_user_id: str,
    target_user_id: str,
    rule_description: str,
    request_message_id: str | None = None,
) -> dict[str, Any]:
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    existing = _execute_data(
        sb_admin()
        .table("pending_target_consents")
        .select("*")
        .eq("source_user_id", source_user_id)
        .eq("target_user_id", target_user_id)
        .eq("status", "pending")
        .maybe_single()
    )
    if existing:
        res = (
            sb_admin()
            .table("pending_target_consents")
            .update(
                {
                    "rule_description": rule_description,
                    "request_message_id": request_message_id,
                    "expires_at": expires_at,
                }
            )
            .eq("id", existing["id"])
            .select("*")
            .single()
            .execute()
        )
        return res.data if res and res.data else existing
    res = (
        sb_admin()
        .table("pending_target_consents")
        .insert(
            {
                "source_user_id": source_user_id,
                "target_user_id": target_user_id,
                "rule_description": rule_description,
                "request_message_id": request_message_id,
                "expires_at": expires_at,
            }
        )
        .select("*")
        .single()
        .execute()
    )
    return res.data if res and res.data else {}


def set_pending_target_consent_message(pending_id: str, request_message_id: str) -> Optional[dict[str, Any]]:
    res = (
        sb_admin()
        .table("pending_target_consents")
        .update({"request_message_id": request_message_id})
        .eq("id", pending_id)
        .eq("status", "pending")
        .execute()
    )
    return res.data[0] if res and res.data else None


def pending_target_consent_by_message(message_id: str) -> Optional[dict[str, Any]]:
    if not message_id:
        return None
    row = _execute_data(
        sb_admin()
        .table("pending_target_consents")
        .select("*, source:source_user_id(handle, display_name), target:target_user_id(handle, display_name)")
        .eq("request_message_id", message_id)
        .eq("status", "pending")
        .gte("expires_at", _utc_now_iso())
        .maybe_single()
    )
    return row or None


def resolve_pending_target_consent(pending_id: str, status: str) -> Optional[dict[str, Any]]:
    if status not in {"granted", "declined", "expired"}:
        raise ValueError("invalid pending target consent status")
    res = (
        sb_admin()
        .table("pending_target_consents")
        .update({"status": status, "resolved_at": _utc_now_iso()})
        .eq("id", pending_id)
        .eq("status", "pending")
        .execute()
    )
    return res.data[0] if res and res.data else None


def feishu_link_for_user_id(user_id: str) -> Optional[dict[str, Any]]:
    if not user_id:
        return None
    res = (
        sb_admin()
        .table("feishu_links")
        .select(
            "user_id, feishu_open_id, feishu_name, feishu_email, "
            "feishu_mobile, timezone, profiles!inner(handle, display_name)"
        )
        .eq("user_id", user_id)
        .maybe_single()
        .execute()
    )
    if not res or not res.data:
        return None
    row = _feishu_link_row_to_person(res.data)
    row["timezone"] = res.data.get("timezone") or "Asia/Shanghai"
    return row


def resolve_subject_open_id(user_id: str) -> dict[str, Any]:
    linked = feishu_link_for_user_id(user_id)
    if not linked:
        profile = lookup_profile_by_user_id(user_id) or {}
        return {
            "open_id": None,
            "display_name": profile.get("display_name") or profile.get("handle"),
            "handle": profile.get("handle"),
        }
    return {
        "open_id": linked.get("open_id"),
        "display_name": linked.get("display_name") or linked.get("handle"),
        "handle": linked.get("handle"),
    }


def recent_decision_logs_for_scope(
    scope_kind: str,
    scope_id: str,
    *,
    since_hours: int = 24,
    limit: int = 200,
) -> list[dict[str, Any]]:
    since = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    rows = (
        sb_admin()
        .table("decision_logs")
        .select(
            "*, subscriptions!inner(scope_kind, scope_id, description)"
        )
        .eq("subscriptions.scope_kind", scope_kind)
        .eq("subscriptions.scope_id", scope_id)
        .gte("created_at", since.isoformat())
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
        .data
        or []
    )
    pairs = {
        (int(row["event_id"]), str(row["subscription_id"]))
        for row in rows
        if row.get("event_id") is not None and row.get("subscription_id")
    }
    current_by_pair = fetch_notifications_for_event_subscription_pairs(pairs)
    jobs_by_id = fetch_investigation_jobs_by_ids(
        {int(row["investigation_job_id"]) for row in rows if row.get("investigation_job_id") is not None}
    )
    for row in rows:
        current = current_by_pair.get((int(row.get("event_id")), str(row.get("subscription_id"))))
        row["current_notification"] = {
            "status": current.get("status"),
            "suppressed_by": current.get("suppressed_by"),
            "feishu_msg_id": current.get("feishu_msg_id"),
            "decided_payload_version": current.get("decided_payload_version"),
        } if current else None
        job_id = row.get("investigation_job_id")
        row["current_investigation_job"] = jobs_by_id.get(int(job_id)) if job_id is not None else None
    return rows


def judge_parse_failure_count(event_id: int, subscription_id: str, payload_version: int) -> int:
    rows = (
        sb_admin()
        .table("decision_logs")
        .select("id, judge_output")
        .eq("event_id", event_id)
        .eq("subscription_id", subscription_id)
        .eq("payload_version", payload_version)
        .order("created_at", desc=True)
        .limit(10)
        .execute()
        .data
        or []
    )
    return sum(
        1
        for row in rows
        if (row.get("judge_output") or {}).get("suppressed_by")
        in {"judge_parse_error", "gatekeeper_parse_error"}
    )


def gatekeeper_transient_failure_count(event_id: int, subscription_id: str, payload_version: int) -> int:
    rows = (
        sb_admin()
        .table("decision_logs")
        .select("id, judge_output")
        .eq("event_id", event_id)
        .eq("subscription_id", subscription_id)
        .eq("payload_version", payload_version)
        .order("created_at", desc=True)
        .limit(10)
        .execute()
        .data
        or []
    )
    return sum(
        1
        for row in rows
        if (row.get("judge_output") or {}).get("suppressed_by")
        == "gatekeeper_transient_error"
    )


def investigation_parse_failure_count(job_id: int) -> int:
    row = _execute_data(
        sb_admin()
        .table("investigation_jobs")
        .select("attempt_count")
        .eq("id", job_id)
        .maybe_single()
    )
    return int((row or {}).get("attempt_count") or 0)
