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

from agent import imaging
from db import queries


# Set by app.py before each agent run so the image tool can rate-limit
# per conversation and so the marker output references the right scope.
_current_conversation_key_var: str = ""


def set_current_conversation(conversation_key: str) -> None:
    global _current_conversation_key_var
    _current_conversation_key_var = conversation_key


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
    "canonical absolute path like '/Users/a/Desktop/pmo_agent'. "
    "Pass user_id from a previous lookup_user call.",
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
    "generate_image",
    "Generate an image with doubao-seedream and embed it into your reply. "
    "Use when the user asks for a portrait, sketch, illustration, "
    "or visualization based on someone's work — e.g. \"画一下 bcc 的样子\", "
    "\"给团队画一张合影\", \"想象一下这个项目的视觉风格\". \n\n"
    "PROMPT GUIDANCE: Be concrete. Anchor the image in specifics you "
    "already discovered from the data — projects, tools, vibes — rather "
    "than generic descriptions. Stylized illustration is preferred over "
    "photorealistic to avoid pretending a real photo exists. Size "
    "values: use '2K' (default, square), '4K', or aspect ratios like "
    "'16:9' / '9:16' / '1:1'. doubao-seedream-5.0-lite requires at "
    "least ~3.7M pixels — do not pass small sizes like '1024x1024'.\n\n"
    "RETURN: A dict with image_key (Feishu's reference) and image_url. "
    "To DISPLAY the image in your reply, include the literal token "
    "[IMAGE:<image_key>] anywhere in your final answer text — the host "
    "app will replace it with a separate image message in the chat. "
    "Do NOT paste the image_url as a link; users see it as a real image "
    "via the marker. \n\n"
    "RATE LIMIT: 5 images per hour per conversation. If you hit the "
    "limit, the tool returns {error: ...} — apologize and continue with "
    "text only.",
    {"prompt": str, "size": str},
)
async def generate_image(args: dict) -> dict[str, Any]:
    try:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return _err("prompt is required")
        size = (args.get("size") or "2K").strip()
        result = await imaging.generate_and_upload(
            conversation_key=_current_conversation_key_var or "anon",
            prompt=prompt,
            size=size,
        )
        return _ok(result)
    except Exception as e:
        return _err(f"{type(e).__name__}: {e}")


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
            generate_image,
            today_iso,
        ],
    )
