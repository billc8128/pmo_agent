from __future__ import annotations

from agent.tool_utils import logical_key
from agent.canonical_args import canonicalize_args


def test_attendee_order_does_not_affect_logical_key():
    base = {
        "title": "Design review",
        "start_time": "2026-05-08T15:00:00+08:00",
        "attendee_open_ids": ["ou_a", "ou_b"],
    }
    swapped = {**base, "attendee_open_ids": ["ou_b", "ou_a"]}

    assert logical_key(chat_id="c", sender_open_id="s", action_type="schedule_meeting", args=base) == logical_key(
        chat_id="c", sender_open_id="s", action_type="schedule_meeting", args=swapped
    )


def test_timestamp_offsets_normalize_to_same_logical_key():
    a = {"title": "X", "start_time": "2026-05-08T15:00:00+08:00"}
    b = {"title": "X", "start_time": "2026-05-08T07:00:00Z"}

    assert logical_key(chat_id="c", sender_open_id="s", action_type="schedule_meeting", args=a) == logical_key(
        chat_id="c", sender_open_id="s", action_type="schedule_meeting", args=b
    )


def test_default_schedule_values_normalize_to_same_logical_key():
    a = {"title": "X", "start_time": "2026-05-08T15:00:00+08:00"}
    b = {
        "title": "X",
        "start_time": "2026-05-08T15:00:00+08:00",
        "duration_minutes": 30,
        "include_asker": True,
    }

    assert canonicalize_args("schedule_meeting", a) == canonicalize_args("schedule_meeting", b)


def test_trailing_newline_in_markdown_does_not_affect_logical_key():
    a = {"title": "Notes", "markdown_body": "# Notes\nbody"}
    b = {"title": "Notes", "markdown_body": "# Notes\nbody\n"}

    assert logical_key(chat_id="c", sender_open_id="s", action_type="create_doc", args=a) == logical_key(
        chat_id="c", sender_open_id="s", action_type="create_doc", args=b
    )


def test_action_item_order_does_not_affect_logical_key():
    a = {
        "items": [
            {"title": "B", "owner_open_id": "ou_b"},
            {"title": "A", "owner_open_id": "ou_a"},
        ],
        "project": "/repo",
    }
    b = {
        "project": "/repo",
        "items": [
            {"title": "A", "owner_open_id": "ou_a", "status": "todo"},
            {"title": "B", "owner_open_id": "ou_b", "status": "todo"},
        ],
    }

    assert logical_key(chat_id="c", sender_open_id="s", action_type="append_action_items", args=a) == logical_key(
        chat_id="c", sender_open_id="s", action_type="append_action_items", args=b
    )
