"""MCP tools the PMO agent can call.

The agent's job is to answer free-form questions like:
  - "bcc 昨天做了啥？"
  - "albert 这周在干嘛？"
  - "谁最近最活跃？"
  - "vibelive 这个项目最近有什么进展？"

We expose just enough tools to let the agent answer these, and refuse
to expose write-side capabilities — this is a read-only assistant.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from supabase import queries


# ──────────────────────────────────────────────────────────────────────
# Helpers — the @tool decorator expects an async function returning
# plain JSON-serializable dicts. We wrap every supabase call in our
# own try/except so the agent gets a usable error rather than the
# whole tool crashing the SDK loop.
# ──────────────────────────────────────────────────────────────────────


def _ok(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _err(message: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps({"error": message}, ensure_ascii=False)}],
        "isError": True,
    }


# ──────────────────────────────────────────────────────────────────────
# Tools
# ──────────────────────────────────────────────────────────────────────


@tool(
    "list_users",
    "List all known users (handle, display name, when they joined). "
    "Use this when the question doesn't pin to a specific person — e.g. "
    "'who's here?', '谁最近最活跃' — so you can pick the right handles "
    "to query in detail.",
    {},
)
async def list_users(args: dict) -> dict[str, Any]:
    try:
        rows = queries.list_profiles()
        return _ok({"users": rows})
    except Exception as e:
        return _err(str(e))


@tool(
    "lookup_user",
    "Resolve a single handle to a user record. Use this to get the "
    "user_id required by the other turn-related tools. Accepts handles "
    "with or without a leading '@'.",
    {"handle": str},
)
async def lookup_user(args: dict) -> dict[str, Any]:
    try:
        h = args.get("handle", "")
        rec = queries.lookup_profile(h)
        if not rec:
            return _ok({"found": False, "handle": h})
        return _ok({"found": True, **rec})
    except Exception as e:
        return _err(str(e))


@tool(
    "get_recent_turns",
    "Fetch up to N recent turns for a user, optionally narrowed to a "
    "time window or project root. Returns user prompts, one-sentence "
    "agent summaries, and metadata (agent type, project, device). "
    "Use this when you need concrete activity to summarize.\n\n"
    "since / until are ISO-8601 timestamps. project_root is an "
    "absolute path like '/Users/a/Desktop/pmo_agent' (matches sub-paths "
    "too). Pass user_id from a previous lookup_user call.",
    {
        "user_id": str,
        "since": str,
        "until": str,
        "project_root": str,
        "limit": int,
    },
)
async def get_recent_turns(args: dict) -> dict[str, Any]:
    try:
        user_id = args["user_id"]
        since = args.get("since") or None
        until = args.get("until") or None
        project_root = args.get("project_root") or None
        limit = int(args.get("limit") or 50)
        rows = queries.recent_turns(
            user_id,
            since_iso=since,
            until_iso=until,
            project_root=project_root,
            limit=limit,
        )
        return _ok({"turns": rows, "count": len(rows)})
    except Exception as e:
        return _err(str(e))


@tool(
    "get_project_overview",
    "Fetch the cached per-project narrative summaries for a user. Use "
    "this for 'what projects has X been working on' or 'give me a "
    "high-level on bcc' — it's faster and richer than enumerating turns.",
    {"user_id": str},
)
async def get_project_overview(args: dict) -> dict[str, Any]:
    try:
        rows = queries.project_overview(args["user_id"])
        return _ok({"projects": rows})
    except Exception as e:
        return _err(str(e))


@tool(
    "get_activity_stats",
    "Aggregate turn counts for the last N days, broken down by project "
    "and by day. Useful for 'how active has X been' / 'who's been busy "
    "this week' style questions. Default days=7.",
    {"user_id": str, "days": int},
)
async def get_activity_stats(args: dict) -> dict[str, Any]:
    try:
        days = int(args.get("days") or 7)
        stats = queries.turn_counts_by_window(args["user_id"], days=days)
        return _ok(stats)
    except Exception as e:
        return _err(str(e))


@tool(
    "today_iso",
    "Return the current UTC date and a few useful time anchors (today's "
    "ISO, yesterday's start, 7d ago, 30d ago). Call this at the start "
    "of any time-sensitive question so you don't guess.",
    {},
)
async def today_iso(args: dict) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return _ok(
        {
            "now": now.isoformat(),
            "today_start": today_start.isoformat(),
            "yesterday_start": (today_start - timedelta(days=1)).isoformat(),
            "yesterday_end": today_start.isoformat(),
            "seven_days_ago": (now - timedelta(days=7)).isoformat(),
            "thirty_days_ago": (now - timedelta(days=30)).isoformat(),
        }
    )


# ──────────────────────────────────────────────────────────────────────
# MCP server registration — claude-agent-sdk picks up these tools.
# ──────────────────────────────────────────────────────────────────────


def build_pmo_mcp():
    return create_sdk_mcp_server(
        name="pmo",
        version="0.1.0",
        tools=[
            list_users,
            lookup_user,
            get_recent_turns,
            get_project_overview,
            get_activity_stats,
            today_iso,
        ],
    )
