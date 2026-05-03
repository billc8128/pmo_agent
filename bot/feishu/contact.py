from __future__ import annotations

import asyncio
from typing import Any

import httpx
import lark_oapi as lark

from config import settings
from feishu.client import feishu_client


def _lark_client() -> lark.Client:
    return feishu_client.client


async def get_user(open_id: str) -> dict:
    from lark_oapi.api.contact.v3 import GetUserRequest

    req = GetUserRequest.builder().user_id(open_id).user_id_type("open_id").build()
    resp = await asyncio.to_thread(_lark_client().contact.v3.user.get, req)
    if not resp.success():
        raise RuntimeError(f"contact.get_user failed: {resp.code} {resp.msg}")
    return _user_to_dict(resp.data.user)


async def batch_get_id_by_email_or_phone(emails: list[str] | None = None, phones: list[str] | None = None) -> dict:
    from lark_oapi.api.contact.v3 import BatchGetIdUserRequest, BatchGetIdUserRequestBody

    body = BatchGetIdUserRequestBody.builder().emails(emails or []).mobiles(phones or []).include_resigned(False).build()
    req = BatchGetIdUserRequest.builder().user_id_type("open_id").request_body(body).build()
    resp = await asyncio.to_thread(_lark_client().contact.v3.user.batch_get_id, req)
    if not resp.success():
        raise RuntimeError(f"contact.batch_get_id failed: {resp.code} {resp.msg}")
    found: list[dict[str, Any]] = []
    for item in resp.data.user_list or []:
        found.append({
            "email": getattr(item, "email", None),
            "mobile": getattr(item, "mobile", None),
            "open_id": getattr(item, "open_id", None),
            "user_id": getattr(item, "user_id", None),
        })
    return {"users": found}


async def search_users(query: str) -> list[dict]:
    needle = query.strip().lower()
    if not needle:
        return []
    token = await _tenant_access_token()
    async with httpx.AsyncClient(timeout=10.0) as ac:
        params = {"query": query, "page_size": 20}
        resp = await ac.get(
            "https://open.feishu.cn/open-apis/search/v1/user",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        if resp.status_code == 429 or resp.status_code >= 500:
            await asyncio.sleep(0.5)
            resp = await ac.get(
                "https://open.feishu.cn/open-apis/search/v1/user",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
        resp.raise_for_status()
        data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"contact.search_users failed: {data.get('code')} {data.get('msg')}")
    users = (data.get("data") or {}).get("users") or (data.get("data") or {}).get("items") or []
    return [_user_obj_to_dict(u) for u in users][:10]


def _user_to_dict(user: Any) -> dict[str, Any]:
    return {
        "open_id": getattr(user, "open_id", None),
        "user_id": getattr(user, "user_id", None),
        "name": getattr(user, "name", None),
        "en_name": getattr(user, "en_name", None),
        "email": getattr(user, "email", None),
        "mobile": getattr(user, "mobile", None),
        "time_zone": getattr(user, "time_zone", None),
    }


def _user_obj_to_dict(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "open_id": user.get("open_id"),
        "user_id": user.get("user_id"),
        "name": user.get("name"),
        "en_name": user.get("en_name"),
        "email": user.get("email"),
        "mobile": user.get("mobile"),
        "department_ids": user.get("department_ids"),
    }


async def _tenant_access_token() -> str:
    async with httpx.AsyncClient(timeout=10.0) as ac:
        resp = await ac.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": settings.feishu_app_id, "app_secret": settings.feishu_app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"feishu auth failed: {data}")
    return data["tenant_access_token"]
