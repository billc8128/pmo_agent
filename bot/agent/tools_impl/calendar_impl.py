from __future__ import annotations

from datetime import timedelta
from typing import Any

from agent.request_context import RequestContext
from agent.tool_utils import err, ok
from agent.tools_impl.common import fail_action, parse_rfc3339, start_action, workspace_or_error
from db import queries
from feishu import calendar


async def schedule_meeting(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    if not args.get("title") or not args.get("start_time"):
        return err("title and start_time are required")
    try:
        start = parse_rfc3339(args["start_time"])
    except ValueError as e:
        return err(str(e))
    duration = int(args.get("duration_minutes") or 30)
    end = start + timedelta(minutes=duration)
    row, replay = start_action(ctx, "schedule_meeting", args)
    if replay:
        return replay
    try:
        ws, ws_err = workspace_or_error()
        if ws_err:
            queries.mark_bot_action_failed(row["id"], "workspace not bootstrapped")
            return ws_err
        attendees = list(dict.fromkeys(args.get("attendee_open_ids") or []))
        if args.get("include_asker", True) and ctx.sender_open_id:
            attendees = list(dict.fromkeys([*attendees, ctx.sender_open_id]))
        conflicts = await calendar.batch_freebusy(attendees, start.isoformat(), end.isoformat())
        if conflicts:
            result = {
                "outcome": "conflict",
                "conflicts": conflicts,
                "attendees": attendees,
                "start_time": start.isoformat(),
                "end_time": end.isoformat(),
            }
            queries.mark_bot_action_success(row["id"], result)
            return ok(result)
        created = await calendar.create_event(
            calendar_id=ws["calendar_id"],
            title=args["title"],
            start_time=start.isoformat(),
            end_time=end.isoformat(),
            description=args.get("description") or "",
            idempotency_key=f"schedule_meeting:{row['id']}",
        )
        queries.record_bot_action_target_pending(
            row["id"],
            target_id=created["event_id"],
            target_kind="calendar_event",
            result_patch={**created, "title": args["title"], "attendees": attendees},
        )
        try:
            await calendar.invite_attendees(ws["calendar_id"], created["event_id"], attendees)
        except Exception as invite_error:
            queries.mark_bot_action_reconciled_unknown(
                row["id"],
                reconciliation_kind="partial_success",
                error=f"attendee_invite_failed: {type(invite_error).__name__}: {invite_error}",
                keep_lock=True,
            )
            return err(
                "会议已创建，但邀请参会人失败；请检查日历，或让我撤销这个会议",
                event_id=created["event_id"],
                calendar_id=ws["calendar_id"],
            )
        result = {**created, "title": args["title"], "attendees": attendees}
        queries.mark_bot_action_success(row["id"], result)
        return ok(result)
    except Exception as e:
        return fail_action(row, e)


async def cancel_meeting(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    event_id = args.get("event_id")
    if not event_id and not args.get("last"):
        return err("event_id is required, or pass last=true")
    row, replay = start_action(ctx, "cancel_meeting", args)
    if replay:
        return replay
    if event_id:
        source = queries.get_bot_action_by_target(
            chat_id=ctx.chat_id,
            sender_open_id=ctx.sender_open_id,
            target_id=event_id,
            target_kind="calendar_event",
            action_type_in=["schedule_meeting", "restore_schedule_meeting"],
            status_in=["success", "reconciled_unknown"],
        )
    else:
        source = queries.last_meeting_action_for_sender_in_chat(ctx.chat_id, ctx.sender_open_id)
        event_id = source.get("target_id") if source else None
    if not source:
        queries.mark_bot_action_failed(row["id"], "no_source_meeting_for_cancel")
        return err("只能取消我在这个会话里为你创建的会议")
    snapshot_persisted = False
    try:
        calendar_id = (source.get("result") or {}).get("calendar_id")
        snapshot = await calendar.get_event(calendar_id, event_id)
        snapshot.setdefault("calendar_id", calendar_id)
        queries.record_bot_action_target_pending(
            row["id"],
            target_id=event_id,
            target_kind="calendar_event_cancel",
            result_patch={
                "pre_cancel_event_snapshot": snapshot,
                "calendar_id": calendar_id,
                "source_meeting_action_id": source["id"],
            },
        )
        snapshot_persisted = True
        await calendar.delete_event(calendar_id, event_id)
        queries.retire_source_action(source["id"])
        result = {
            "cancelled": True,
            "event_id": event_id,
            "calendar_id": calendar_id,
            "pre_cancel_event_snapshot": snapshot,
            "source_meeting_action_id": source["id"],
        }
        queries.mark_bot_action_success(row["id"], result)
        return ok({"cancelled": True, "event_id": event_id})
    except Exception as e:
        if snapshot_persisted:
            queries.mark_bot_action_reconciled_unknown(
                row["id"],
                reconciliation_kind="partial_success",
                error=f"cancel_delete_failed_after_snapshot: {type(e).__name__}: {e}",
                keep_lock=True,
            )
            return err(
                "已保存会议快照，但删除会议失败；我没有覆盖快照。请检查日历后决定是否重试或撤销。",
                event_id=event_id,
            )
        return fail_action(row, e)


async def list_my_meetings(ctx: RequestContext, args: dict[str, Any]) -> dict[str, Any]:
    target = args.get("target_open_id") or (None if args.get("target") in {None, "", "self"} else args.get("target")) or ctx.sender_open_id
    since = args.get("since") or args.get("time_min")
    until = args.get("until") or args.get("time_max")
    bot_known = queries.bot_known_events_for_attendee(ctx.chat_id, target) if target else []
    user_events: list[dict[str, Any]] = []
    if target and since and until:
        try:
            primary = await calendar.primary_calendar_id(target)
            if primary:
                user_events = await calendar.list_events(primary, since, until)
        except Exception:
            user_events = []
    return ok({
        "target_open_id": target,
        "since": since,
        "until": until,
        "bot_known_events": bot_known,
        "user_calendar_events": user_events,
        "visibility_note": "只返回包工头可见的日程；用户主日历可能受飞书权限限制。",
    })
