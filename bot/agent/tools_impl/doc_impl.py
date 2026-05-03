from __future__ import annotations

import logging
from typing import Any

from lark_oapi.api.docx.v1 import Block, Text, TextElement, TextRun

from agent.request_context import RequestContext
from agent.tool_utils import err, ok
from agent.tools_external import _normalize_doc_token
from agent.tools_impl.common import fail_action, start_action, workspace_or_error
from db import queries
from feishu import docx, drive

logger = logging.getLogger(__name__)


async def create_doc(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    return await _create_doc_common(ctx, args, action_type="create_doc")


async def create_meeting_doc(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    return await _create_doc_common(ctx, args, action_type="create_meeting_doc")


async def _create_doc_common(ctx: RequestContext, args: dict[str, Any], *, action_type: str) -> dict[str, Any]:
    if not args.get("title") or not args.get("markdown_body"):
        return err("title and markdown_body are required")
    ws, ws_err = workspace_or_error()
    if ws_err:
        return ws_err
    row, replay = start_action(ctx, action_type, args)
    if replay:
        return replay
    try:
        created = await _drive_import_markdown(
            action_id=row["id"],
            title=args["title"],
            markdown_body=args["markdown_body"],
            folder_token=ws["docs_folder_token"],
        )
        doc_token = created["doc_token"]
        queries.record_bot_action_target_pending(
            row["id"],
            target_id=doc_token,
            target_kind="docx",
            result_patch=created,
        )
        queries.mark_bot_action_success(row["id"], created)
        return ok(created)
    except Exception as e:
        return fail_action(row, e)


async def _drive_import_markdown(
    *,
    action_id: str,
    title: str,
    markdown_body: str,
    folder_token: str,
) -> dict[str, Any]:
    source_file_token = ""
    ticket = ""
    try:
        source_file_token = await drive.upload_markdown_source(title, markdown_body, folder_token)
        logger.info("doc.upload ok file_token=%s action=%s", source_file_token, action_id)
        queries.record_bot_action_target_pending(
            action_id,
            result_patch={"source_file_token": source_file_token},
        )
        ticket = await drive.create_import_task(source_file_token, title, folder_token)
        logger.info("doc.import_task ok ticket=%s file_token=%s action=%s", ticket, source_file_token, action_id)
        queries.record_bot_action_target_pending(
            action_id,
            result_patch={"source_file_token": source_file_token, "import_ticket": ticket},
        )
        imported = await drive.poll_import_task(ticket)
        result = {
            "doc_token": imported["doc_token"],
            "url": imported.get("url"),
            "source_file_token": source_file_token,
            "import_ticket": ticket,
        }
        queries.record_bot_action_target_pending(
            action_id,
            target_id=imported["doc_token"],
            target_kind="docx",
            result_patch=result,
        )
        return result
    except TimeoutError as e:
        queries.mark_bot_action_reconciled_unknown(
            action_id,
            reconciliation_kind="partial_success",
            error=f"import_poll_timeout: {e}",
            keep_lock=True,
        )
        raise
    except Exception:
        if source_file_token:
            try:
                await drive.delete_file(source_file_token, file_type="file")
            except Exception as cleanup_error:
                queries.record_bot_action_target_pending(
                    action_id,
                    target_id=source_file_token,
                    target_kind="file",
                    result_patch={"source_file_token": source_file_token},
                )
                queries.mark_bot_action_reconciled_unknown(
                    action_id,
                    reconciliation_kind="partial_success",
                    error=f"source_cleanup_failed: {cleanup_error}",
                    keep_lock=True,
                )
        raise


async def append_to_doc(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    if not args.get("markdown_body"):
        return err("markdown_body is required")
    try:
        token = await _normalize_doc_token(args.get("doc_link_or_token") or "")
    except Exception as e:
        return err(str(e))
    if not queries.is_doc_authored_by_bot(token):
        return err("我只能改我自己创建的文档。这个文档不是我建的，请让我新建一个相关文档。")
    row, replay = start_action(ctx, "append_to_doc", args)
    if replay:
        return replay
    try:
        parent = token
        blocks = _markdown_to_blocks(
            f"<!-- bot_action_id={row['id']} -->\n"
            + ((f"## {args['heading']}\n\n") if args.get("heading") else "")
            + args.get("markdown_body", "")
        )
        block_ids = await docx.append_blocks(token, parent, blocks, client_token=row["id"])
        result = {
            "appended_block_ids": block_ids,
            "parent_block_id": parent,
            "append_marker_block_id": block_ids[0] if block_ids else None,
        }
        queries.record_bot_action_target_pending(
            row["id"], target_id=token, target_kind="docx_block_append", result_patch=result
        )
        queries.mark_bot_action_success(row["id"], result)
        return ok({"doc_token": token, **result})
    except Exception as e:
        return fail_action(row, e)


def _text_block(content: str, block_type: int = 2):
    tr = TextRun.builder().content(content).build()
    el = TextElement.builder().text_run(tr).build()
    text = Text.builder().elements([el]).build()
    builder = Block.builder().block_type(block_type)
    if block_type == 3:
        return builder.heading1(text).build()
    if block_type == 4:
        return builder.heading2(text).build()
    if block_type == 5:
        return builder.heading3(text).build()
    return builder.text(text).build()


def _markdown_to_blocks(markdown: str) -> list[Any]:
    blocks: list[Any] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("### "):
            blocks.append(_text_block(stripped[4:], 5))
        elif stripped.startswith("## "):
            blocks.append(_text_block(stripped[3:], 4))
        elif stripped.startswith("# "):
            blocks.append(_text_block(stripped[2:], 3))
        else:
            blocks.append(_text_block(stripped, 2))
    return blocks or [_text_block(markdown or "")]
