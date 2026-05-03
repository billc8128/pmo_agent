from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from agent.request_context import RequestContext
from agent.tool_utils import err, logical_key, ok
from db import queries


def _success_replay(row: dict[str, Any], *, logical_key_replay: bool = False) -> dict[str, Any]:
    payload = dict(row.get("result") or {})
    payload["cached_result"] = True
    is_meeting_conflict = (
        row.get("action_type") in {"schedule_meeting", "restore_schedule_meeting"}
        and payload.get("outcome") == "conflict"
    )
    if is_meeting_conflict:
        payload["meeting_created"] = False
        payload["agent_directive"] = (
            "This cached result is a freebusy conflict, not a created meeting. "
            "Tell the user no meeting was created and ask for a different time or attendees."
        )
    if logical_key_replay:
        payload["deduplicated_from_logical_key"] = True
    return ok(payload)


def _reconciled_unknown_replay(row: dict[str, Any], *, logical_key_replay: bool = False) -> dict[str, Any]:
    result = row.get("result") or {}
    payload = {
        **result,
        "cached_result": True,
        "reconciled_unknown": True,
        "reconciliation_kind": result.get("reconciliation_kind") or row.get("reconciliation_kind"),
        "source_action_id": row.get("id"),
        "suggest_undo": True,
        "agent_directive": (
            "Do not present this as a normal success. Tell the user the prior write may be partial, "
            "summarize the persisted handles, and offer undo or manual inspection."
        ),
    }
    if logical_key_replay:
        payload["deduplicated_from_logical_key"] = True
    return ok(payload)


def parse_rfc3339(value: str) -> datetime:
    if not value or "T" not in value:
        raise ValueError("start_time must be RFC3339 with timezone")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def start_action(ctx: RequestContext, action_type: str, args: dict[str, Any]):
    message_id = ctx.message_id or f"manual:{action_type}"
    lk = logical_key(
        chat_id=ctx.chat_id,
        sender_open_id=ctx.sender_open_id,
        action_type=action_type,
        args=args,
    )
    existing_for_message = queries.get_bot_action(message_id, action_type)
    if existing_for_message:
        status = existing_for_message.get("status")
        if status == "success":
            return None, _success_replay(existing_for_message)
        if status == "reconciled_unknown":
            return None, _reconciled_unknown_replay(existing_for_message)
        if status == "failed":
            return None, err("这个请求上次执行失败了。为了避免静默重复写入，请重新发一条消息再试。")
        if status == "undone":
            return None, err("这个请求已经被撤销；如果要重新执行，请重新发一条消息")
        return None, err("这个请求已经在处理，等它完成后我会回复")

    existing = queries.get_locked_by_logical_key(lk)
    if existing:
        if existing.get("status") == "success":
            return None, _success_replay(existing, logical_key_replay=True)
        if existing.get("status") == "reconciled_unknown":
            return None, _reconciled_unknown_replay(existing, logical_key_replay=True)
        return None, err("同一个动作还在进行中，先等它完成")
    try:
        row = queries.insert_bot_action_pending(
            message_id=message_id,
            chat_id=ctx.chat_id,
            sender_open_id=ctx.sender_open_id,
            action_type=action_type,
            args=args,
            logical_key=lk,
        )
        return row, None
    except queries.MessageActionConflict as exc:
        row = exc.existing_row
        if row and row.get("status") == "success":
            return None, _success_replay(row)
        if row and row.get("status") == "reconciled_unknown":
            return None, _reconciled_unknown_replay(row)
        if row and row.get("status") == "failed":
            return None, err("这个请求上次执行失败了。为了避免静默重复写入，请重新发一条消息再试。")
        return None, err("这个请求已经在处理或处于不可重试状态")
    except queries.LogicalKeyConflict as exc:
        row = exc.existing_row
        if row and row.get("status") == "success":
            return None, _success_replay(row, logical_key_replay=True)
        if row and row.get("status") == "reconciled_unknown":
            return None, _reconciled_unknown_replay(row, logical_key_replay=True)
        return None, err("同一个动作还在进行中，先等它完成")


def workspace_or_error():
    ws = queries.get_bot_workspace()
    if not ws:
        return None, err("bot_workspace 尚未初始化，请先运行 bootstrap_bot_workspace")
    return ws, None


def fail_action(row: dict[str, Any] | None, exc: Exception):
    if row:
        queries.mark_bot_action_failed(row["id"], f"{type(exc).__name__}: {exc}")
    return err(f"{type(exc).__name__}: {exc}")
