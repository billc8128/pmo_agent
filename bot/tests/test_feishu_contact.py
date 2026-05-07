from __future__ import annotations

import asyncio

import httpx

from feishu import contact


def test_search_users_uses_get_with_query_params(monkeypatch):
    calls = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers=None, params=None):
            calls.append(("GET", url, headers, params))
            return httpx.Response(
                200,
                request=httpx.Request("GET", url, params=params),
                json={
                    "code": 0,
                    "data": {
                        "users": [
                            {"open_id": "ou_1", "name": "张伟", "email": "zhang@example.com"},
                        ]
                    },
                },
            )

        async def post(self, *args, **kwargs):
            raise AssertionError("search_users must use GET")

    monkeypatch.setattr("feishu.contact._tenant_access_token", lambda: asyncio.sleep(0, result="tenant-token"))
    monkeypatch.setattr("feishu.contact.httpx.AsyncClient", FakeAsyncClient)

    users = asyncio.run(contact.search_users("张伟"))

    assert users[0]["open_id"] == "ou_1"
    assert calls == [
        (
            "GET",
            "https://open.feishu.cn/open-apis/search/v1/user",
            {"Authorization": "Bearer tenant-token"},
            {"query": "张伟", "page_size": 20},
        )
    ]


def test_list_chat_member_open_ids_paginates(monkeypatch):
    calls = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers=None, params=None):
            calls.append((url, headers, params))
            page_token = (params or {}).get("page_token")
            if not page_token:
                return httpx.Response(
                    200,
                    request=httpx.Request("GET", url),
                    json={"code": 0, "data": {"items": [{"member_id": "ou_1"}], "has_more": True, "page_token": "p2"}},
                )
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={"code": 0, "data": {"items": [{"member_id": "ou_2"}, {"member_id": "ou_1"}], "has_more": False}},
            )

    monkeypatch.setattr("feishu.contact._tenant_access_token", lambda: asyncio.sleep(0, result="tenant-token"))
    monkeypatch.setattr("feishu.contact.httpx.AsyncClient", FakeAsyncClient)

    members = asyncio.run(contact.list_chat_member_open_ids("oc_1"))

    assert members == ["ou_1", "ou_2"]
    assert calls[0][2] == {"member_id_type": "open_id", "page_size": 100}
    assert calls[1][2] == {"member_id_type": "open_id", "page_size": 100, "page_token": "p2"}
