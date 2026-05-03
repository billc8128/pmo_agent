from __future__ import annotations

import asyncio
from typing import Any

import lark_oapi as lark
from lark_oapi.api.docx.v1 import (
    BatchDeleteDocumentBlockChildrenRequest,
    BatchDeleteDocumentBlockChildrenRequestBody,
    CreateDocumentBlockChildrenRequest,
    CreateDocumentBlockChildrenRequestBody,
    GetDocumentBlockChildrenRequest,
    ListDocumentBlockRequest,
)

from feishu.client import feishu_client


def _lark_client() -> lark.Client:
    return feishu_client.client


async def list_blocks(document_id: str) -> list[Any]:
    blocks: list[Any] = []
    page_token: str | None = None
    while True:
        req = ListDocumentBlockRequest.builder().document_id(document_id).page_size(500)
        if page_token:
            req = req.page_token(page_token)
        resp = await asyncio.to_thread(_lark_client().docx.v1.document_block.list, req.build())
        if not resp.success():
            raise RuntimeError(f"docx.list_blocks failed: {resp.code} {resp.msg}")
        blocks.extend(resp.data.items or [])
        if not resp.data.has_more:
            return blocks
        page_token = resp.data.page_token


async def list_child_blocks(document_id: str, parent_block_id: str) -> list[Any]:
    children: list[Any] = []
    page_token: str | None = None
    while True:
        req = (
            GetDocumentBlockChildrenRequest.builder()
            .document_id(document_id)
            .block_id(parent_block_id)
            .page_size(500)
        )
        if page_token:
            req = req.page_token(page_token)
        resp = await asyncio.to_thread(
            _lark_client().docx.v1.document_block_children.get, req.build()
        )
        if not resp.success():
            raise RuntimeError(f"docx.list_child_blocks failed: {resp.code} {resp.msg}")
        children.extend(resp.data.items or [])
        if not resp.data.has_more:
            return children
        page_token = resp.data.page_token


async def append_blocks(
    document_id: str,
    parent_block_id: str,
    children: list[Any],
    *,
    index: int = -1,
    client_token: str | None = None,
) -> list[str]:
    body = CreateDocumentBlockChildrenRequestBody.builder().children(children).index(index).build()
    req = (
        CreateDocumentBlockChildrenRequest.builder()
        .document_id(document_id)
        .block_id(parent_block_id)
        .request_body(body)
    )
    if client_token:
        req = req.client_token(client_token)
    resp = await asyncio.to_thread(_lark_client().docx.v1.document_block_children.create, req.build())
    if not resp.success():
        raise RuntimeError(f"docx.append_blocks failed: {resp.code} {resp.msg}")
    return [c.block_id for c in (resp.data.children or [])]


def _contiguous_ranges(indexes: list[int]) -> list[tuple[int, int]]:
    if not indexes:
        return []
    ranges: list[tuple[int, int]] = []
    start = prev = indexes[0]
    for idx in indexes[1:]:
        if idx == prev + 1:
            prev = idx
            continue
        ranges.append((start, prev))
        start = prev = idx
    ranges.append((start, prev))
    return ranges


async def delete_blocks(
    document_id: str, parent_block_id: str, block_ids: list[str]
) -> dict[str, int]:
    current = await list_child_blocks(document_id, parent_block_id)
    index_by_id = {c.block_id: i for i, c in enumerate(current)}
    indexes = sorted(index_by_id[b] for b in block_ids if b in index_by_id)
    missing = len(block_ids) - len(indexes)
    if not indexes:
        return {"deleted": 0, "missing": missing}

    for start, end in reversed(_contiguous_ranges(indexes)):
        body = (
            BatchDeleteDocumentBlockChildrenRequestBody.builder()
            .start_index(start)
            .end_index(end + 1)
            .build()
        )
        req = (
            BatchDeleteDocumentBlockChildrenRequest.builder()
            .document_id(document_id)
            .block_id(parent_block_id)
            .client_token(f"delete:{parent_block_id}:{start}:{end}")
            .request_body(body)
            .build()
        )
        resp = await asyncio.to_thread(
            _lark_client().docx.v1.document_block_children.batch_delete, req
        )
        if not resp.success():
            raise RuntimeError(f"docx.delete_blocks failed: code={resp.code} msg={resp.msg}")
    return {"deleted": len(indexes), "missing": missing}
