from __future__ import annotations

import logging
import time
from typing import Any

from claude_agent_sdk import tool

from agent.request_context import RequestContext
from agent.tool_utils import err, ok
from chat_memory import ingest as chat_memory_ingest
from db import queries

logger = logging.getLogger(__name__)


def _require_group_chat(ctx: RequestContext) -> str:
    if ctx.chat_type != "group" or not ctx.chat_id:
        raise ValueError("聊天记忆只能在群聊里开启、关闭或查询")
    return ctx.chat_id


def _actor(ctx: RequestContext) -> dict[str, str | None]:
    return {"user_id": ctx.asker_user_id, "open_id": ctx.sender_open_id or None}


def _status_payload(row: dict[str, Any] | None, chat_id: str) -> dict[str, Any]:
    row = row or {}
    return {
        "enabled": bool(row.get("enabled")),
        "enabled_at": row.get("enabled_at"),
        "enabled_by": {
            "user_id": row.get("enabled_by_user_id"),
            "open_id": row.get("enabled_by_open_id"),
        },
        "retention_days": int(row.get("retention_days") or 90),
        "observer_enabled": bool(row.get("observer_enabled")),
        "chat_id": chat_id,
    }


def _reject_unexpected_chat_id(args: dict) -> dict[str, Any] | None:
    if "chat_id" in args and args.get("chat_id"):
        return err("chat_id 由当前飞书群上下文决定，不能由工具参数指定")
    return None


def _require_enabled_memory(chat_id: str) -> dict[str, Any]:
    row = queries.chat_memory_status(chat_id)
    if not row or not row.get("enabled"):
        raise ValueError("这个群还未开启 PMO 记忆。先在群里让 bot 开始记录这个群。")
    return row


def _cap_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        raw = int(value if value is not None else default)
    except (TypeError, ValueError):
        raw = default
    return max(minimum, min(raw, maximum))


def build_chat_memory_tools(ctx: RequestContext):
    @tool(
        "enable_chat_memory",
        "Enable passive PMO chat memory for the current Feishu group. Uses the current chat only; does not accept arbitrary chat_id.",
        {"retention_days": int | None},
    )
    async def enable_chat_memory(args: dict) -> dict[str, Any]:
        try:
            chat_id = _require_group_chat(ctx)
            retention_days = int(args.get("retention_days") or 90)
            if retention_days < 1 or retention_days > 730:
                return err("retention_days must be between 1 and 730")
            actor = _actor(ctx)
            row = queries.enable_chat_memory(
                chat_id,
                user_id=actor["user_id"],
                open_id=actor["open_id"],
                retention_days=retention_days,
            )
            chat_memory_ingest.invalidate_chat_memory_cache(chat_id)
            logger.info(
                "chat_memory_status_changed action=enable chat=%s actor_user=%s actor_open=%s retention_days=%d result=enabled",
                chat_id,
                actor["user_id"],
                actor["open_id"],
                retention_days,
            )
            return ok(
                {
                    "status": "enabled",
                    "chat_id": chat_id,
                    "settings": row,
                    "public_notice": (
                        "已开始记录这个群的文字/富文本聊天记忆。PMO bot 之后可以基于本群已记录内容回答“刚才/我们聊过的/TODO”这类问题。"
                        "任何群成员都可以让 bot 停止记录。"
                    ),
                }
            )
        except Exception as e:
            return err(str(e))

    @tool(
        "disable_chat_memory",
        "Disable passive PMO chat memory for the current Feishu group. Any current chat member may disable it.",
        {},
    )
    async def disable_chat_memory(args: dict) -> dict[str, Any]:
        try:
            chat_id = _require_group_chat(ctx)
            actor = _actor(ctx)
            row = queries.disable_chat_memory(
                chat_id,
                user_id=actor["user_id"],
                open_id=actor["open_id"],
            )
            chat_memory_ingest.invalidate_chat_memory_cache(chat_id)
            logger.info(
                "chat_memory_status_changed action=disable chat=%s actor_user=%s actor_open=%s result=disabled",
                chat_id,
                actor["user_id"],
                actor["open_id"],
            )
            return ok({"status": "disabled", "chat_id": chat_id, "settings": row})
        except Exception as e:
            return err(str(e))

    @tool(
        "chat_memory_status",
        "Return passive PMO chat memory status for the current Feishu group as structured fields.",
        {},
    )
    async def chat_memory_status(args: dict) -> dict[str, Any]:
        try:
            chat_id = _require_group_chat(ctx)
            row = queries.chat_memory_status(chat_id)
            return ok(_status_payload(row, chat_id))
        except Exception as e:
            return err(str(e))

    @tool(
        "get_recent_chat_messages",
        "Return recent passive chat-memory messages from the current Feishu group only. Does not accept chat_id.",
        {"since": str | None, "until": str | None, "limit": int | None, "sender": str | None},
    )
    async def get_recent_chat_messages(args: dict) -> dict[str, Any]:
        started = time.monotonic()
        try:
            rejected = _reject_unexpected_chat_id(args)
            if rejected:
                logger.warning(
                    "chat_memory_tool_rejected tool=get_recent_chat_messages reason=unexpected_chat_id chat=%s",
                    ctx.chat_id,
                )
                return rejected
            chat_id = _require_group_chat(ctx)
            _require_enabled_memory(chat_id)
            limit = _cap_int(args.get("limit"), default=80, minimum=1, maximum=120)
            rows = queries.get_recent_chat_messages(
                chat_id,
                since=args.get("since") or None,
                until=args.get("until") or None,
                limit=limit,
                sender=args.get("sender") or None,
            )
            logger.info(
                "chat_memory_tool tool=get_recent_chat_messages chat=%s row_count=%d latency_ms=%d",
                chat_id,
                len(rows),
                int((time.monotonic() - started) * 1000),
            )
            return ok({"messages": rows, "count": len(rows)})
        except Exception as e:
            logger.info(
                "chat_memory_tool tool=get_recent_chat_messages chat=%s error=%s latency_ms=%d",
                ctx.chat_id,
                type(e).__name__,
                int((time.monotonic() - started) * 1000),
            )
            return err(str(e))

    @tool(
        "search_chat_messages_with_context",
        "Search current Feishu group chat memory and return each hit with same-chat context. Context includes the hit itself. Use for '刚才/今天/我们聊的/TODO/达成一致' questions. Does not accept chat_id.",
        {
            "query": str | None,
            "anchor_message_id": str | None,
            "since": str | None,
            "until": str | None,
            "limit": int | None,
            "before": int | None,
            "after": int | None,
        },
    )
    async def search_chat_messages_with_context(args: dict) -> dict[str, Any]:
        started = time.monotonic()
        try:
            rejected = _reject_unexpected_chat_id(args)
            if rejected:
                logger.warning(
                    "chat_memory_tool_rejected tool=search_chat_messages_with_context reason=unexpected_chat_id chat=%s",
                    ctx.chat_id,
                )
                return rejected
            chat_id = _require_group_chat(ctx)
            _require_enabled_memory(chat_id)
            query = (args.get("query") or "").strip()
            anchor_message_id = (args.get("anchor_message_id") or "").strip() or None
            if not query and not anchor_message_id:
                return err("query or anchor_message_id is required")
            limit = _cap_int(args.get("limit"), default=8, minimum=1, maximum=10)
            before = _cap_int(args.get("before"), default=8, minimum=0, maximum=10)
            after = _cap_int(args.get("after"), default=8, minimum=0, maximum=10)
            hits = queries.search_chat_messages_with_context(
                chat_id,
                query=query or None,
                anchor_message_id=anchor_message_id,
                since=args.get("since") or None,
                until=args.get("until") or None,
                limit=limit,
                before=before,
                after=after,
            )
            context_rows = sum(len(hit.get("context") or []) for hit in hits)
            logger.info(
                "chat_memory_tool tool=search_chat_messages_with_context chat=%s hit_count=%d context_row_count=%d latency_ms=%d",
                chat_id,
                len(hits),
                context_rows,
                int((time.monotonic() - started) * 1000),
            )
            return ok({"hits": hits, "count": len(hits)})
        except Exception as e:
            logger.info(
                "chat_memory_tool tool=search_chat_messages_with_context chat=%s error=%s latency_ms=%d",
                ctx.chat_id,
                type(e).__name__,
                int((time.monotonic() - started) * 1000),
            )
            return err(str(e))

    return [
        enable_chat_memory,
        disable_chat_memory,
        chat_memory_status,
        get_recent_chat_messages,
        search_chat_messages_with_context,
    ]
