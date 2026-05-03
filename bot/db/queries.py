"""Typed query helpers backing the agent's MCP tools.

Each function returns plain Python data structures (lists of dicts),
ready to JSON-encode back to the LLM. Errors raise — the tool wrapper
turns them into tool error messages the LLM can react to.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
import re

from .client import sb, sb_admin


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
