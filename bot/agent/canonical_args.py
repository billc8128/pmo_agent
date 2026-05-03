from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


def canonicalize_args(action_type: str, args: dict[str, Any]) -> dict[str, Any]:
    if action_type in {"schedule_meeting", "restore_schedule_meeting"}:
        return {
            "title": _clean(args.get("title")),
            "start_time_utc": _to_utc_iso(args.get("start_time")),
            "duration_minutes": int(args.get("duration_minutes") or 30),
            "attendee_open_ids": sorted(set(args.get("attendee_open_ids") or [])),
            "description_sha256": _sha256(_body(args.get("description"))),
            "include_asker": bool(args.get("include_asker", True)),
        }
    if action_type == "cancel_meeting":
        return {
            "event_id": _clean(args.get("event_id")),
            "last": bool(args.get("last")),
        }
    if action_type == "append_action_items":
        items = sorted(
            (_canonical_action_item(item) for item in (args.get("items") or [])),
            key=lambda item: (item["project"], item["title"], item["owner_open_id"]),
        )
        return {
            "items": items,
            "project": _clean(args.get("project")),
            "meeting_event_id": _clean(args.get("meeting_event_id")),
        }
    if action_type in {"create_doc", "create_meeting_doc"}:
        return {
            "title": _clean(args.get("title")),
            "markdown_sha256": _sha256(_body(args.get("markdown_body"))),
            "meeting_event_id": _clean(args.get("meeting_event_id")),
        }
    if action_type == "append_to_doc":
        return {
            "doc_link_or_token": _clean(args.get("doc_link_or_token")),
            "heading": _clean(args.get("heading")),
            "markdown_sha256": _sha256(_body(args.get("markdown_body"))),
        }
    if action_type == "create_bitable_table":
        return {
            "name": _clean(args.get("name")),
            "fields": [_canonical_field(field) for field in (args.get("fields") or [])],
        }
    if action_type == "append_to_my_table":
        return {
            "table_id": _clean(args.get("table_id")),
            "records_sha256": _sha256(_stable(args.get("records") or [])),
        }
    return json.loads(_stable(args))


def _canonical_action_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": _clean(item.get("title")),
        "owner_open_id": _clean(item.get("owner_open_id")),
        "due_date": _to_utc_iso(item.get("due_date")),
        "project": _clean(item.get("project")),
        "status": item.get("status") or "todo",
    }


def _canonical_field(field: dict[str, Any]) -> dict[str, Any]:
    raw_options = field.get("options") or {}
    choices = raw_options.get("choices") if isinstance(raw_options, dict) else None
    return {
        "name": _clean(field.get("name") or field.get("field_name")),
        "type": str(field.get("type") or ""),
        "options": sorted(choices or field.get("choices") or []),
    }


def _to_utc_iso(value: Any) -> str:
    if not value:
        return ""
    text = str(value).strip()
    if "T" not in text:
        return text
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if dt.tzinfo is None:
        return dt.isoformat()
    return dt.astimezone(timezone.utc).isoformat()


def _body(value: Any) -> str:
    return str(value or "").rstrip("\n")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
