from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from agent.request_context import RequestContext
from agent.tool_utils import err, logical_key, ok


def build_calendar_tools(ctx: RequestContext):
    @tool("schedule_meeting", "Schedule a Feishu calendar meeting.", {"title": str, "start_time": str, "duration_minutes": int, "attendee_open_ids": list, "description": str, "include_asker": bool})
    async def schedule_meeting(args: dict) -> dict[str, Any]:
        from agent.tools_impl import calendar_impl

        return await calendar_impl.schedule_meeting(ctx, args)

    @tool("cancel_meeting", "Cancel a bot-owned Feishu calendar meeting by event_id, or last=true for the latest one in this chat.", {"event_id": str, "last": bool})
    async def cancel_meeting(args: dict) -> dict[str, Any]:
        from agent.tools_impl import calendar_impl

        return await calendar_impl.cancel_meeting(ctx, args)

    @tool("list_my_meetings", "List meetings visible to the bot for the asker or a target open_id.", {"since": str, "until": str, "target": str, "target_open_id": str})
    async def list_my_meetings(args: dict) -> dict[str, Any]:
        from agent.tools_impl import calendar_impl

        return await calendar_impl.list_my_meetings(ctx, args)

    return [schedule_meeting, cancel_meeting, list_my_meetings]


def build_calendar_mcp(ctx: RequestContext):
    return create_sdk_mcp_server("pmo_calendar", "0.1.0", build_calendar_tools(ctx))
