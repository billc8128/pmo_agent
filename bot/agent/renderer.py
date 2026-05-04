from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    create_sdk_mcp_server,
    tool,
)

from agent.request_context import RequestContext
from agent.tool_utils import err, ok
from config import settings
from db import queries

logger = logging.getLogger(__name__)


class RenderError(Exception):
    pass


_RENDERER_PROMPT = """你是 pmo_agent 的主动通知渲染器。你的任务是把一条已批准的通知写成飞书里可读的中文 markdown。

约束：
- 输出 200-400 字符，直接说结论，不要解释你调用了什么工具。
- 必须围绕 event_payload 和订阅 description 写，不要编造不存在的事实。
- 如果是群通知，优先用 resolve_subject_mention 得到 open_id，并可用 `<at user_id="ou_xxx"></at>` 提及事件主体；解析不到就用 @handle 或 display_name。
- 可以调用只读工具补充背景，但不要拉太多 raw turns。
- 不要输出 JSON，不要包含内部 id/token。
"""


def _inject_anthropic_env() -> None:
    os.environ["ANTHROPIC_AUTH_TOKEN"] = settings.anthropic_auth_token
    os.environ["ANTHROPIC_BASE_URL"] = settings.anthropic_base_url
    os.environ["ANTHROPIC_MODEL"] = settings.anthropic_model
    os.environ["ANTHROPIC_DEFAULT_OPUS_MODEL"] = settings.anthropic_default_opus_model
    os.environ["ANTHROPIC_DEFAULT_SONNET_MODEL"] = settings.anthropic_default_sonnet_model
    os.environ["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = settings.anthropic_default_haiku_model
    os.environ["API_TIMEOUT_MS"] = settings.api_timeout_ms
    os.environ["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = settings.claude_code_disable_nonessential_traffic


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    return value


def _renderer_mcp(ctx: RequestContext):
    @tool("list_users", "List all known users.", {})
    async def list_users(args: dict) -> dict[str, Any]:
        try:
            return ok({"users": queries.list_profiles()})
        except Exception as e:
            return err(str(e))

    @tool("lookup_user", "Resolve a single handle to a user record.", {"handle": str})
    async def lookup_user(args: dict) -> dict[str, Any]:
        try:
            rec = queries.lookup_profile(args.get("handle") or "")
            return ok({"found": bool(rec), **(rec or {})})
        except Exception as e:
            return err(str(e))

    @tool(
        "get_recent_turns",
        "Fetch recent turns for a user, optionally narrowed to a time window or project root.",
        {"user_id": str, "since": str, "until": str, "project_root": str, "limit": int},
    )
    async def get_recent_turns(args: dict) -> dict[str, Any]:
        try:
            rows = queries.recent_turns(
                args["user_id"],
                since_iso=args.get("since") or None,
                until_iso=args.get("until") or None,
                project_root=args.get("project_root") or None,
                limit=min(int(args.get("limit") or 20), 50),
            )
            return ok({"turns": rows, "count": len(rows)})
        except Exception as e:
            return err(str(e))

    @tool("get_project_overview", "Fetch cached per-project summaries for a user.", {"user_id": str})
    async def get_project_overview(args: dict) -> dict[str, Any]:
        try:
            return ok({"projects": queries.project_overview(args["user_id"])})
        except Exception as e:
            return err(str(e))

    @tool("get_activity_stats", "Aggregate turn counts for the last N days.", {"user_id": str, "days": int})
    async def get_activity_stats(args: dict) -> dict[str, Any]:
        try:
            return ok(queries.turn_counts_by_window(args["user_id"], days=int(args.get("days") or 7)))
        except Exception as e:
            return err(str(e))

    @tool("today_iso", "Return current date/time anchors.", {})
    async def today_iso(args: dict) -> dict[str, Any]:
        now_utc = datetime.now(timezone.utc)
        try:
            zone = ZoneInfo("Asia/Shanghai")
        except ZoneInfoNotFoundError:
            zone = ZoneInfo("UTC")
        now = now_utc.astimezone(zone)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return ok(
            {
                "now_utc": now_utc.isoformat(),
                "now": now.isoformat(),
                "current_date": now.date().isoformat(),
                "today_start": today_start.isoformat(),
                "yesterday_start": (today_start - timedelta(days=1)).isoformat(),
                "seven_days_ago": (now - timedelta(days=7)).isoformat(),
            }
        )

    @tool("resolve_subject_mention", "Resolve a pmo_agent user_id to a Feishu open_id.", {"user_id": str})
    async def resolve_subject_mention(args: dict) -> dict[str, Any]:
        try:
            return ok(queries.resolve_subject_open_id(args.get("user_id") or ""))
        except Exception as e:
            return err(str(e))

    return create_sdk_mcp_server(
        name="pmo_renderer",
        version="0.1.0",
        tools=[
            list_users,
            lookup_user,
            get_recent_turns,
            get_project_overview,
            get_activity_stats,
            today_iso,
            resolve_subject_mention,
        ],
    )


async def _render_inner(
    notif_row: Any,
    event_payload: dict[str, Any],
    subscription: Any,
) -> str:
    _inject_anthropic_env()
    ctx = RequestContext(conversation_key=f"notification:{getattr(notif_row, 'id', '')}")
    options = ClaudeAgentOptions(
        system_prompt=_RENDERER_PROMPT,
        allowed_tools=[
            "mcp__pmo_renderer__list_users",
            "mcp__pmo_renderer__lookup_user",
            "mcp__pmo_renderer__get_recent_turns",
            "mcp__pmo_renderer__get_project_overview",
            "mcp__pmo_renderer__get_activity_stats",
            "mcp__pmo_renderer__today_iso",
            "mcp__pmo_renderer__resolve_subject_mention",
        ],
        mcp_servers={"pmo_renderer": _renderer_mcp(ctx)},
        disallowed_tools=[
            "Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Task", "TodoWrite",
        ],
        max_turns=4,
    )
    message = {
        "notification": _plain(notif_row),
        "event_payload": event_payload,
        "subscription": _plain(subscription),
    }
    client = ClaudeSDKClient(options=options)
    chunks: list[str] = []
    try:
        await client.connect()
        await client.query(json.dumps(message, ensure_ascii=False, default=str))
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
            elif isinstance(msg, ResultMessage):
                break
    finally:
        await client.disconnect()
    return "\n".join(chunks).strip()


async def render_notification(
    notif_row: Any,
    event_payload: dict[str, Any],
    subscription: Any,
) -> str:
    try:
        text = await asyncio.wait_for(
            _render_inner(notif_row, event_payload, subscription),
            timeout=settings.notification_render_max_seconds,
        )
    except asyncio.TimeoutError as e:
        raise RenderError("renderer timed out") from e
    if not text:
        raise RenderError("renderer returned empty text")
    return text
