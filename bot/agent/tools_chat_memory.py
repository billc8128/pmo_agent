from __future__ import annotations

import logging
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
        "observer_enabled": False,
        "chat_id": chat_id,
    }


def build_chat_memory_tools(ctx: RequestContext):
    @tool(
        "enable_chat_memory",
        "Enable passive PMO chat memory for the current Feishu group. Uses the current chat only; does not accept arbitrary chat_id.",
        {"retention_days": int},
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

    return [enable_chat_memory, disable_chat_memory, chat_memory_status]

