from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import lark_oapi as lark
from feishu.client import feishu_client


def _lark_client() -> lark.Client:
    return feishu_client.client


def _is_not_found(resp: Any) -> bool:
    return getattr(resp, "code", None) in {404, 190004} or "not" in str(getattr(resp, "msg", "")).lower()


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _timestamp(value: str) -> str:
    return str(int(_parse_datetime(value).timestamp()))


def _timezone_name(value: str) -> str:
    dt = _parse_datetime(value)
    if not dt.tzinfo:
        return "UTC"
    offset = dt.utcoffset()
    if offset and offset.total_seconds() == 8 * 3600:
        return "Asia/Shanghai"
    if offset and offset.total_seconds() == 0:
        return "UTC"
    return dt.tzinfo.tzname(dt) or "UTC"


def _event_time(value: str):
    from lark_oapi.api.calendar.v4 import TimeInfo

    return TimeInfo.builder().timestamp(_timestamp(value)).timezone(_timezone_name(value)).build()


def _event_to_dict(calendar_id: str, ev: Any) -> dict[str, Any]:
    attendees = [
        getattr(a, "user_id", None)
        for a in (getattr(ev, "attendees", None) or [])
        if getattr(a, "user_id", None)
    ]
    return {
        "event_id": getattr(ev, "event_id", None),
        "calendar_id": calendar_id,
        "title": getattr(ev, "summary", None),
        "summary": getattr(ev, "summary", None),
        "description": getattr(ev, "description", None),
        "start_time": _time_to_iso(getattr(ev, "start_time", None)),
        "end_time": _time_to_iso(getattr(ev, "end_time", None)),
        "link": getattr(ev, "app_link", None),
        "attendees": attendees,
        "location": _location_to_dict(getattr(ev, "location", None)),
        "visibility": getattr(ev, "visibility", None),
    }


def _location_to_dict(location: Any) -> dict[str, Any] | None:
    if not location:
        return None
    return {
        "name": getattr(location, "name", None),
        "address": getattr(location, "address", None),
        "latitude": getattr(location, "latitude", None),
        "longitude": getattr(location, "longitude", None),
    }


def _time_to_iso(time_info: Any) -> str | None:
    if not time_info:
        return None
    ts = getattr(time_info, "timestamp", None) or getattr(time_info, "time_stamp", None)
    if ts:
        return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()
    return getattr(time_info, "date", None)


async def create_calendar(*, summary: str) -> str:
    from lark_oapi.api.calendar.v4 import Calendar, CreateCalendarRequest

    calendar = Calendar.builder().summary(summary).build()
    req = CreateCalendarRequest.builder().request_body(calendar).build()
    resp = await asyncio.to_thread(_lark_client().calendar.v4.calendar.create, req)
    if not resp.success():
        raise RuntimeError(f"calendar.create_calendar failed: {resp.code} {resp.msg}")
    return resp.data.calendar.calendar_id


async def create_event(
    *,
    calendar_id: str,
    title: str,
    start_time: str,
    end_time: str,
    description: str = "",
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    from lark_oapi.api.calendar.v4 import CalendarEvent, CreateCalendarEventRequest

    event = (
        CalendarEvent.builder()
        .summary(title)
        .description(description)
        .start_time(_event_time(start_time))
        .end_time(_event_time(end_time))
        .attendee_ability("can_modify_event")
        .need_notification(True)
        .build()
    )
    req = CreateCalendarEventRequest.builder().calendar_id(calendar_id).user_id_type("open_id").request_body(event)
    if idempotency_key:
        req = req.idempotency_key(idempotency_key)
    resp = await asyncio.to_thread(_lark_client().calendar.v4.calendar_event.create, req.build())
    if not resp.success():
        raise RuntimeError(f"calendar.create_event failed: {resp.code} {resp.msg}")
    ev = resp.data.event
    return {
        "event_id": ev.event_id,
        "link": getattr(ev, "app_link", None),
        "calendar_id": calendar_id,
        "start_time": start_time,
        "end_time": end_time,
    }


async def invite_attendees(calendar_id: str, event_id: str, open_ids: list[str]) -> None:
    from lark_oapi.api.calendar.v4 import (
        CalendarEventAttendee,
        CreateCalendarEventAttendeeRequest,
        CreateCalendarEventAttendeeRequestBody,
    )

    if not open_ids:
        return
    attendees = [CalendarEventAttendee.builder().type("user").user_id(oid).build() for oid in open_ids]
    body = CreateCalendarEventAttendeeRequestBody.builder().attendees(attendees).need_notification(True).build()
    req = (
        CreateCalendarEventAttendeeRequest.builder()
        .calendar_id(calendar_id)
        .event_id(event_id)
        .user_id_type("open_id")
        .request_body(body)
        .build()
    )
    resp = await asyncio.to_thread(_lark_client().calendar.v4.calendar_event_attendee.create, req)
    if not resp.success():
        raise RuntimeError(f"calendar.invite_attendees failed: {resp.code} {resp.msg}")


async def get_event(calendar_id: str, event_id: str) -> dict[str, Any]:
    from lark_oapi.api.calendar.v4 import GetCalendarEventRequest

    req = (
        GetCalendarEventRequest.builder()
        .calendar_id(calendar_id)
        .event_id(event_id)
        .need_attendee(True)
        .user_id_type("open_id")
        .build()
    )
    resp = await asyncio.to_thread(_lark_client().calendar.v4.calendar_event.get, req)
    if not resp.success():
        raise RuntimeError(f"calendar.get_event failed: {resp.code} {resp.msg}")
    return _event_to_dict(calendar_id, resp.data.event)


async def delete_event(calendar_id: str, event_id: str) -> None:
    from lark_oapi.api.calendar.v4 import DeleteCalendarEventRequest

    req = DeleteCalendarEventRequest.builder().calendar_id(calendar_id).event_id(event_id).build()
    resp = await asyncio.to_thread(_lark_client().calendar.v4.calendar_event.delete, req)
    if not resp.success() and not _is_not_found(resp):
        raise RuntimeError(f"calendar.delete_event failed: {resp.code} {resp.msg}")


async def restore_event(snapshot: dict[str, Any]) -> dict[str, Any]:
    created = await create_event(
        calendar_id=snapshot["calendar_id"],
        title=snapshot.get("summary") or snapshot.get("title") or "Restored meeting",
        start_time=snapshot["start_time"],
        end_time=snapshot["end_time"],
        description=snapshot.get("description") or "",
        idempotency_key="restore:" + (snapshot.get("event_id") or ""),
    )
    await invite_attendees(snapshot["calendar_id"], created["event_id"], snapshot.get("attendees") or [])
    return created


async def batch_freebusy(open_ids: list[str], time_min: str, time_max: str) -> list[dict[str, Any]]:
    from lark_oapi.api.calendar.v4 import BatchFreebusyRequest, BatchFreebusyRequestBody

    if not open_ids:
        return []
    body = (
        BatchFreebusyRequestBody.builder()
        .user_ids(open_ids)
        .time_min(time_min)
        .time_max(time_max)
        .include_external_calendar(False)
        .only_busy(True)
        .build()
    )
    req = BatchFreebusyRequest.builder().user_id_type("open_id").request_body(body).build()
    resp = await asyncio.to_thread(_lark_client().calendar.v4.freebusy.batch, req)
    if not resp.success():
        raise RuntimeError(f"calendar.batch_freebusy failed: {resp.code} {resp.msg}")
    conflicts: list[dict[str, Any]] = []
    for user in (resp.data.freebusy_lists or []):
        for item in (getattr(user, "freebusy_items", None) or []):
            conflicts.append({
                "open_id": getattr(user, "user_id", None),
                "start_time": getattr(item, "start_time", None),
                "end_time": getattr(item, "end_time", None),
                "rsvp_status": getattr(item, "rsvp_status", None),
            })
    return conflicts


async def primary_calendar_id(open_id: str) -> str | None:
    from lark_oapi.api.calendar.v4 import CalendarPrimaryBatchReq, PrimarysCalendarRequest

    body = CalendarPrimaryBatchReq.builder().user_ids([open_id]).build()
    req = PrimarysCalendarRequest.builder().user_id_type("open_id").request_body(body).build()
    resp = await asyncio.to_thread(_lark_client().calendar.v4.calendar.primarys, req)
    if not resp.success():
        raise RuntimeError(f"calendar.primary_calendar_id failed: {resp.code} {resp.msg}")
    for item in (resp.data.calendars or []):
        if getattr(item, "user_id", None) == open_id and getattr(item, "calendar", None):
            return item.calendar.calendar_id
    if resp.data.calendars:
        first = resp.data.calendars[0]
        return getattr(getattr(first, "calendar", None), "calendar_id", None)
    return None


async def list_events(calendar_id: str, time_min: str, time_max: str) -> list[dict[str, Any]]:
    from lark_oapi.api.calendar.v4 import ListCalendarEventRequest

    events: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        req = (
            ListCalendarEventRequest.builder()
            .calendar_id(calendar_id)
            .start_time(_timestamp(time_min))
            .end_time(_timestamp(time_max))
            .page_size(500)
            .user_id_type("open_id")
        )
        if page_token:
            req = req.page_token(page_token)
        resp = await asyncio.to_thread(_lark_client().calendar.v4.calendar_event.list, req.build())
        if not resp.success():
            raise RuntimeError(f"calendar.list_events failed: {resp.code} {resp.msg}")
        events.extend(_event_to_dict(calendar_id, ev) for ev in (resp.data.items or []))
        if not resp.data.has_more:
            return events
        page_token = resp.data.page_token


async def list_event_instances(calendar_id: str, time_min: str, time_max: str) -> list[dict[str, Any]]:
    from lark_oapi.api.calendar.v4 import InstanceViewCalendarEventRequest

    req = (
        InstanceViewCalendarEventRequest.builder()
        .calendar_id(calendar_id)
        .start_time(_timestamp(time_min))
        .end_time(_timestamp(time_max))
        .user_id_type("open_id")
        .build()
    )
    resp = await asyncio.to_thread(_lark_client().calendar.v4.calendar_event.instance_view, req)
    if not resp.success():
        raise RuntimeError(f"calendar.list_event_instances failed: {resp.code} {resp.msg}")
    return [_event_to_dict(calendar_id, ev) for ev in (resp.data.items or [])]
