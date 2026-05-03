from __future__ import annotations

import asyncio

import lark_oapi as lark
from lark_oapi.api.wiki.v2 import GetNodeSpaceRequest

from feishu.client import feishu_client


def _lark_client() -> lark.Client:
    return feishu_client.client


async def resolve_node(token: str) -> dict[str, str]:
    req = GetNodeSpaceRequest.builder().token(token).build()
    resp = await asyncio.to_thread(_lark_client().wiki.v2.space.get_node, req)
    if not resp.success():
        raise RuntimeError(f"wiki.resolve_node failed: {resp.code} {resp.msg}")
    node = resp.data.node
    return {"obj_token": node.obj_token, "obj_type": node.obj_type}
