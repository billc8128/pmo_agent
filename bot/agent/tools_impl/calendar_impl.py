from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent.request_context import RequestContext
from agent.tool_utils import err, ok
from agent.tools_impl.common import fail_action, parse_rfc3339, start_action, workspace_or_error
from db import queries
from feishu import calendar


def _zoneinfo_or_default(timezone_name: str | None) -> tuple[str, ZoneInfo]:
    name = timezone_name or "Asia/Shanghai"
    try:
        return name, ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return "Asia/Shanghai", ZoneInfo("Asia/Shanghai")


async def _timezone_for_user(ctx: RequestContext) -> str:
    if not ctx.sender_open_id:
        return "Asia/Shanghai"
    try:
        from feishu import contact

        user = await contact.get_user(ctx.sender_open_id)
        return user.get("time_zone") or "Asia/Shanghai"
    except Exception:
        return "Asia/Shanghai"


def _is_date_only(value: str | None) -> bool:
    return bool(value) and len(value or "") == 10 and "T" not in (value or "")


def _normalize_meeting_window(since: str | None, until: str | None, timezone_name: str) -> tuple[str | None, str | None]:
    if not since and not until:
        return None, None
    timezone_name, user_zone = _zoneinfo_or_default(timezone_name)
    if _is_date_only(since):
        start_date = datetime.fromisoformat(since).date()
        start = datetime.combine(start_date, datetime.min.time(), tzinfo=user_zone)
    elif since:
        start = datetime.fromisoformat(since.replace("Z", "+00:00"))
        if not start.tzinfo:
            start = start.replace(tzinfo=user_zone)
    elif _is_date_only(until):
        end_date = datetime.fromisoformat(until).date()
        start = datetime.combine(end_date, datetime.min.time(), tzinfo=user_zone)
    else:
        start = None

    if _is_date_only(until):
        end_date = datetime.fromisoformat(until).date()
        if _is_date_only(since) and start is not None:
            if end_date <= start.date():
                end_date = start.date() + timedelta(days=1)
            end = datetime.combine(end_date, datetime.min.time(), tzinfo=user_zone)
        else:
            end = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=user_zone)
    elif until:
        end = datetime.fromisoformat(until.replace("Z", "+00:00"))
        if not end.tzinfo:
            end = end.replace(tzinfo=user_zone)
    elif start is not None:
        end = start + timedelta(days=1)
    else:
        end = None

    if start is not None and end is not None and end <= start:
        end = start + timedelta(days=1)
    return start.isoformat() if start else None, end.isoformat() if end else None


def _parse_window_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _filter_events_by_window(events: list[dict[str, Any]], since: str | None, until: str | None) -> list[dict[str, Any]]:
    start = _parse_window_datetime(since)
    end = _parse_window_datetime(until)
    if not start or not end:
        return events
    filtered: list[dict[str, Any]] = []
    for event in events:
        try:
            event_start = _parse_window_datetime(event.get("start_time"))
            event_end = _parse_window_datetime(event.get("end_time")) or event_start
        except (TypeError, ValueError):
            continue
        if event_start and event_start < end and event_end and event_end > start:
            filtered.append(event)
    return filtered


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
    requested_since = args.get("since") or args.get("time_min")
    requested_until = args.get("until") or args.get("time_max")
    user_timezone = await _timezone_for_user(ctx)
    since, until = _normalize_meeting_window(requested_since, requested_until, user_timezone)
    bot_known_all = queries.bot_known_events_for_attendee(ctx.chat_id, target) if target else []
    bot_known = _filter_events_by_window(bot_known_all, since, until)
    user_events: list[dict[str, Any]] = []
    user_busy_slots: list[dict[str, Any]] = []
    user_calendar_error: str | None = None
    user_calendar_warning: str | None = None
    user_freebusy_error: str | None = None
    if target and since and until:
        try:
            user_busy_slots = await calendar.batch_freebusy([target], since, until)
        except Exception as e:
            user_freebusy_error = f"{type(e).__name__}: {e}"
        try:
            primary = await calendar.primary_calendar_id(target)
            if primary:
                user_events = await calendar.list_events(primary, since, until)
                user_events = _filter_events_by_window(user_events, since, until)
            else:
                user_calendar_warning = "primary_calendar_not_visible_to_bot"
        except Exception as e:
            user_calendar_error = f"{type(e).__name__}: {e}"
            user_events = []
    return ok({
        "target_open_id": target,
        "requested_since": requested_since,
        "requested_until": requested_until,
        "normalized_since": since,
        "normalized_until": until,
        "user_timezone": user_timezone,
        "bot_known_events": bot_known,
        "user_calendar_events": user_events,
        "user_busy_slots": user_busy_slots,
        "user_calendar_error": user_calendar_error,
        "user_calendar_warning": user_calendar_warning,
        "user_freebusy_error": user_freebusy_error,
        "visibility_note": "只返回包工头可见的日程；用户主日历可能受飞书权限限制。",
    })
