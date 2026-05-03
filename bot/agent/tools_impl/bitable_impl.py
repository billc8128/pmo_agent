from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from agent.request_context import RequestContext
from agent.tool_utils import err, ok
from agent.tools_impl.common import fail_action, start_action, workspace_or_error
from db import queries
from feishu import bitable


async def append_action_items(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    items = args.get("items") or []
    if not items:
        return err("items is required")
    default_project = args.get("project")
    if not default_project and any(not item.get("project") for item in items):
        default_project = _default_project_for_asker(ctx)
    missing_project = [item for item in items if not item.get("project") and not default_project]
    if missing_project:
        return ok({
            "needs_input": "project",
            "items_pending": missing_project,
            "auto_suggestion": None,
            "auto_suggestion_confidence": "low",
            "agent_directive": "Ask the user which project these action items belong to before calling append_action_items again.",
        })
    ws, ws_err = workspace_or_error()
    if ws_err:
        return ws_err
    row, replay = start_action(ctx, "append_action_items", args)
    if replay:
        return replay
    try:
        meeting_link = ""
        if args.get("meeting_event_id"):
            meeting_row = queries.get_bot_action_by_target(
                chat_id=ctx.chat_id,
                target_id=args["meeting_event_id"],
                target_kind="calendar_event",
                action_type_in=["schedule_meeting", "restore_schedule_meeting"],
                status_in=["success", "reconciled_unknown"],
            )
            meeting_link = ((meeting_row or {}).get("result") or {}).get("link") or ""
        records = [
            {
                **item,
                "project": item.get("project") or default_project,
                "status": item.get("status") or "todo",
                "source_action_id": row["id"],
                **({"created_by_meeting": meeting_link} if meeting_link else {}),
            }
            for item in items
        ]
        record_ids = await bitable.batch_create_records(
            ws["base_app_token"], ws["action_items_table_id"], records, client_token=row["id"]
        )
        queries.record_bot_action_target_pending(
            row["id"],
            target_id=ws["action_items_table_id"],
            target_kind="bitable_records",
            result_patch={"record_ids": record_ids, "records": records},
        )
        queries.mark_bot_action_success(row["id"], {"record_ids": record_ids, "records": records})
        return ok({
            "record_ids": record_ids,
            "count": len(record_ids),
            "records": [
                {
                    "record_id": rid,
                    "title": records[idx].get("title"),
                    "project_used": records[idx].get("project"),
                    "project_source": "user_explicit" if (items[idx].get("project") or args.get("project")) else "auto_recent_turns",
                }
                for idx, rid in enumerate(record_ids)
            ],
        })
    except Exception as e:
        return fail_action(row, e)


async def query_action_items(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    ws, ws_err = workspace_or_error()
    if ws_err:
        return ws_err
    try:
        filter_value = args.get("filter") or _action_items_filter(args)
        return ok(await bitable.search_records(
            app_token=ws["base_app_token"],
            table_id=ws["action_items_table_id"],
            filter=filter_value,
            page_size=min(int(args.get("page_size") or 50), 200),
        ))
    except Exception as e:
        return err(str(e))


async def create_bitable_table(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    ws, ws_err = workspace_or_error()
    if ws_err:
        return ws_err
    if not args.get("name") or len(args["name"]) > 100:
        return err("name is required and must be at most 100 chars")
    if not args.get("fields"):
        return err("fields is required")
    try:
        for f in args["fields"]:
            bitable._field_type_to_code(str(f.get("type"))) if not isinstance(f.get("type"), int) else int(f["type"])
    except Exception as e:
        return err(str(e))
    row, replay = start_action(ctx, "create_bitable_table", args)
    if replay:
        return replay
    try:
        table_id = await bitable.create_table(ws["base_app_token"], args["name"], args.get("fields") or [])
        queries.record_bot_action_target_pending(
            row["id"], target_id=table_id, target_kind="bitable_table", result_patch={"table_id": table_id}
        )
        queries.mark_bot_action_success(row["id"], {"table_id": table_id})
        return ok({"table_id": table_id})
    except Exception as e:
        return fail_action(row, e)


async def append_to_my_table(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    ws, ws_err = workspace_or_error()
    if ws_err:
        return ws_err
    table_id = args.get("table_id")
    if table_id in {ws["action_items_table_id"], ws["meetings_table_id"]}:
        return err("请使用 append_action_items，而不是 append_to_my_table 写 action_items/meetings 系统表")
    if not table_id or not args.get("records"):
        return err("table_id and records are required")
    if not _is_bot_owned_custom_table(table_id):
        return err("这张表不在我的工作台里，不能写入")
    fields = await bitable.list_fields(ws["base_app_token"], table_id)
    known_fields = {f["name"] for f in fields}
    unknown = sorted({k for r in (args.get("records") or []) for k in r.keys()} - known_fields)
    if unknown:
        return err(f"字段不存在: {', '.join(unknown)}。请先调用 describe_my_table 查看字段。")
    row, replay = start_action(ctx, "append_to_my_table", args)
    if replay:
        return replay
    try:
        records = [{**r, "source_action_id": row["id"]} for r in (args.get("records") or [])]
        record_ids = await bitable.batch_create_records(ws["base_app_token"], table_id, records, client_token=row["id"])
        queries.record_bot_action_target_pending(row["id"], target_id=table_id, target_kind="bitable_records", result_patch={"record_ids": record_ids})
        queries.mark_bot_action_success(row["id"], {"record_ids": record_ids})
        return ok({"record_ids": record_ids, "count": len(record_ids)})
    except Exception as e:
        return fail_action(row, e)


async def query_my_table(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    ws, ws_err = workspace_or_error()
    if ws_err:
        return ws_err
    if not _is_bot_owned_custom_table(args["table_id"]):
        return err("这张表不在我的工作台里，不能读取")
    try:
        return ok(await bitable.search_records(
            app_token=ws["base_app_token"],
            table_id=args["table_id"],
            filter=args.get("filter") or None,
            page_size=min(int(args.get("page_size") or 50), 200),
            page_token=args.get("page_token") or None,
        ))
    except Exception as e:
        return err(str(e))


async def describe_my_table(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    ws, ws_err = workspace_or_error()
    if ws_err:
        return ws_err
    if not _is_bot_owned_custom_table(args["table_id"]):
        return err("这张表不在我的工作台里，不能读取")
    fields = await bitable.list_fields(ws["base_app_token"], args["table_id"])
    return ok({"table_id": args["table_id"], "fields": fields})


def _action_items_filter(args: dict[str, Any]) -> dict[str, Any] | None:
    conditions: list[dict[str, Any]] = []
    if args.get("owner_open_id"):
        conditions.append({"field_name": "owner_open_id", "operator": "is", "value": args["owner_open_id"]})
    if args.get("project"):
        conditions.append({"field_name": "project", "operator": "is", "value": args["project"]})
    if args.get("status"):
        conditions.append({"field_name": "status", "operator": "is", "value": args["status"]})
    if not conditions:
        return None
    return {"conjunction": "and", "conditions": conditions}


def _is_bot_owned_custom_table(table_id: str) -> bool:
    return bool(queries.get_bot_action_by_target(
        target_id=table_id,
        target_kind="bitable_table",
        action_type_in=["create_bitable_table"],
        status_in=["success", "reconciled_unknown"],
    ))


def _default_project_for_asker(ctx: RequestContext) -> str | None:
    profile = queries.lookup_by_feishu_open_id(ctx.sender_open_id)
    user_id = (profile or {}).get("user_id")
    if not user_id:
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = queries.recent_turns(user_id, since_iso=since, limit=1000)
    stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        project = queries.project_root_for_row(row)
        item = stats.setdefault(project, {"count": 0, "latest": ""})
        item["count"] += 1
        item["latest"] = max(item["latest"], row.get("user_message_at") or "")
    if not stats:
        return None
    ranked = sorted(stats.items(), key=lambda kv: (kv[1]["count"], kv[1]["latest"]), reverse=True)
    project, data = ranked[0]
    if data["count"] < 3:
        return None
    return project
