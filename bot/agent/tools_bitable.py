from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from agent.request_context import RequestContext


def build_bitable_tools(ctx: RequestContext):
    from agent.tools_impl import bitable_impl

    @tool("append_action_items", "Append action items into the bot action_items table.", {"items": list, "project": str, "meeting_event_id": str})
    async def append_action_items(args: dict) -> dict[str, Any]:
        return await bitable_impl.append_action_items(ctx, args)

    @tool("query_action_items", "Query the bot action_items table.", {"owner_open_id": str, "project": str, "status": str, "since": str, "until": str, "filter": str, "page_size": int})
    async def query_action_items(args: dict) -> dict[str, Any]:
        return await bitable_impl.query_action_items(ctx, args)

    @tool("create_bitable_table", "Create a table inside the bot workspace base.", {"name": str, "fields": list})
    async def create_bitable_table(args: dict) -> dict[str, Any]:
        return await bitable_impl.create_bitable_table(ctx, args)

    @tool("append_to_my_table", "Append records to a bot-owned table.", {"table_id": str, "records": list})
    async def append_to_my_table(args: dict) -> dict[str, Any]:
        return await bitable_impl.append_to_my_table(ctx, args)

    @tool("query_my_table", "Query rows from a bot-owned table.", {"table_id": str, "filter": str, "page_size": int, "page_token": str})
    async def query_my_table(args: dict) -> dict[str, Any]:
        return await bitable_impl.query_my_table(ctx, args)

    @tool("describe_my_table", "Describe schema of a bot-owned table.", {"table_id": str})
    async def describe_my_table(args: dict) -> dict[str, Any]:
        return await bitable_impl.describe_my_table(ctx, args)

    return [
        append_action_items,
        query_action_items,
        create_bitable_table,
        append_to_my_table,
        query_my_table,
        describe_my_table,
    ]


def build_bitable_mcp(ctx: RequestContext):
    return create_sdk_mcp_server("pmo_bitable", "0.1.0", build_bitable_tools(ctx))
