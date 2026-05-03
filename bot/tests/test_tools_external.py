from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from agent.request_context import RequestContext
from agent.tools_external import _external_table_calls, build_external_tools


def _tool(ctx: RequestContext, name: str):
    return next(t for t in build_external_tools(ctx) if t.name == name).handler


def test_resolve_wiki_redirects(monkeypatch):
    from feishu import wiki

    monkeypatch.setattr(
        wiki,
        "resolve_node",
        AsyncMock(return_value={"obj_token": "dxBBBB", "obj_type": "docx"}),
    )
    out = asyncio.run(
        _tool(RequestContext(), "resolve_feishu_link")(
            {"url": "https://example.feishu.cn/wiki/wikC"}
        )
    )
    payload = json.loads(out["content"][0]["text"])
    assert payload == {"kind": "docx", "token": "dxBBBB", "via_wiki": "wikC"}


def test_read_doc_uses_doc_link_or_token_and_20000_default(monkeypatch):
    from feishu import docx

    fake_blocks = [
        MagicMock(block_type=2, text=MagicMock(elements=[
            MagicMock(text_run=MagicMock(content="Hello world"))
        ])),
        MagicMock(block_type=4, heading2=MagicMock(elements=[
            MagicMock(text_run=MagicMock(content="Section A"))
        ])),
    ]
    monkeypatch.setattr(docx, "list_blocks", AsyncMock(return_value=fake_blocks))

    out = asyncio.run(
        _tool(RequestContext(), "read_doc")({"doc_link_or_token": "doc_xxx"})
    )
    payload = json.loads(out["content"][0]["text"])
    assert "Hello world" in payload["markdown"]
    assert "## Section A" in payload["markdown"]
    assert payload["truncated"] is False
    assert payload["max_chars"] == 20000


def test_read_doc_reports_when_max_chars_is_capped(monkeypatch):
    from feishu import docx

    fake_blocks = [
        MagicMock(block_type=2, text=MagicMock(elements=[
            MagicMock(text_run=MagicMock(content="x" * 21000))
        ])),
    ]
    monkeypatch.setattr(docx, "list_blocks", AsyncMock(return_value=fake_blocks))

    out = asyncio.run(
        _tool(RequestContext(), "read_doc")({"doc_link_or_token": "doc_xxx", "max_chars": 50000})
    )
    payload = json.loads(out["content"][0]["text"])
    assert payload["max_chars"] == 20000
    assert payload["requested_max_chars"] == 50000
    assert payload["max_chars_was_capped"] is True
    assert payload["truncated"] is True


def test_read_doc_clamps_negative_max_chars_without_truncating_from_end(monkeypatch):
    from feishu import docx

    fake_blocks = [
        MagicMock(block_type=2, text=MagicMock(elements=[
            MagicMock(text_run=MagicMock(content="hello world"))
        ])),
    ]
    monkeypatch.setattr(docx, "list_blocks", AsyncMock(return_value=fake_blocks))

    out = asyncio.run(
        _tool(RequestContext(), "read_doc")({"doc_link_or_token": "doc_xxx", "max_chars": -5})
    )
    payload = json.loads(out["content"][0]["text"])

    assert payload["truncated"] is False
    assert payload["markdown"] == "hello world"
    assert payload["max_chars"] > 0


def test_read_external_table_does_not_count_failed_normalization(monkeypatch):
    from feishu import bitable

    _external_table_calls.clear()
    ctx = RequestContext(conversation_key="conv-1")
    tool = _tool(ctx, "read_external_table")
    monkeypatch.setattr(bitable, "search_records", AsyncMock(return_value={"items": []}))

    for _ in range(5):
        out = asyncio.run(tool({"link_or_app_table_token": "not-a-table"}))
        assert out["isError"] is True

    assert "conv-1" not in _external_table_calls
    out = asyncio.run(tool({"link_or_app_table_token": "base:tbl"}))
    payload = json.loads(out["content"][0]["text"])
    assert payload == {"items": []}


def test_read_external_table_prunes_empty_old_conversation_entries(monkeypatch):
    from collections import deque

    from feishu import bitable

    _external_table_calls.clear()
    _external_table_calls["old-conv"] = deque([0.0])
    monkeypatch.setattr(bitable, "search_records", AsyncMock(return_value={"items": []}))

    out = asyncio.run(
        _tool(RequestContext(conversation_key="new-conv"), "read_external_table")(
            {"link_or_app_table_token": "base:tbl"}
        )
    )

    assert "isError" not in out
    assert "old-conv" not in _external_table_calls
