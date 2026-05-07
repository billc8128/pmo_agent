from __future__ import annotations

import logging
import time
from typing import Any

from claude_agent_sdk import tool

from agent.request_context import RequestContext
from agent.tool_utils import err, ok
from chat_memory import people
from db import queries

logger = logging.getLogger(__name__)


def _require_group_chat(ctx: RequestContext) -> str:
    if ctx.chat_type != "group" or not ctx.chat_id:
        raise ValueError("成员认知只能在当前群聊上下文里使用")
    return ctx.chat_id


def _reject_unexpected_chat_id(args: dict) -> dict[str, Any] | None:
    if "chat_id" in args and args.get("chat_id"):
        return err("chat_id 由当前飞书群上下文决定，不能由工具参数指定")
    return None


def _require_enabled_memory(chat_id: str) -> None:
    row = queries.chat_memory_status(chat_id)
    if not row or not row.get("enabled"):
        raise ValueError("这个群还未开启 PMO 记忆。没有足够的群聊上下文来判断成员信号。")


def _cap_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        raw = int(value if value is not None else default)
    except (TypeError, ValueError):
        raw = default
    return max(minimum, min(raw, maximum))


def build_people_memory_tools(ctx: RequestContext):
    @tool(
        "get_people_context",
        "Read already-distilled PMO people context for people observed in the current Feishu group. Returns topic-scoped context only, never internal ids. Does not accept chat_id.",
        {"query": str | None, "topic": str | None},
    )
    async def get_people_context(args: dict) -> dict[str, Any]:
        started = time.monotonic()
        try:
            rejected = _reject_unexpected_chat_id(args)
            if rejected:
                logger.warning(
                    "people_memory_tool_rejected tool=get_people_context reason=unexpected_chat_id chat=%s",
                    ctx.chat_id,
                )
                return rejected
            chat_id = _require_group_chat(ctx)
            _require_enabled_memory(chat_id)
            query = (args.get("query") or "").strip() or None
            topic = (args.get("topic") or query or "").strip()
            rows = queries.people_memory_for_chat(chat_id, query=query or topic, limit=10)
            summaries = [
                summary
                for row in rows
                if (summary := people.people_signal_summary(row, topic=topic))
            ]
            logger.info(
                "people_memory_tool tool=get_people_context chat=%s row_count=%d returned=%d latency_ms=%d",
                chat_id,
                len(rows),
                len(summaries),
                int((time.monotonic() - started) * 1000),
            )
            if not summaries:
                return ok(
                    {
                        "people": [],
                        "count": 0,
                        "status": "not_enough_signal",
                        "message": "这个群的 PMO 记忆里还没有足够的成员信号。",
                    }
                )
            return ok({"people": summaries[:10], "count": len(summaries[:10])})
        except Exception as e:
            logger.info(
                "people_memory_tool tool=get_people_context chat=%s error=%s latency_ms=%d",
                ctx.chat_id,
                type(e).__name__,
                int((time.monotonic() - started) * 1000),
            )
            return err(str(e))

    @tool(
        "suggest_people_for_topic",
        "Suggest people from the current Feishu group who may be relevant to a topic. Uses server-side people memory and returns brief reasons only, never raw notes. Does not accept chat_id.",
        {"topic": str, "limit": int | None},
    )
    async def suggest_people_for_topic(args: dict) -> dict[str, Any]:
        started = time.monotonic()
        try:
            rejected = _reject_unexpected_chat_id(args)
            if rejected:
                logger.warning(
                    "people_memory_tool_rejected tool=suggest_people_for_topic reason=unexpected_chat_id chat=%s",
                    ctx.chat_id,
                )
                return rejected
            chat_id = _require_group_chat(ctx)
            _require_enabled_memory(chat_id)
            topic = (args.get("topic") or "").strip()
            if not topic:
                return err("topic is required")
            limit = _cap_int(args.get("limit"), default=5, minimum=1, maximum=10)
            rows = queries.people_memory_for_chat(chat_id, query=topic, limit=30)
            candidates = [
                summary
                for row in rows
                if (summary := people.people_signal_summary(row, topic=topic))
                and summary.get("confidence") in {"medium", "high"}
            ]
            candidates = candidates[:limit]
            logger.info(
                "people_memory_tool tool=suggest_people_for_topic chat=%s candidate_count=%d latency_ms=%d",
                chat_id,
                len(candidates),
                int((time.monotonic() - started) * 1000),
            )
            if not candidates:
                return ok(
                    {
                        "candidates": [],
                        "count": 0,
                        "status": "not_enough_signal",
                        "message": "这个群的 PMO 记忆里还没有足够证据建议具体成员。",
                    }
                )
            return ok({"candidates": candidates, "count": len(candidates)})
        except Exception as e:
            logger.info(
                "people_memory_tool tool=suggest_people_for_topic chat=%s error=%s latency_ms=%d",
                ctx.chat_id,
                type(e).__name__,
                int((time.monotonic() - started) * 1000),
            )
            return err(str(e))

    return [get_people_context, suggest_people_for_topic]
