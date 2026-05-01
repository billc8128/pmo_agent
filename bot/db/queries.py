"""Typed query helpers backing the agent's MCP tools.

Each function returns plain Python data structures (lists of dicts),
ready to JSON-encode back to the LLM. Errors raise — the tool wrapper
turns them into tool error messages the LLM can react to.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

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
        .select("user_id, feishu_name, feishu_email, profiles!inner(handle, display_name)")
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
