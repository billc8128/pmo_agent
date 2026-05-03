from __future__ import annotations

from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from agent.request_context import RequestContext


def build_doc_tools(ctx: RequestContext):
    from agent.tools_impl import doc_impl

    @tool("create_meeting_doc", "Create a meeting-notes docx in the bot docs folder.", {"title": str, "markdown_body": str, "meeting_event_id": str})
    async def create_meeting_doc(args: dict) -> dict[str, Any]:
        return await doc_impl.create_meeting_doc(ctx, args)

    @tool("create_doc", "Create a generic docx in the bot docs folder.", {"title": str, "markdown_body": str})
    async def create_doc(args: dict) -> dict[str, Any]:
        return await doc_impl.create_doc(ctx, args)

    @tool("append_to_doc", "Append markdown to a bot-authored docx.", {"doc_link_or_token": str, "markdown_body": str, "heading": str})
    async def append_to_doc(args: dict) -> dict[str, Any]:
        return await doc_impl.append_to_doc(ctx, args)

    return [create_meeting_doc, create_doc, append_to_doc]


def build_doc_mcp(ctx: RequestContext):
    return create_sdk_mcp_server("pmo_doc", "0.1.0", build_doc_tools(ctx))
