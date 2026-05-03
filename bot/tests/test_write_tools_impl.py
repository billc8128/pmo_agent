from __future__ import annotations

import asyncio

from agent.request_context import RequestContext
from agent.tool_utils import content_payload
from agent.tools_impl import bitable_impl, calendar_impl, doc_impl
from agent.tools_impl.common import start_action
from agent.tools_meta import _undo_row, build_meta_tools
from feishu.bitable import _build_filter


def _ctx() -> RequestContext:
    return RequestContext(
        message_id="msg-1",
        chat_id="chat-1",
        sender_open_id="ou_asker",
        conversation_key="chat-1:ou_asker",
    )


def _patch_start(monkeypatch, row=None):
    row = row or {"id": "act-1"}
    monkeypatch.setattr("db.queries.get_bot_action", lambda message_id, action_type: None)
    monkeypatch.setattr("db.queries.get_locked_by_logical_key", lambda logical_key: None)
    monkeypatch.setattr("db.queries.insert_bot_action_pending", lambda **kwargs: row)
    return row


def _meta_tool(ctx: RequestContext, name: str):
    return next(t for t in build_meta_tools(ctx) if t.name == name).handler


def test_today_iso_uses_feishu_contact_timezone(monkeypatch):
    monkeypatch.setattr(
        "feishu.contact.get_user",
        lambda open_id: asyncio.sleep(0, result={"time_zone": "America/Los_Angeles"}),
    )

    result = asyncio.run(_meta_tool(_ctx(), "today_iso")({}))
    payload = content_payload(result)

    assert payload["user_timezone"] == "America/Los_Angeles"
    assert payload["user_timezone_source"] == "feishu_contact"


def test_resolve_people_uses_local_phone_link_before_remote_lookup(monkeypatch):
    monkeypatch.setattr("db.queries.lookup_profile_by_handle_or_display", lambda *args, **kwargs: None)
    monkeypatch.setattr("db.queries.lookup_feishu_link_by_email", lambda *args, **kwargs: None)
    monkeypatch.setattr("db.queries.lookup_feishu_link_by_phone", lambda phone: {
        "user_id": "user-1",
        "handle": "alice",
        "display_name": "Alice",
        "open_id": "ou_alice",
        "email": None,
        "mobile": phone,
    })
    monkeypatch.setattr(
        "feishu.contact.batch_get_id_by_email_or_phone",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("local phone link should avoid remote lookup")),
    )

    result = asyncio.run(_meta_tool(_ctx(), "resolve_people")({"people": [{"phone": "13800138000"}]}))
    payload = content_payload(result)

    assert payload["resolved"][0]["open_id"] == "ou_alice"
    assert payload["resolved"][0]["source"] == "profiles"


def test_schedule_meeting_invite_failure_becomes_partial_success(monkeypatch):
    _patch_start(monkeypatch)
    monkeypatch.setattr("db.queries.get_bot_workspace", lambda: {"calendar_id": "cal-1"})
    monkeypatch.setattr("feishu.calendar.batch_freebusy", lambda *args, **kwargs: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(
        "feishu.calendar.create_event",
        lambda **kwargs: asyncio.sleep(0, result={"event_id": "evt-1", "calendar_id": "cal-1", "link": "url"}),
    )

    async def fail_invite(*args, **kwargs):
        raise RuntimeError("invite failed")

    monkeypatch.setattr("feishu.calendar.invite_attendees", fail_invite)
    pending_updates = []
    reconciled = []
    monkeypatch.setattr("db.queries.record_bot_action_target_pending", lambda *args, **kwargs: pending_updates.append(kwargs))
    monkeypatch.setattr("db.queries.mark_bot_action_reconciled_unknown", lambda *args, **kwargs: reconciled.append(kwargs))
    monkeypatch.setattr("db.queries.mark_bot_action_failed", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not mark failed")))

    result = asyncio.run(calendar_impl.schedule_meeting(_ctx(), {
        "title": "Design review",
        "start_time": "2026-05-04T10:00:00+08:00",
        "duration_minutes": 30,
        "attendee_open_ids": ["ou_albert"],
    }))

    assert result["isError"] is True
    assert pending_updates[0]["target_id"] == "evt-1"
    assert reconciled[0]["reconciliation_kind"] == "partial_success"
    assert reconciled[0]["keep_lock"] is True


def test_schedule_meeting_freebusy_conflict_is_success_without_event_create(monkeypatch):
    _patch_start(monkeypatch)
    monkeypatch.setattr("db.queries.get_bot_workspace", lambda: {"calendar_id": "cal-1"})
    monkeypatch.setattr(
        "feishu.calendar.batch_freebusy",
        lambda *args, **kwargs: asyncio.sleep(0, result=[{"open_id": "ou_albert", "start_time": "x", "end_time": "y"}]),
    )
    monkeypatch.setattr("feishu.calendar.create_event", lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not create event")))
    successes = []
    monkeypatch.setattr("db.queries.mark_bot_action_success", lambda *args, **kwargs: successes.append((args, kwargs)))

    result = asyncio.run(calendar_impl.schedule_meeting(_ctx(), {
        "title": "Design review",
        "start_time": "2026-05-04T10:00:00+08:00",
        "duration_minutes": 30,
        "attendee_open_ids": ["ou_albert"],
    }))

    payload = content_payload(result)
    assert payload["outcome"] == "conflict"
    assert successes[0][1] == {}


def test_start_action_conflict_logical_replay_says_no_meeting_was_created(monkeypatch):
    monkeypatch.setattr("db.queries.get_bot_action", lambda *args: None)
    monkeypatch.setattr("db.queries.get_locked_by_logical_key", lambda *args: {
        "id": "act-1",
        "action_type": "schedule_meeting",
        "status": "success",
        "result": {"outcome": "conflict", "conflicts": [{"open_id": "ou_a"}]},
    })

    row, replay = start_action(_ctx(), "schedule_meeting", {"title": "X"})
    payload = content_payload(replay)

    assert row is None
    assert payload["deduplicated_from_logical_key"] is True
    assert payload["outcome"] == "conflict"
    assert payload["meeting_created"] is False
    assert "not" in payload["agent_directive"].lower()


def test_start_action_non_meeting_conflict_replay_does_not_emit_meeting_directive(monkeypatch):
    monkeypatch.setattr("db.queries.get_bot_action", lambda *args: None)
    monkeypatch.setattr("db.queries.get_locked_by_logical_key", lambda *args: {
        "id": "act-1",
        "action_type": "append_action_items",
        "status": "success",
        "result": {"outcome": "conflict", "records": []},
    })

    row, replay = start_action(_ctx(), "append_action_items", {"items": []})
    payload = content_payload(replay)

    assert row is None
    assert payload["outcome"] == "conflict"
    assert "meeting_created" not in payload
    assert "agent_directive" not in payload


def test_start_action_surfaces_reconciled_unknown_instead_of_success_replay(monkeypatch):
    monkeypatch.setattr("db.queries.get_bot_action", lambda *args: {
        "id": "act-1",
        "status": "reconciled_unknown",
        "result": {"event_id": "evt-1", "reconciliation_kind": "partial_success"},
    })
    monkeypatch.setattr("db.queries.get_locked_by_logical_key", lambda *args: (_ for _ in ()).throw(AssertionError("message replay should stop first")))

    row, replay = start_action(_ctx(), "schedule_meeting", {"title": "X"})
    payload = content_payload(replay)

    assert row is None
    assert payload["reconciled_unknown"] is True
    assert payload["suggest_undo"] is True
    assert payload["source_action_id"] == "act-1"


def test_start_action_failed_message_requires_new_message(monkeypatch):
    monkeypatch.setattr("db.queries.get_bot_action", lambda *args: {"id": "act-1", "status": "failed", "result": {}})
    monkeypatch.setattr("db.queries.update_for_retry", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not retry same message")))

    row, replay = start_action(_ctx(), "schedule_meeting", {"title": "X"})

    assert row is None
    assert replay["isError"] is True


def test_cancel_meeting_delete_failure_after_snapshot_becomes_partial_success(monkeypatch):
    _patch_start(monkeypatch)
    monkeypatch.setattr("db.queries.get_bot_action_by_target", lambda **kwargs: {
        "id": "schedule-1",
        "result": {"calendar_id": "cal-1"},
    })
    monkeypatch.setattr("feishu.calendar.get_event", lambda *args, **kwargs: asyncio.sleep(0, result={
        "event_id": "evt-1",
        "summary": "Design review",
        "start_time": "2026-05-04T10:00:00+08:00",
        "end_time": "2026-05-04T10:30:00+08:00",
    }))

    async def fail_delete(*args, **kwargs):
        raise RuntimeError("delete failed")

    monkeypatch.setattr("feishu.calendar.delete_event", fail_delete)
    pending_updates = []
    reconciled = []
    monkeypatch.setattr("db.queries.record_bot_action_target_pending", lambda *args, **kwargs: pending_updates.append(kwargs))
    monkeypatch.setattr("db.queries.mark_bot_action_reconciled_unknown", lambda *args, **kwargs: reconciled.append(kwargs))
    monkeypatch.setattr("db.queries.mark_bot_action_failed", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not mark failed after snapshot")))

    result = asyncio.run(calendar_impl.cancel_meeting(_ctx(), {"event_id": "evt-1"}))

    assert result["isError"] is True
    assert pending_updates[0]["result_patch"]["pre_cancel_event_snapshot"]["calendar_id"] == "cal-1"
    assert reconciled[0]["reconciliation_kind"] == "partial_success"
    assert reconciled[0]["keep_lock"] is True


def test_cancel_meeting_webhook_retry_after_success_replays_cached_result(monkeypatch):
    cached_result = {"cancelled": True, "event_id": "evt-1", "calendar_id": "cal-1"}
    monkeypatch.setattr("db.queries.get_bot_action", lambda message_id, action_type: {
        "id": "cancel-1",
        "status": "success",
        "result": cached_result,
    })
    monkeypatch.setattr(
        "db.queries.last_meeting_action_for_sender_in_chat",
        lambda *args: (_ for _ in ()).throw(AssertionError("source lookup must not run after cache hit")),
    )

    result = asyncio.run(calendar_impl.cancel_meeting(_ctx(), {"last": True}))
    payload = content_payload(result)

    assert payload["cancelled"] is True
    assert payload["event_id"] == "evt-1"
    assert payload["cached_result"] is True


def test_create_doc_persists_source_ticket_and_doc_token(monkeypatch):
    _patch_start(monkeypatch)
    monkeypatch.setattr("db.queries.get_bot_workspace", lambda: {"docs_folder_token": "fld-1"})
    monkeypatch.setattr("feishu.drive.upload_markdown_source", lambda *args, **kwargs: asyncio.sleep(0, result="file-1"))
    monkeypatch.setattr("feishu.drive.create_import_task", lambda *args, **kwargs: asyncio.sleep(0, result="ticket-1"))
    monkeypatch.setattr(
        "feishu.drive.poll_import_task",
        lambda *args, **kwargs: asyncio.sleep(0, result={"doc_token": "doc-1", "url": "url-1"}),
    )
    updates = []
    monkeypatch.setattr("db.queries.record_bot_action_target_pending", lambda *args, **kwargs: updates.append(kwargs))
    monkeypatch.setattr("db.queries.mark_bot_action_success", lambda *args, **kwargs: None)

    result = asyncio.run(doc_impl.create_doc(_ctx(), {"title": "Notes", "markdown_body": "# Hello"}))
    payload = content_payload(result)

    assert payload["doc_token"] == "doc-1"
    assert updates[0]["result_patch"]["source_file_token"] == "file-1"
    assert updates[1]["result_patch"]["import_ticket"] == "ticket-1"
    assert updates[2]["target_id"] == "doc-1"
    assert updates[2]["target_kind"] == "docx"


def test_append_to_doc_uses_document_token_as_root_parent(monkeypatch):
    _patch_start(monkeypatch)
    monkeypatch.setattr("db.queries.is_doc_authored_by_bot", lambda token: True)
    parents = []
    monkeypatch.setattr("feishu.docx.list_child_blocks", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("append must not prefetch children")))
    monkeypatch.setattr("feishu.docx.append_blocks", lambda token, parent, blocks, **kwargs: parents.append(("append", token, parent, kwargs.get("client_token"))) or asyncio.sleep(0, result=["blk-1"]))
    updates = []
    monkeypatch.setattr("db.queries.record_bot_action_target_pending", lambda *args, **kwargs: updates.append(kwargs))
    monkeypatch.setattr("db.queries.mark_bot_action_success", lambda *args, **kwargs: None)

    result = asyncio.run(doc_impl.append_to_doc(_ctx(), {"doc_link_or_token": "doc-1", "markdown_body": "hello"}))
    payload = content_payload(result)

    assert payload["parent_block_id"] == "doc-1"
    assert parents == [("append", "doc-1", "doc-1", "act-1")]
    assert updates[0]["target_kind"] == "docx_block_append"


def test_bitable_filter_string_builds_sdk_filter():
    filter_info = _build_filter('project="/repo" AND status="todo"')
    assert filter_info.conjunction == "and"
    assert [c.field_name for c in filter_info.conditions] == ["project", "status"]
    assert [c.operator for c in filter_info.conditions] == ["is", "is"]
    assert [c.value for c in filter_info.conditions] == [["/repo"], ["todo"]]


def test_undo_cancel_restores_event_with_restore_audit(monkeypatch):
    async def missing_event(*args, **kwargs):
        raise RuntimeError("404 not found")

    monkeypatch.setattr("db.queries.get_bot_workspace", lambda: {"calendar_id": "cal-1"})
    monkeypatch.setattr("feishu.calendar.get_event", missing_event)
    monkeypatch.setattr(
        "feishu.calendar.create_event",
        lambda **kwargs: asyncio.sleep(0, result={"event_id": "evt-restored", "calendar_id": "cal-1"}),
    )
    monkeypatch.setattr("feishu.calendar.invite_attendees", lambda *args, **kwargs: asyncio.sleep(0, result=None))
    monkeypatch.setattr("db.queries.get_bot_action", lambda *args, **kwargs: None)
    monkeypatch.setattr("db.queries.insert_bot_action_pending", lambda **kwargs: {"id": "restore-1", **kwargs})
    pending_updates = []
    successes = []
    retired = []
    monkeypatch.setattr("db.queries.record_bot_action_target_pending", lambda *args, **kwargs: pending_updates.append((args, kwargs)))
    monkeypatch.setattr("db.queries.mark_bot_action_success", lambda *args, **kwargs: successes.append((args, kwargs)))
    monkeypatch.setattr("db.queries.retire_source_action", lambda action_id: retired.append(action_id))
    monkeypatch.setattr("db.queries.record_undo_audit", lambda row: None)

    result = asyncio.run(_undo_row({
        "id": "cancel-1",
        "chat_id": "chat-1",
        "sender_open_id": "ou_asker",
        "action_type": "cancel_meeting",
        "status": "success",
        "target_id": "evt-old",
        "target_kind": "calendar_event_cancel",
        "result": {
            "calendar_id": "cal-1",
            "source_meeting_action_id": "schedule-1",
            "pre_cancel_event_snapshot": {
                "calendar_id": "cal-1",
                "summary": "Old meeting",
                "start_time": "2026-05-04T10:00:00+08:00",
                "end_time": "2026-05-04T10:30:00+08:00",
                "attendees": ["ou_asker", "ou_albert"],
            },
        },
    }))

    assert result["status"] == "undone"
    assert pending_updates[0][1]["target_id"] == "evt-restored"
    assert successes[0][0][0] == "restore-1"
    assert retired == ["schedule-1", "cancel-1"]


def test_undo_cancel_restore_invite_failure_records_partial_audit_without_retiring_cancel(monkeypatch):
    async def missing_event(*args, **kwargs):
        raise RuntimeError("404 not found")

    async def fail_invite(*args, **kwargs):
        raise RuntimeError("invite failed")

    monkeypatch.setattr("db.queries.get_bot_workspace", lambda: {"calendar_id": "cal-1"})
    monkeypatch.setattr("feishu.calendar.get_event", missing_event)
    monkeypatch.setattr(
        "feishu.calendar.create_event",
        lambda **kwargs: asyncio.sleep(0, result={"event_id": "evt-restored", "calendar_id": "cal-1"}),
    )
    monkeypatch.setattr("feishu.calendar.invite_attendees", fail_invite)
    monkeypatch.setattr("db.queries.get_bot_action", lambda *args, **kwargs: None)
    monkeypatch.setattr("db.queries.insert_bot_action_pending", lambda **kwargs: {"id": "restore-1", **kwargs})
    monkeypatch.setattr("db.queries.record_bot_action_target_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr("db.queries.mark_bot_action_success", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("restore must not be success")))
    reconciled = []
    retired = []
    audits = []
    monkeypatch.setattr("db.queries.mark_bot_action_reconciled_unknown", lambda *args, **kwargs: reconciled.append((args, kwargs)))
    monkeypatch.setattr("db.queries.retire_source_action", lambda action_id: retired.append(action_id))
    monkeypatch.setattr("db.queries.record_undo_audit", lambda row, **kwargs: audits.append((row, kwargs)))

    result = asyncio.run(_undo_row({
        "id": "cancel-1",
        "chat_id": "chat-1",
        "sender_open_id": "ou_asker",
        "action_type": "cancel_meeting",
        "status": "success",
        "target_id": "evt-old",
        "target_kind": "calendar_event_cancel",
        "result": {
            "calendar_id": "cal-1",
            "source_meeting_action_id": "schedule-1",
            "pre_cancel_event_snapshot": {
                "calendar_id": "cal-1",
                "summary": "Old meeting",
                "start_time": "2026-05-04T10:00:00+08:00",
                "end_time": "2026-05-04T10:30:00+08:00",
                "attendees": ["ou_asker", "ou_albert"],
            },
        },
    }))

    assert result["status"] == "partial_success"
    assert result["restore_action_id"] == "restore-1"
    assert reconciled[0][0][0] == "restore-1"
    assert audits[0][1]["status"] == "reconciled_unknown"
    assert retired == ["schedule-1"]


def test_append_action_items_uses_recent_project_when_confident(monkeypatch):
    _patch_start(monkeypatch)
    monkeypatch.setattr("db.queries.get_bot_workspace", lambda: {
        "base_app_token": "base-1",
        "action_items_table_id": "tbl-actions",
        "meetings_table_id": "tbl-meetings",
    })
    monkeypatch.setattr("db.queries.lookup_by_feishu_open_id", lambda open_id: {"user_id": "user-1"})
    monkeypatch.setattr("db.queries.project_root_for_row", lambda row: row["project_root"])
    monkeypatch.setattr("db.queries.recent_turns", lambda *args, **kwargs: [
        {"project_root": "/repo/a", "user_message_at": "2026-05-01T01:00:00Z"},
        {"project_root": "/repo/a", "user_message_at": "2026-05-02T01:00:00Z"},
        {"project_root": "/repo/a", "user_message_at": "2026-05-03T01:00:00Z"},
    ])
    monkeypatch.setattr("feishu.bitable.batch_create_records", lambda *args, **kwargs: asyncio.sleep(0, result=["rec-1"]))
    monkeypatch.setattr("db.queries.record_bot_action_target_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr("db.queries.mark_bot_action_success", lambda *args, **kwargs: None)

    result = asyncio.run(bitable_impl.append_action_items(_ctx(), {
        "items": [{"title": "ship write tools", "owner_open_id": "ou_asker"}],
    }))
    payload = content_payload(result)

    assert payload["records"][0]["project_used"] == "/repo/a"
    assert payload["records"][0]["project_source"] == "auto_recent_turns"


def test_append_to_my_table_uses_local_authorship_gate_not_table_exists(monkeypatch):
    _patch_start(monkeypatch)
    monkeypatch.setattr("db.queries.get_bot_workspace", lambda: {
        "base_app_token": "base-1",
        "action_items_table_id": "tbl-actions",
        "meetings_table_id": "tbl-meetings",
    })
    monkeypatch.setattr("db.queries.get_bot_action_by_target", lambda **kwargs: {"id": "create-table-1"})
    monkeypatch.setattr("feishu.bitable.table_exists", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not list all tables")))
    monkeypatch.setattr("feishu.bitable.list_fields", lambda *args, **kwargs: asyncio.sleep(0, result=[{"name": "title"}]))
    monkeypatch.setattr("feishu.bitable.batch_create_records", lambda *args, **kwargs: asyncio.sleep(0, result=["rec-1"]))
    monkeypatch.setattr("db.queries.record_bot_action_target_pending", lambda *args, **kwargs: None)
    monkeypatch.setattr("db.queries.mark_bot_action_success", lambda *args, **kwargs: None)

    result = asyncio.run(bitable_impl.append_to_my_table(_ctx(), {
        "table_id": "tbl-custom",
        "records": [{"title": "hello"}],
    }))
    payload = content_payload(result)

    assert payload["record_ids"] == ["rec-1"]
