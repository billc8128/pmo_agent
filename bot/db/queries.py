"""Typed query helpers backing the agent's MCP tools.

Each function returns plain Python data structures (lists of dicts),
ready to JSON-encode back to the LLM. Errors raise — the tool wrapper
turns them into tool error messages the LLM can react to.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .client import sb


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

    project_root matches by ilike prefix — '/Users/a/Desktop/pmo_agent'
    catches turns from .../pmo_agent, .../pmo_agent/daemon, etc.
    """
    q = (
        sb()
        .table("turns")
        .select(
            "id, agent, agent_session_id, project_path, turn_index, "
            "user_message, agent_summary, device_label, "
            "user_message_at, agent_response_at"
        )
        .eq("user_id", user_id)
        .order("user_message_at", desc=True)
        .limit(limit)
    )
    if since_iso:
        q = q.gte("user_message_at", since_iso)
    if until_iso:
        q = q.lte("user_message_at", until_iso)
    if project_root:
        # Match the root and any sub-paths.
        q = q.ilike("project_path", f"{project_root}%")

    res = q.execute()
    return res.data or []


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
        # project_root heuristic mirrors web/lib/grouping.ts:
        # first 4 path components after the leading slash.
        path = r.get("project_path") or ""
        parts = path.lstrip("/").split("/") if path else []
        root = "/" + "/".join(parts[:4]) if len(parts) > 4 else (path or "(unknown)")
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
