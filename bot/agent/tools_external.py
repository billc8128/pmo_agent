from __future__ import annotations

from collections import deque
from time import monotonic
from typing import Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from agent.request_context import RequestContext
from agent.tool_utils import err, ok
from feishu import bitable, docx, links, wiki


_external_table_calls: dict[str, deque[float]] = {}


def build_external_tools(ctx: RequestContext):
    @tool(
        "resolve_feishu_link",
        "Parse a Feishu URL (docx/wiki/base/sheet) and return its kind and token.",
        {"url": str},
    )
    async def resolve_feishu_link(args: dict) -> dict[str, Any]:
        try:
            parsed = links.parse_url(args["url"])
            if parsed["kind"] == "wiki":
                node = await wiki.resolve_node(parsed["token"])
                kind_map = {"docx": "docx", "doc": "docx", "bitable": "bitable", "sheet": "sheet"}
                return ok({
                    "kind": kind_map.get(node["obj_type"], node["obj_type"]),
                    "token": node["obj_token"],
                    "via_wiki": parsed["token"],
                })
            return ok(parsed)
        except Exception as e:
            return err(str(e))

    @tool(
        "read_doc",
        "Read any Feishu docx the bot can access. Input doc_link_or_token, optional max_chars.",
        {"doc_link_or_token": str, "max_chars": int},
    )
    async def read_doc(args: dict) -> dict[str, Any]:
        try:
            token = await _normalize_doc_token(args["doc_link_or_token"])
            requested_max_chars = int(args.get("max_chars") or 20000)
            max_chars = 20000 if requested_max_chars <= 0 else min(requested_max_chars, 20000)
            blocks = await docx.list_blocks(token)
            markdown = "\n".join(filter(None, (_render_block(b) for b in blocks)))
            char_count = len(markdown)
            truncated = char_count > max_chars
            if truncated:
                markdown = markdown[:max_chars] + f"\n\n[... 文档已截断，剩余 {char_count - max_chars} 字符]"
            return ok({
                "doc_token": token,
                "markdown": markdown,
                "char_count": char_count,
                "truncated": truncated,
                "max_chars": max_chars,
                "max_chars_was_capped": requested_max_chars > max_chars,
                **(
                    {
                        "requested_max_chars": requested_max_chars,
                        "warning": "max_chars is capped at 20000 for this tool call.",
                    }
                    if requested_max_chars > max_chars
                    else {}
                ),
            })
        except Exception as e:
            msg = str(e)
            if "99991663" in msg or "Permission" in msg or "403" in msg:
                return err("我没有权限读这个文档。请把它分享给 @包工头，或者把内容复制粘贴给我")
            return err(msg)

    @tool(
        "read_external_table",
        "Read an external Feishu Bitable by URL or app_token:table_id. Rate-limited to 5/hour/conversation.",
        {"link_or_app_table_token": str, "filter": str, "page_size": int, "page_token": str},
    )
    async def read_external_table(args: dict) -> dict[str, Any]:
        key = ctx.conversation_key or "anon"
        now = monotonic()
        _prune_external_table_calls(now)
        calls = _external_table_calls.get(key)
        if calls and len(calls) >= 5:
            return err("read_external_table 每小时每会话最多 5 次。请改用文字描述或缩小范围。")
        try:
            app_token, table_id = await _normalize_table(args["link_or_app_table_token"])
            page_size = min(int(args.get("page_size") or 50), 200)
            result = await bitable.search_records(
                app_token=app_token,
                table_id=table_id,
                filter=args.get("filter") or None,
                page_size=page_size,
                page_token=args.get("page_token") or None,
            )
        except Exception as e:
            return err(str(e))
        calls = _external_table_calls.setdefault(key, deque())
        calls.append(now)
        return ok(result)

    return [resolve_feishu_link, read_doc, read_external_table]


def _prune_external_table_calls(now: float) -> None:
    for key in list(_external_table_calls.keys()):
        calls = _external_table_calls[key]
        while calls and now - calls[0] > 3600:
            calls.popleft()
        if not calls:
            del _external_table_calls[key]


async def _normalize_doc_token(value: str) -> str:
    if "://" not in value:
        return value
    parsed = links.parse_url(value)
    if parsed["kind"] == "wiki":
        node = await wiki.resolve_node(parsed["token"])
        return node["obj_token"]
    if parsed["kind"] != "docx":
        raise ValueError("not a docx URL")
    return parsed["token"]


async def _normalize_table(value: str) -> tuple[str, str]:
    if "://" in value:
        parsed = links.parse_url(value)
        if parsed["kind"] == "wiki":
            node = await wiki.resolve_node(parsed["token"])
            if node["obj_type"] != "bitable":
                raise ValueError("wiki URL does not resolve to a bitable")
            raise ValueError("wiki bitable links must include table_id; paste the base URL with ?table=")
        if parsed["kind"] != "bitable" or not parsed.get("table_id"):
            raise ValueError("need a base URL with ?table=<table_id>")
        return parsed["app_token"], parsed["table_id"]
    if ":" in value:
        app_token, table_id = value.split(":", 1)
        return app_token, table_id
    raise ValueError("expected Feishu base URL or app_token:table_id")


def _text_from(obj: Any) -> str:
    elements = getattr(obj, "elements", None)
    if not isinstance(elements, list):
        return ""
    parts: list[str] = []
    for el in elements:
        tr = getattr(el, "text_run", None)
        if tr and getattr(tr, "content", None):
            parts.append(tr.content)
    return "".join(parts)


def _render_block(block: Any) -> str:
    block_type = getattr(block, "block_type", None)
    if block_type == 3 and getattr(block, "heading1", None):
        return "# " + _text_from(block.heading1)
    if block_type == 4 and getattr(block, "heading2", None):
        return "## " + _text_from(block.heading2)
    if block_type == 5 and getattr(block, "heading3", None):
        return "### " + _text_from(block.heading3)
    if block_type == 12 and getattr(block, "bullet", None):
        return "- " + _text_from(block.bullet)
    if block_type == 13 and getattr(block, "ordered", None):
        return "1. " + _text_from(block.ordered)
    if block_type == 14 and getattr(block, "code", None):
        return "```\n" + _text_from(block.code) + "\n```"
    if block_type == 15 and getattr(block, "quote", None):
        return "> " + _text_from(block.quote)
    if block_type == 2 and getattr(block, "text", None):
        return _text_from(block.text)
    return ""


def build_external_mcp(ctx: RequestContext):
    return create_sdk_mcp_server(
        name="pmo_external",
        version="0.1.0",
        tools=build_external_tools(ctx),
    )
