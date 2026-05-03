from __future__ import annotations

from agent.request_context import RequestContext
from agent.tools_calendar import build_calendar_mcp, build_calendar_tools
from agent.tools_external import build_external_tools
from agent.tools_meta import build_meta_mcp, build_meta_tools


def test_request_context_closure_sees_mutations():
    ctx = RequestContext()
    captured: list[str] = []

    def closure_reader():
        captured.append(ctx.message_id)

    ctx.message_id = "first"
    closure_reader()
    ctx.message_id = "second"
    closure_reader()

    assert captured == ["first", "second"]


def test_mcp_builders_return_sdk_servers_with_truthy_tools():
    ctx = RequestContext()
    meta = build_meta_mcp(ctx)
    calendar = build_calendar_mcp(ctx)
    assert meta["name"] == "pmo_meta"
    assert calendar["name"] == "pmo_calendar"
    assert build_meta_tools(ctx)
    assert build_calendar_tools(ctx)


def test_external_tools_are_real_not_private_sdk_state():
    ctx = RequestContext()
    names = [tool_def.name for tool_def in build_external_tools(ctx)]
    assert "resolve_feishu_link" in names
    assert "read_doc" in names
