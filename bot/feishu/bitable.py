from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import lark_oapi as lark
from feishu.client import feishu_client


def _lark_client() -> lark.Client:
    return feishu_client.client


async def search_records(
    *,
    app_token: str,
    table_id: str,
    filter: str | None = None,
    page_size: int = 50,
    page_token: str | None = None,
    sort: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from lark_oapi.api.bitable.v1 import SearchAppTableRecordRequest, SearchAppTableRecordRequestBody

    body = SearchAppTableRecordRequestBody.builder()
    filter_obj = _build_filter(filter)
    if filter_obj:
        body = body.filter(filter_obj)
    if sort:
        body = body.sort(_build_sorts(sort))
    req = (
        SearchAppTableRecordRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .page_size(min(page_size, 200))
        .user_id_type("open_id")
        .request_body(body.build())
    )
    if page_token:
        req = req.page_token(page_token)
    resp = await asyncio.to_thread(_lark_client().bitable.v1.app_table_record.search, req.build())
    if not resp.success():
        raise RuntimeError(f"bitable.search_records failed: {resp.code} {resp.msg}")
    return {
        "records": resp.data.items or [],
        "has_more": bool(resp.data.has_more),
        "next_page_token": getattr(resp.data, "page_token", None),
    }


async def batch_create_records(
    app_token: str, table_id: str, records: list[dict[str, Any]], *, client_token: str
) -> list[str]:
    from lark_oapi.api.bitable.v1 import (
        AppTableRecord,
        BatchCreateAppTableRecordRequest,
        BatchCreateAppTableRecordRequestBody,
    )

    rows = [AppTableRecord.builder().fields(r).build() for r in records]
    body = BatchCreateAppTableRecordRequestBody.builder().records(rows).build()
    req = (
        BatchCreateAppTableRecordRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .client_token(client_token)
        .user_id_type("open_id")
        .request_body(body)
        .build()
    )
    resp = await asyncio.to_thread(_lark_client().bitable.v1.app_table_record.batch_create, req)
    if not resp.success():
        raise RuntimeError(f"bitable.batch_create_records failed: {resp.code} {resp.msg}")
    return [r.record_id for r in (resp.data.records or [])]


async def batch_delete_records(app_token: str, table_id: str, record_ids: list[str]) -> None:
    from lark_oapi.api.bitable.v1 import (
        BatchDeleteAppTableRecordRequest,
        BatchDeleteAppTableRecordRequestBody,
    )

    if not record_ids:
        return
    body = BatchDeleteAppTableRecordRequestBody.builder().records(record_ids).build()
    req = (
        BatchDeleteAppTableRecordRequest.builder()
        .app_token(app_token)
        .table_id(table_id)
        .request_body(body)
        .build()
    )
    resp = await asyncio.to_thread(_lark_client().bitable.v1.app_table_record.batch_delete, req)
    if not resp.success() and "not" not in str(resp.msg).lower():
        raise RuntimeError(f"bitable.batch_delete_records failed: {resp.code} {resp.msg}")


async def create_table(app_token: str, name: str, fields: list[dict[str, Any]]) -> str:
    from lark_oapi.api.bitable.v1 import (
        AppTableCreateHeader,
        CreateAppTableRequest,
        CreateAppTableRequestBody,
        ReqTable,
    )

    headers = [_build_header(f) for f in fields]
    table = ReqTable.builder().name(name).default_view_name("默认视图").fields(headers).build()
    body = CreateAppTableRequestBody.builder().table(table).build()
    req = CreateAppTableRequest.builder().app_token(app_token).request_body(body).build()
    resp = await asyncio.to_thread(_lark_client().bitable.v1.app_table.create, req)
    if not resp.success():
        raise RuntimeError(f"bitable.create_table failed: {resp.code} {resp.msg}")
    return resp.data.table_id


async def delete_table(app_token: str, table_id: str) -> None:
    from lark_oapi.api.bitable.v1 import BatchDeleteAppTableRequest, BatchDeleteAppTableRequestBody

    body = BatchDeleteAppTableRequestBody.builder().table_ids([table_id]).build()
    req = BatchDeleteAppTableRequest.builder().app_token(app_token).request_body(body).build()
    resp = await asyncio.to_thread(_lark_client().bitable.v1.app_table.batch_delete, req)
    if not resp.success() and "not" not in str(resp.msg).lower():
        raise RuntimeError(f"bitable.delete_table failed: {resp.code} {resp.msg}")


async def list_tables(app_token: str) -> list[dict[str, Any]]:
    from lark_oapi.api.bitable.v1 import ListAppTableRequest

    tables: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        req = ListAppTableRequest.builder().app_token(app_token).page_size(100)
        if page_token:
            req = req.page_token(page_token)
        resp = await asyncio.to_thread(_lark_client().bitable.v1.app_table.list, req.build())
        if not resp.success():
            raise RuntimeError(f"bitable.list_tables failed: {resp.code} {resp.msg}")
        tables.extend(
            {"table_id": getattr(t, "table_id", None), "name": getattr(t, "name", None)}
            for t in (resp.data.items or [])
        )
        if not resp.data.has_more:
            return tables
        page_token = resp.data.page_token


async def table_exists(app_token: str, table_id: str) -> bool:
    return any(t["table_id"] == table_id for t in await list_tables(app_token))


async def create_base(name: str, *, folder_token: str | None = None) -> str:
    from lark_oapi.api.bitable.v1 import CreateAppRequest, ReqApp

    body = ReqApp.builder().name(name).time_zone("Asia/Shanghai")
    if folder_token:
        body = body.folder_token(folder_token)
    req = CreateAppRequest.builder().request_body(body.build()).build()
    resp = await asyncio.to_thread(_lark_client().bitable.v1.app.create, req)
    if not resp.success():
        raise RuntimeError(f"bitable.create_base failed: {resp.code} {resp.msg}")
    return resp.data.app.app_token


async def bootstrap_base() -> tuple[str, str, str]:
    app_token = await create_base("包工头的工作台")
    action_items_table_id = await create_table(
        app_token,
        "action_items",
        [
            {"name": "title", "type": "text"},
            {"name": "owner_open_id", "type": "text"},
            {"name": "due_date", "type": "date_time"},
            {"name": "project", "type": "text"},
            {"name": "status", "type": "single_select"},
            {"name": "source_action_id", "type": "text"},
            {"name": "created_by_meeting", "type": "url"},
        ],
    )
    meetings_table_id = await create_table(
        app_token,
        "meetings",
        [
            {"name": "event_id", "type": "text"},
            {"name": "title", "type": "text"},
            {"name": "start_time", "type": "date_time"},
            {"name": "attendees", "type": "text"},
            {"name": "source_action_id", "type": "text"},
        ],
    )
    return app_token, action_items_table_id, meetings_table_id


async def list_fields(app_token: str, table_id: str) -> list[dict[str, Any]]:
    from lark_oapi.api.bitable.v1 import ListAppTableFieldRequest

    req = ListAppTableFieldRequest.builder().app_token(app_token).table_id(table_id).page_size(200).build()
    resp = await asyncio.to_thread(_lark_client().bitable.v1.app_table_field.list, req)
    if not resp.success():
        raise RuntimeError(f"bitable.list_fields failed: {resp.code} {resp.msg}")
    return [
        {
            "field_id": getattr(f, "field_id", None),
            "name": getattr(f, "field_name", None),
            "type": _code_to_field_type(getattr(f, "type", None)),
            "raw_type": getattr(f, "type", None),
            "property": getattr(f, "property", None),
        }
        for f in (resp.data.items or [])
    ]


def _field_type_to_code(name: str) -> int:
    mapping = {
        "text": 1,
        "number": 2,
        "single_select": 3,
        "multi_select": 4,
        "date_time": 5,
        "checkbox": 7,
        "person": 11,
        "phone": 13,
        "url": 15,
        "email": 1,
    }
    if name not in mapping:
        raise ValueError(f"unsupported Bitable field type: {name}")
    return mapping[name]


def _build_header(field: dict[str, Any]):
    from lark_oapi.api.bitable.v1 import (
        AppTableCreateHeader,
        AppTableFieldProperty,
        AppTableFieldPropertyOption,
    )

    kind = field.get("type")
    code = int(kind) if isinstance(kind, int) else _field_type_to_code(str(kind))
    header = AppTableCreateHeader.builder().field_name(field.get("field_name") or field["name"]).type(code)
    options = (field.get("options") or {}).get("choices") or field.get("choices")
    if options:
        prop_options = [
            AppTableFieldPropertyOption.builder().name(str(choice)).color((idx % 10) + 1).build()
            for idx, choice in enumerate(options)
        ]
        header = header.property(AppTableFieldProperty.builder().options(prop_options).build())
    return header.build()


def _code_to_field_type(code: int | None) -> str:
    reverse = {
        1: "text",
        2: "number",
        3: "single_select",
        4: "multi_select",
        5: "date_time",
        7: "checkbox",
        11: "person",
        13: "phone",
        15: "url",
    }
    return reverse.get(code or 0, f"unknown:{code}")


def _build_filter(filter_value: Any):
    if not filter_value:
        return None
    if isinstance(filter_value, str):
        filter_value = _parse_filter_string(filter_value)
    if not isinstance(filter_value, dict):
        return None
    from lark_oapi.api.bitable.v1 import Condition, FilterInfo

    conditions = [
        Condition.builder()
        .field_name(item["field_name"])
        .operator(item.get("operator") or "is")
        .value([str(v) for v in (item.get("value") if isinstance(item.get("value"), list) else [item.get("value")])])
        .build()
        for item in (filter_value.get("conditions") or [])
    ]
    return FilterInfo.builder().conjunction(filter_value.get("conjunction") or "and").conditions(conditions).build()


def _parse_filter_string(value: str) -> dict[str, Any] | None:
    text = value.strip()
    if not text:
        return None
    if text.startswith("{"):
        return json.loads(text)
    conditions = []
    for part in re.split(r"\s+AND\s+", text, flags=re.IGNORECASE):
        part = part.strip()
        if not part:
            continue
        if m := re.match(r"^(.+?)\s+contains\s+(.+)$", part, flags=re.IGNORECASE):
            conditions.append({
                "field_name": m.group(1).strip(),
                "operator": "contains",
                "value": _strip_quotes(m.group(2).strip()),
            })
        elif m := re.match(r"^(.+?)\s*=\s*(.+)$", part):
            conditions.append({
                "field_name": m.group(1).strip(),
                "operator": "is",
                "value": _strip_quotes(m.group(2).strip()),
            })
        else:
            raise ValueError("unsupported Bitable filter expression; use field=value or field contains value")
    return {"conjunction": "and", "conditions": conditions} if conditions else None


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _build_sorts(sort: list[dict[str, Any]]):
    from lark_oapi.api.bitable.v1 import Sort

    return [
        Sort.builder().field_name(item["field"]).desc(bool(item.get("desc"))).build()
        for item in sort
    ]
