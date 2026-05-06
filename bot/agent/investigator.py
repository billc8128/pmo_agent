from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, is_dataclass
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

from agent import decider
from agent.request_context import RequestContext
from agent.tool_utils import err, ok
from config import settings
from db import queries

logger = logging.getLogger(__name__)


class TransientInvestigatorError(Exception):
    pass


@dataclass
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None


_INVESTIGATOR_PROMPT = """你是 pmo_agent 的 PMO 调查员。一条订阅触发了一组事件需要你判断和撰写。
你有完整的只读 PMO 工具集，可以读 turn 详情、项目概览、最近活动统计、最近通知历史等。

输入：
- subscription.description: 订阅的原始自然语言
- subscription.created_at: 订阅创建时间（早于此的事件不要算证据）
- seed_events: 触发这次调查的事件列表（已经 plausibly 相关）
- recent_notifications_for_subscription: 这条订阅最近已经通知过什么

你的任务：
1. 读 enough context，判断是否真的值得通知。
2. 如果不值得，输出 notify=false，并给出 suppressed_by/reason。
3. 如果值得，输出一个结构化 brief。brief 是事实边界，renderer 只能按它组词，不能加入新事实。

不要因为 seed event 存在就默认通知。只有当这些事件对 subscription.description 代表实质进展、风险、完成、阻塞、重要变更时才通知。
不要重复通知 recent_notifications_for_subscription 已经覆盖过的同一事实。

输出严格 JSON：
{
  "notify": true | false,
  "headline": "要通知时的一句话标题；不通知可为空",
  "key_facts": ["事实1", "事实2"],
  "evidence_event_ids": [123, 456],
  "subject_user_ids": ["pmo_agent profile uuid"],
  "suppressed_by": null | "not_enough_signal" | "duplicate" | "mismatch",
  "reason": "一句话 audit 理由"
}
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


def _investigator_mcp(ctx: RequestContext):
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
                limit=min(int(args.get("limit") or 20), settings.investigator_max_turns_context),
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

    return create_sdk_mcp_server(
        name="pmo_investigator",
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


async def investigate(bundle: Any) -> tuple[dict[str, Any], Usage]:
    _inject_anthropic_env()
    ctx = RequestContext(conversation_key=f"investigation:{getattr(bundle.job, 'id', '')}")
    options = ClaudeAgentOptions(
        system_prompt=_INVESTIGATOR_PROMPT,
        allowed_tools=[
            "mcp__pmo_investigator__list_users",
            "mcp__pmo_investigator__lookup_user",
            "mcp__pmo_investigator__get_recent_turns",
            "mcp__pmo_investigator__get_project_overview",
            "mcp__pmo_investigator__get_activity_stats",
            "mcp__pmo_investigator__today_iso",
        ],
        mcp_servers={"pmo_investigator": _investigator_mcp(ctx)},
        disallowed_tools=[
            "Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Task", "TodoWrite",
        ],
        max_turns=settings.investigator_max_turns,
    )
    message = {
        "investigation_job": _plain(bundle.job),
        "subscription": _plain(bundle.subscription),
        "seed_events": bundle.events,
        "recent_notifications_for_subscription": bundle.recent_notifications_for_subscription,
    }
    client = ClaudeSDKClient(options=options)
    started = time.monotonic()
    chunks: list[str] = []
    usage = Usage()
    try:
        await client.connect()
        await client.query(json.dumps(message, ensure_ascii=False, default=str))
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        chunks.append(block.text)
            elif isinstance(msg, ResultMessage):
                usage.input_tokens, usage.output_tokens = decider._usage_from_result_message(msg)
                break
    finally:
        await client.disconnect()

    raw_text = "\n".join(chunks)
    try:
        brief = decider.parse_decision_json(raw_text)
    except (json.JSONDecodeError, ValueError) as e:
        raise decider.DecisionParseError(
            str(e),
            raw_text=raw_text,
            raw_input=message,
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        ) from e
    if not isinstance(brief.get("notify"), bool):
        raise decider.DecisionParseError(
            "investigator output missing boolean notify",
            raw_text=raw_text,
            raw_input=message,
            latency_ms=int((time.monotonic() - started) * 1000),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
        )
    return brief, usage
