from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.tool_utils import content_payload


class _FakeSupabase:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {}

    def table(self, name: str):
        self.tables.setdefault(name, [])
        return _FakeTable(self, name)


class _FakeTable:
    def __init__(self, client: _FakeSupabase, name: str) -> None:
        self.client = client
        self.name = name
        self.filters: list[tuple[str, str, object]] = []
        self._payload = None
        self._op = "select"
        self._maybe_single = False
        self._limit: int | None = None
        self._orders: list[tuple[str, bool]] = []

    def select(self, *args, **kwargs):
        return self

    def maybe_single(self):
        self._maybe_single = True
        return self

    def eq(self, column, value):
        self.filters.append(("eq", column, value))
        return self

    def gte(self, column, value):
        self.filters.append(("gte", column, value))
        return self

    def lte(self, column, value):
        self.filters.append(("lte", column, value))
        return self

    def is_(self, column, value):
        self.filters.append(("is", column, value))
        return self

    def order(self, column, desc=False):
        self._orders.append((column, bool(desc)))
        return self

    def limit(self, value):
        self._limit = int(value)
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def upsert(self, payload, on_conflict=None, **kwargs):
        self._op = "upsert"
        self._payload = (payload, on_conflict)
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def execute(self):
        if self._op == "insert":
            rows = self._payload if isinstance(self._payload, list) else [self._payload]
            inserted = []
            for row in rows:
                copy = deepcopy(row)
                copy.setdefault("id", len(self.client.tables[self.name]) + 1)
                self.client.tables[self.name].append(copy)
                inserted.append(copy)
            return SimpleNamespace(data=inserted)
        if self._op == "upsert":
            payload, on_conflict = self._payload
            rows = payload if isinstance(payload, list) else [payload]
            upserted = []
            conflict_cols = [c.strip() for c in str(on_conflict or "id").split(",")]
            for row in rows:
                copy = deepcopy(row)
                existing = next(
                    (
                        item
                        for item in self.client.tables[self.name]
                        if all(item.get(col) == copy.get(col) for col in conflict_cols)
                    ),
                    None,
                )
                if existing is None:
                    copy.setdefault("id", len(self.client.tables[self.name]) + 1)
                    self.client.tables[self.name].append(copy)
                    existing = copy
                else:
                    existing.update(copy)
                upserted.append(deepcopy(existing))
            return SimpleNamespace(data=upserted)
        if self._op == "update":
            rows = self._filtered()
            for row in rows:
                row.update(deepcopy(self._payload))
            return SimpleNamespace(data=rows)
        rows = self._filtered()
        if self._maybe_single:
            return SimpleNamespace(data=rows[0] if rows else None)
        return SimpleNamespace(data=rows)

    def _filtered(self):
        rows = list(self.client.tables[self.name])
        for op, column, value in self.filters:
            if op == "eq":
                rows = [row for row in rows if row.get(column) == value]
            elif op == "gte":
                rows = [row for row in rows if str(row.get(column) or "") >= str(value)]
            elif op == "lte":
                rows = [row for row in rows if str(row.get(column) or "") <= str(value)]
            elif op == "is" and value is None:
                rows = [row for row in rows if row.get(column) is None]
        for column, desc in reversed(self._orders):
            rows.sort(key=lambda row: str(row.get(column) or ""), reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return rows


def test_chat_redaction_patterns_cover_private_chat_shapes():
    from external.redaction import redact_text, redact_text_with_categories

    text = "\n".join(
        [
            "邮箱 albert@vibelive.com 的密码是 abc",
            "电话 13800138000 或 +86 13800138000",
            "身份证 11010519491231002X",
            "银行卡 4242424242424242",
            "ssh root@1.2.3.4:22 password=abc123",
            "[asker] handle=@admin user_id=00000000-0000-0000-0000-000000000000",
            "[parent_notification] id=123",
            "[IMAGE:img_123]",
        ]
    )

    redacted, text_categories = redact_text(text)
    categorized_redacted, categories = redact_text_with_categories(text)

    assert sum(text_categories.values()) >= 8
    assert categorized_redacted == redacted
    assert text_categories == categories
    assert categories["email"] == 1
    assert categories["phone"] >= 1
    assert categories["id_card"] == 1
    assert categories["payment_card"] == 1
    assert categories["sensitive_host"] == 1
    assert categories["assignment"] == 1
    assert categories["host_marker"] == 3
    assert "albert@vibelive.com" not in redacted
    assert "13800138000" not in redacted
    assert "+86 13800138000" not in redacted
    assert "11010519491231002X" not in redacted
    assert "4242424242424242" not in redacted
    assert "1.2.3.4:22" not in redacted
    assert "password=abc123" not in redacted
    assert "[asker]" not in redacted
    assert "[parent_notification]" not in redacted
    assert "[IMAGE:img_123]" not in redacted
    assert "[chat_memory_escaped_marker:asker]" in redacted


def test_chat_redaction_does_not_redact_arbitrary_long_numbers_as_payment_cards():
    from external.redaction import redact_text_with_categories

    redacted, categories = redact_text_with_categories("构建号 1234567890123 失败了")

    assert "1234567890123" in redacted
    assert categories.get("payment_card", 0) == 0


def test_redact_payload_returns_categories_recursively():
    from external.redaction import redact_payload

    payload = {
        "body": "contact albert@vibelive.com",
        "nested": ["token=abc123", {"hook": "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token"}],
    }

    redacted, categories = redact_payload(payload)

    assert redacted["body"] == "contact [REDACTED]"
    assert redacted["nested"][0] == "token=[REDACTED]"
    assert redacted["nested"][1]["hook"] == "[REDACTED]"
    assert categories["email"] == 1
    assert categories["assignment"] == 1
    assert categories["token"] == 1


def test_migration_0024_creates_chat_memory_tables():
    sql = Path("backend/supabase/migrations/0024_chat_memory.sql").read_text()

    assert "create table if not exists public.chat_memory_settings" in sql
    assert "create table if not exists public.chat_memory_settings_history" in sql
    assert "create table if not exists public.chat_messages" in sql
    assert "create table if not exists public.people_memory" in sql
    assert "create table if not exists public.people_memory_updates" in sql
    assert "feishu_message_id" in sql
    assert "people_loop_cursor" in sql
    assert "content_metadata" in sql
    assert "sender_is_bot" in sql
    assert "edited_at" in sql
    assert "deleted_at" in sql
    assert "references public.chat_memory_settings(chat_id) on delete cascade" in sql
    assert "between 1 and 730" in sql
    assert "message_type not in ('text', 'post')" in sql
    assert "where length(text_redacted) > 0" in sql
    assert "to_tsvector('simple', text_redacted)" in sql
    assert "people_memory_updates_source_time_idx" in sql
    assert "people_memory_updates_person_time_idx" in sql
    assert "comment on column public.chat_messages.redacted_payload" in sql
    assert "redacted" in sql
    assert "No RLS policies are created deliberately" in sql


def test_chat_memory_setting_helpers_write_history(monkeypatch):
    from db import queries

    fake = _FakeSupabase()
    monkeypatch.setattr(queries, "sb_admin", lambda: fake)

    enabled = queries.enable_chat_memory(
        "oc_1",
        user_id="profile-1",
        open_id="ou_1",
        retention_days=30,
    )

    assert enabled["enabled"] is True
    assert enabled["chat_id"] == "oc_1"
    assert enabled["enabled_by_user_id"] == "profile-1"
    assert queries.is_chat_memory_enabled("oc_1") is True
    assert queries.chat_memory_status("oc_1")["retention_days"] == 30

    disabled = queries.disable_chat_memory("oc_1", user_id=None, open_id="ou_2")

    assert disabled["enabled"] is False
    assert disabled["disabled_by_open_id"] == "ou_2"
    assert queries.is_chat_memory_enabled("oc_1") is False
    assert [row["action"] for row in fake.tables["chat_memory_settings_history"]] == [
        "enable",
        "disable",
    ]


def test_chat_memory_settings_are_idempotent_and_reset_current_actor_fields(monkeypatch):
    from db import queries

    fake = _FakeSupabase()
    monkeypatch.setattr(queries, "sb_admin", lambda: fake)

    queries.enable_chat_memory("oc_1", user_id="profile-1", open_id="ou_1", retention_days=30)
    queries.enable_chat_memory("oc_1", user_id="profile-1", open_id="ou_1", retention_days=30)
    disabled = queries.disable_chat_memory("oc_1", user_id="profile-2", open_id="ou_2")
    disabled_again = queries.disable_chat_memory("oc_1", user_id="profile-2", open_id="ou_2")

    assert [row["action"] for row in fake.tables["chat_memory_settings_history"]] == [
        "enable",
        "disable",
    ]
    assert disabled["enabled"] is False
    assert disabled_again["enabled"] is False
    assert disabled["enabled_at"] is None
    assert disabled["enabled_by_user_id"] is None
    assert disabled["enabled_by_open_id"] is None
    assert disabled["disabled_by_user_id"] == "profile-2"
    assert disabled["disabled_by_open_id"] == "ou_2"


def test_chat_memory_enabled_state_distinguishes_enabled_disabled_unconfigured(monkeypatch):
    from db import queries

    fake = _FakeSupabase()
    monkeypatch.setattr(queries, "sb_admin", lambda: fake)

    assert queries.chat_memory_enabled_state("oc_missing") is None
    queries.enable_chat_memory("oc_enabled", user_id=None, open_id="ou_1")
    queries.enable_chat_memory("oc_disabled", user_id=None, open_id="ou_1")
    queries.disable_chat_memory("oc_disabled", user_id=None, open_id="ou_2")

    assert queries.chat_memory_enabled_state("oc_enabled") is True
    assert queries.chat_memory_enabled_state("oc_disabled") is False
    assert queries.is_chat_memory_enabled("oc_missing") is False


def test_chat_message_helpers_are_idempotent_and_support_recall_edit(monkeypatch):
    from db import queries

    fake = _FakeSupabase()
    monkeypatch.setattr(queries, "sb_admin", lambda: fake)
    queries.enable_chat_memory("oc_1", user_id=None, open_id="ou_admin")

    row = {
        "feishu_message_id": "om_1",
        "chat_id": "oc_1",
        "chat_type": "group",
        "sender_open_id": "ou_1",
        "text_redacted": "",
        "occurred_at": "2026-05-07T10:00:00+08:00",
    }

    inserted = queries.insert_chat_message(row)
    duplicate = queries.insert_chat_message({**row, "text_redacted": "changed"})

    assert inserted["text_redacted"] == "[REDACTED]"
    assert duplicate["text_redacted"] == "changed"
    assert len(fake.tables["chat_messages"]) == 1

    assert queries.update_chat_message_text(
        "om_1",
        text_redacted="edited",
        redacted_payload={"text": "edited"},
        edited_at="2026-05-07T10:05:00+08:00",
    )
    assert fake.tables["chat_messages"][0]["text_redacted"] == "edited"
    assert queries.mark_chat_message_deleted("om_1")
    assert fake.tables["chat_messages"][0]["deleted_at"] is not None


def test_chat_message_helper_allows_empty_non_text_rows(monkeypatch):
    from db import queries

    fake = _FakeSupabase()
    monkeypatch.setattr(queries, "sb_admin", lambda: fake)
    queries.enable_chat_memory("oc_1", user_id=None, open_id="ou_admin")

    inserted = queries.insert_chat_message(
        {
            "feishu_message_id": "om_file",
            "chat_id": "oc_1",
            "chat_type": "group",
            "sender_open_id": "ou_1",
            "message_type": "file",
            "text_redacted": "",
            "content_metadata": {"file_name": "方案.pdf"},
            "occurred_at": "2026-05-07T10:00:00+08:00",
        }
    )

    assert inserted["text_redacted"] == ""
    assert inserted["content_metadata"] == {"file_name": "方案.pdf"}


def test_recent_chat_messages_excludes_deleted_and_bot_rows(monkeypatch):
    from db import queries

    fake = _FakeSupabase()
    fake.tables["chat_messages"] = [
        {
            "feishu_message_id": "om_1",
            "chat_id": "oc_1",
            "sender_open_id": "ou_1",
            "sender_display_name": "bcc",
            "text_redacted": "kept",
            "occurred_at": "2026-05-07T10:00:00+08:00",
            "deleted_at": None,
            "sender_is_bot": False,
        },
        {
            "feishu_message_id": "om_2",
            "chat_id": "oc_1",
            "text_redacted": "deleted",
            "occurred_at": "2026-05-07T10:01:00+08:00",
            "deleted_at": "2026-05-07T10:02:00+08:00",
            "sender_is_bot": False,
        },
        {
            "feishu_message_id": "om_3",
            "chat_id": "oc_1",
            "text_redacted": "bot",
            "occurred_at": "2026-05-07T10:03:00+08:00",
            "deleted_at": None,
            "sender_is_bot": True,
        },
    ]
    monkeypatch.setattr(queries, "sb_admin", lambda: fake)

    rows = queries.get_recent_chat_messages("oc_1", limit=20)

    assert [row["message_id"] for row in rows] == ["om_1"]
    assert rows[0]["text"] == "kept"
    assert "sender_open_id" not in rows[0]


def test_search_chat_messages_with_context_uses_current_chat_and_chinese_substring(monkeypatch):
    from db import queries

    fake = _FakeSupabase()
    fake.tables["chat_messages"] = [
        {
            "feishu_message_id": "om_1",
            "chat_id": "oc_current",
            "sender_display_name": "bcc",
            "text_redacted": "vibelive 播放器方案确认了",
            "occurred_at": "2026-05-07T10:00:00+08:00",
            "deleted_at": None,
            "sender_is_bot": False,
        },
        {
            "feishu_message_id": "om_other",
            "chat_id": "oc_other",
            "sender_display_name": "other",
            "text_redacted": "vibelive 播放器方案在别的群",
            "occurred_at": "2026-05-07T10:01:00+08:00",
            "deleted_at": None,
            "sender_is_bot": False,
        },
        {
            "feishu_message_id": "om_deleted",
            "chat_id": "oc_current",
            "sender_display_name": "bcc",
            "text_redacted": "vibelive 已撤回",
            "occurred_at": "2026-05-07T10:02:00+08:00",
            "deleted_at": "2026-05-07T10:03:00+08:00",
            "sender_is_bot": False,
        },
    ]
    monkeypatch.setattr(queries, "sb_admin", lambda: fake)

    hits = queries.search_chat_messages_with_context("oc_current", query="播放器方案", limit=5, before=1, after=1)

    assert len(hits) == 1
    assert hits[0]["hit"]["message_id"] == "om_1"
    assert [row["message_id"] for row in hits[0]["context"]] == ["om_1"]


def test_search_chat_messages_with_context_anchor_returns_ordered_window(monkeypatch):
    from db import queries

    fake = _FakeSupabase()
    fake.tables["chat_messages"] = [
        {
            "feishu_message_id": f"om_{i}",
            "chat_id": "oc_current",
            "sender_display_name": "bcc",
            "text_redacted": f"消息 {i}",
            "occurred_at": f"2026-05-07T10:0{i}:00+08:00",
            "deleted_at": None,
            "sender_is_bot": False,
        }
        for i in range(5)
    ]
    monkeypatch.setattr(queries, "sb_admin", lambda: fake)

    hits = queries.search_chat_messages_with_context(
        "oc_current",
        anchor_message_id="om_2",
        before=1,
        after=2,
    )

    assert len(hits) == 1
    assert hits[0]["hit"]["message_id"] == "om_2"
    assert [row["message_id"] for row in hits[0]["context"]] == ["om_1", "om_2", "om_3", "om_4"]


def _message_body(*, message_type="text", content=None, mentions=None, create_time="1778126400000"):
    if content is None:
        content = {"text": "hi @_user_1"}
    return {
        "header": {
            "event_id": "evt_1",
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {
                "sender_id": {"open_id": "ou_sender"},
                "sender_name": "bcc",
            },
            "message": {
                "message_id": "om_1",
                "root_id": "om_root",
                "parent_id": "om_parent",
                "chat_id": "oc_1",
                "chat_type": "group",
                "chat_member_id": "ocm_1",
                "message_type": message_type,
                "create_time": create_time,
                "content": content if isinstance(content, str) else json.dumps(content),
                "mentions": mentions
                if mentions is not None
                else [{"id": {"open_id": "ou_bot"}, "name": "包工头", "key": "@_user_1"}],
            },
        },
    }


def test_parse_message_event_exposes_chat_memory_fields_for_text():
    from feishu import events

    events.set_self_identity(open_id="ou_bot", name="包工头")

    parsed = events.parse_message_event(_message_body(content={"text": "hi @_user_1 决策通过"}))

    assert parsed is not None
    assert parsed.text == "hi  决策通过" or parsed.text == "hi 决策通过"
    assert parsed.message_type == "text"
    assert parsed.root_message_id == "om_root"
    assert parsed.parent_message_id == "om_parent"
    assert parsed.occurred_at is not None
    assert parsed.sender_display_name == "bcc"
    assert parsed.mentions[0]["open_id"] == "ou_bot"
    assert parsed.bot_identity_ready is True
    assert parsed.is_at_bot is True
    assert parsed.is_conversational is True


def test_parse_message_event_preserves_post_text_and_metadata():
    from feishu import events

    events.set_self_identity(open_id="ou_bot", name="包工头")
    post_content = {
        "title": "Spec decision",
        "content": [
            [
                {"tag": "text", "text": "同意方案 A"},
                {"tag": "a", "text": "文档", "href": "https://example.com/spec"},
            ]
        ],
    }

    parsed = events.parse_message_event(_message_body(message_type="post", content=post_content))

    assert parsed is not None
    assert parsed.message_type == "post"
    assert "Spec decision" in parsed.text
    assert "同意方案 A" in parsed.text
    assert "文档" in parsed.text
    assert parsed.is_conversational is True
    assert parsed.content_metadata["links"] == [
        {"text": "文档", "href": "https://example.com/spec"}
    ]


def test_parse_message_event_preserves_file_metadata_without_binary_handles():
    from feishu import events

    events.set_self_identity(open_id="ou_bot", name="包工头")
    file_content = {
        "file_name": "方案.pdf",
        "file_key": "file_v2_secret_handle",
        "size": 1200,
    }

    parsed = events.parse_message_event(_message_body(message_type="file", content=file_content))

    assert parsed is not None
    assert parsed.message_type == "file"
    assert "方案.pdf" in parsed.text
    assert parsed.content_metadata == {"file_name": "方案.pdf", "size": 1200}
    assert "file_key" not in parsed.content_metadata
    assert parsed.is_conversational is False


def test_parse_message_event_preserves_link_metadata_for_ingest_without_conversation():
    from feishu import events

    events.set_self_identity(open_id="ou_bot", name="包工头")
    link_content = {
        "title": "vibelive 技术方案",
        "url": "https://docs.example.com/vibelive",
    }

    parsed = events.parse_message_event(_message_body(message_type="link", content=link_content))

    assert parsed is not None
    assert parsed.message_type == "link"
    assert parsed.text == "Shared link: vibelive 技术方案"
    assert parsed.content_metadata == {
        "title": "vibelive 技术方案",
        "url": "https://docs.example.com/vibelive",
    }
    assert parsed.is_conversational is False


def test_parse_message_event_link_without_title_uses_url_without_metadata_placeholder():
    from feishu import events

    events.set_self_identity(open_id="ou_bot", name="包工头")
    link_content = {"url": "https://docs.example.com/vibelive"}

    parsed = events.parse_message_event(_message_body(message_type="link", content=link_content))

    assert parsed is not None
    assert parsed.text == "Shared link: https://docs.example.com/vibelive"
    assert parsed.content_metadata == {"url": "https://docs.example.com/vibelive"}
    assert parsed.is_conversational is False


def test_group_message_without_self_open_id_is_not_identity_ready():
    from feishu import events

    events.set_self_identity(open_id=None, name="包工头")

    parsed = events.parse_message_event(_message_body(content={"text": "hi @_user_1"}))

    assert parsed is not None
    assert parsed.bot_identity_ready is False
    assert parsed.is_at_bot is False


def test_parse_message_mutation_event_for_recall_and_edit():
    from feishu import events

    recall_body = {
        "header": {"event_id": "evt_recall", "event_type": "im.message.recalled_v1"},
        "event": {
            "message": {
                "message_id": "om_recalled",
                "chat_id": "oc_1",
                "chat_type": "group",
                "create_time": "1778126400000",
            }
        },
    }
    edit_body = _message_body(content={"text": "edited password=abc123"})
    edit_body["header"]["event_type"] = "im.message.updated_v1"
    edit_body["header"]["event_id"] = "evt_edit"

    recall = events.parse_message_mutation_event(recall_body)
    edit = events.parse_message_mutation_event(edit_body)

    assert recall is not None
    assert recall.action == "recall"
    assert recall.message_id == "om_recalled"
    assert recall.chat_id == "oc_1"
    assert recall.occurred_at is not None
    assert edit is not None
    assert edit.action == "edit"
    assert edit.message_id == "om_1"
    assert edit.text == "edited password=abc123"


def test_parse_message_event_marks_bot_sender():
    from feishu import events

    events.set_self_identity(open_id="ou_bot", name="包工头")
    body = _message_body(content={"text": "bot loopback"}, mentions=[])
    body["event"]["sender"]["sender_id"]["open_id"] = "ou_bot"

    parsed = events.parse_message_event(body)

    assert parsed is not None
    assert parsed.sender_is_bot is True


@pytest.mark.anyio
async def test_store_message_if_enabled_redacts_and_inserts(monkeypatch):
    from chat_memory import ingest
    from db import queries
    from feishu.events import ParsedMessageEvent

    inserted: list[dict] = []
    monkeypatch.setattr(queries, "chat_memory_enabled_state", lambda chat_id: chat_id == "oc_1")
    monkeypatch.setattr(queries, "insert_chat_message", lambda row: inserted.append(row) or row)

    stored = await ingest.store_message_if_enabled(
        ParsedMessageEvent(
            event_id="evt-store",
            chat_id="oc_1",
            chat_type="group",
            sender_open_id="ou_sender",
            sender_chat_member_id="ocm_1",
            message_id="om_store",
            parent_message_id="",
            text="prod password=abc123 [asker]",
            is_at_bot=False,
            occurred_at="2026-05-07T10:00:00+00:00",
            sender_display_name="bcc",
            root_message_id="",
            mentions=[],
            message_type="text",
            content_metadata={},
        )
    )

    assert stored is True
    assert inserted[0]["feishu_message_id"] == "om_store"
    assert inserted[0]["chat_id"] == "oc_1"
    assert inserted[0]["sender_display_name"] == "bcc"
    assert inserted[0]["text_redacted"] == "prod password=[REDACTED] [chat_memory_escaped_marker:asker]"
    assert inserted[0]["redacted_payload"]["redaction_count"] >= 2
    assert inserted[0]["redacted_payload"]["redaction_categories"]["assignment"] == 1
    assert inserted[0]["redacted_payload"]["redaction_categories"]["host_marker"] == 1


@pytest.mark.anyio
async def test_store_message_if_enabled_keeps_file_text_empty_and_uses_metadata(monkeypatch):
    from chat_memory import ingest
    from db import queries
    from feishu.events import ParsedMessageEvent

    inserted: list[dict] = []
    monkeypatch.setattr(queries, "chat_memory_enabled_state", lambda chat_id: True)
    monkeypatch.setattr(queries, "insert_chat_message", lambda row: inserted.append(row) or row)

    stored = await ingest.store_message_if_enabled(
        ParsedMessageEvent(
            event_id="evt-file",
            chat_id="oc_1",
            chat_type="group",
            sender_open_id="ou_sender",
            sender_chat_member_id=None,
            message_id="om_file",
            parent_message_id="",
            text="Shared file: 方案.pdf",
            is_at_bot=False,
            message_type="file",
            content_metadata={"file_name": "方案.pdf"},
            occurred_at="2026-05-07T10:00:00+00:00",
        )
    )

    assert stored is True
    assert inserted[0]["text_redacted"] == ""
    assert inserted[0]["redacted_payload"]["redaction_count"] == 0
    assert inserted[0]["redacted_payload"]["content_metadata"] == {"file_name": "方案.pdf"}


@pytest.mark.anyio
async def test_store_message_if_enabled_redacts_content_metadata(monkeypatch):
    from chat_memory import ingest
    from db import queries
    from feishu.events import ParsedMessageEvent

    inserted: list[dict] = []
    monkeypatch.setattr(queries, "chat_memory_enabled_state", lambda chat_id: True)
    monkeypatch.setattr(queries, "insert_chat_message", lambda row: inserted.append(row) or row)

    stored = await ingest.store_message_if_enabled(
        ParsedMessageEvent(
            event_id="evt-link-secret",
            chat_id="oc_1",
            chat_type="group",
            sender_open_id="ou_sender",
            sender_chat_member_id=None,
            message_id="om_link_secret",
            parent_message_id="",
            text="Shared link",
            is_at_bot=False,
            message_type="link",
            content_metadata={
                "title": "deploy hook",
                "url": "https://open.feishu.cn/open-apis/bot/v2/hook/secret-token",
            },
            occurred_at="2026-05-07T10:00:00+00:00",
        )
    )

    assert stored is True
    assert inserted[0]["content_metadata"]["url"] == "[REDACTED]"
    assert inserted[0]["redacted_payload"]["content_metadata"]["url"] == "[REDACTED]"
    assert inserted[0]["redacted_payload"]["redaction_categories"]["token"] == 1
    assert inserted[0]["redacted_payload"]["metadata_redaction_count"] == 1


@pytest.mark.anyio
async def test_store_message_if_enabled_skips_disabled_chat(monkeypatch):
    from chat_memory import ingest
    from db import queries
    from feishu.events import ParsedMessageEvent

    monkeypatch.setattr(queries, "chat_memory_enabled_state", lambda chat_id: False)
    monkeypatch.setattr(queries, "insert_chat_message", lambda row: (_ for _ in ()).throw(AssertionError("must not insert")))

    stored = await ingest.store_message_if_enabled(
        ParsedMessageEvent(
            event_id="evt-skip",
            chat_id="oc_disabled",
            chat_type="group",
            sender_open_id="ou_sender",
            sender_chat_member_id=None,
            message_id="om_skip",
            parent_message_id="",
            text="hello",
            is_at_bot=False,
        )
    )

    assert stored is False
    assert ingest.memory_enabled_hint("oc_disabled") is False


@pytest.mark.anyio
async def test_store_message_if_enabled_rejects_unconfigured_chat_and_caches_false(monkeypatch):
    from chat_memory import ingest
    from db import queries
    from feishu.events import ParsedMessageEvent

    monkeypatch.setattr(queries, "chat_memory_enabled_state", lambda chat_id: None)
    monkeypatch.setattr(queries, "insert_chat_message", lambda row: (_ for _ in ()).throw(AssertionError("must not insert")))

    stored = await ingest.store_message_if_enabled(
        ParsedMessageEvent(
            event_id="evt-unconfigured",
            chat_id="oc_unconfigured",
            chat_type="group",
            sender_open_id="ou_sender",
            sender_chat_member_id=None,
            message_id="om_unconfigured",
            parent_message_id="",
            text="hello",
            is_at_bot=False,
        )
    )

    assert stored is False
    assert ingest.should_schedule_storage("oc_unconfigured") is False


@pytest.mark.anyio
async def test_store_message_if_enabled_rejects_missing_occurred_at(monkeypatch):
    from chat_memory import ingest
    from db import queries
    from feishu.events import ParsedMessageEvent

    monkeypatch.setattr(queries, "chat_memory_enabled_state", lambda chat_id: True)
    monkeypatch.setattr(queries, "insert_chat_message", lambda row: (_ for _ in ()).throw(AssertionError("must not insert")))

    stored = await ingest.store_message_if_enabled(
        ParsedMessageEvent(
            event_id="evt-no-time",
            chat_id="oc_1",
            chat_type="group",
            sender_open_id="ou_sender",
            sender_chat_member_id=None,
            message_id="om_no_time",
            parent_message_id="",
            text="hello",
            is_at_bot=False,
        )
    )

    assert stored is False


@pytest.mark.anyio
async def test_store_message_if_enabled_skips_bot_sender(monkeypatch):
    from chat_memory import ingest
    from db import queries
    from feishu.events import ParsedMessageEvent

    monkeypatch.setattr(queries, "chat_memory_enabled_state", lambda chat_id: True)
    monkeypatch.setattr(queries, "insert_chat_message", lambda row: (_ for _ in ()).throw(AssertionError("must not insert")))

    stored = await ingest.store_message_if_enabled(
        ParsedMessageEvent(
            event_id="evt-bot",
            chat_id="oc_1",
            chat_type="group",
            sender_open_id="ou_bot",
            sender_chat_member_id=None,
            message_id="om_bot",
            parent_message_id="",
            text="bot message",
            is_at_bot=False,
            sender_is_bot=True,
        )
    )

    assert stored is False


@pytest.mark.anyio
async def test_apply_message_mutation_handles_recall_and_edit(monkeypatch):
    from chat_memory import ingest
    from db import queries
    from feishu.events import ParsedMessageMutationEvent

    recalled: list[str] = []
    edited: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(queries, "mark_chat_message_deleted", lambda message_id: recalled.append(message_id) or True)
    monkeypatch.setattr(
        queries,
        "update_chat_message_text",
        lambda message_id, *, text_redacted, redacted_payload, edited_at: edited.append(
            (message_id, text_redacted, redacted_payload)
        )
        or True,
    )

    await ingest.apply_message_mutation(
        ParsedMessageMutationEvent(
            event_id="evt_recall",
            chat_id="oc_1",
            chat_type="group",
            message_id="om_1",
            action="recall",
            occurred_at="2026-05-07T10:00:00+00:00",
        )
    )
    await ingest.apply_message_mutation(
        ParsedMessageMutationEvent(
            event_id="evt_edit",
            chat_id="oc_1",
            chat_type="group",
            message_id="om_2",
            action="edit",
            text="new token=abc123",
            occurred_at="2026-05-07T10:01:00+00:00",
        )
    )

    assert recalled == ["om_1"]
    assert edited[0][0] == "om_2"
    assert edited[0][1] == "new token=[REDACTED]"
    assert edited[0][2]["redaction_count"] == 1
    assert edited[0][2]["redaction_categories"]["assignment"] == 1


class _FakeRequest:
    def __init__(self, body: dict) -> None:
        self._body = body

    async def json(self):
        return self._body


def _chat_memory_tool(ctx, name: str):
    from agent.tools_chat_memory import build_chat_memory_tools

    return next(t for t in build_chat_memory_tools(ctx) if t.name == name).handler


def test_chat_memory_tools_registered_in_meta_and_runner_prompt():
    from agent import runner
    from agent.request_context import RequestContext
    from agent.tools_meta import build_meta_tools

    names = {tool_def.name for tool_def in build_meta_tools(RequestContext())}

    assert {
        "enable_chat_memory",
        "disable_chat_memory",
        "chat_memory_status",
        "get_recent_chat_messages",
        "search_chat_messages_with_context",
    } <= names
    assert "mcp__pmo_meta__enable_chat_memory" in runner.SYSTEM_PROMPT or "enable_chat_memory" in runner.SYSTEM_PROMPT
    assert "search_chat_messages_with_context" in runner.SYSTEM_PROMPT
    assert "开始记录这个群" in runner.SYSTEM_PROMPT
    assert "public_notice" in runner.SYSTEM_PROMPT
    assert "原样" in runner.SYSTEM_PROMPT


@pytest.mark.anyio
async def test_enable_chat_memory_rejects_non_group_context():
    from agent.request_context import RequestContext

    result = await _chat_memory_tool(RequestContext(chat_type="p2p", chat_id="ou_chat"), "enable_chat_memory")({})

    assert result.get("isError") is True
    assert "群聊" in content_payload(result)["error"]


@pytest.mark.anyio
async def test_enable_chat_memory_uses_current_chat_and_returns_notice(monkeypatch):
    from agent.request_context import RequestContext
    from chat_memory import ingest
    from db import queries

    calls: list[tuple[str, str | None, str | None, int]] = []
    invalidated: list[str] = []
    monkeypatch.setattr(
        queries,
        "enable_chat_memory",
        lambda chat_id, *, user_id, open_id, retention_days: calls.append(
            (chat_id, user_id, open_id, retention_days)
        )
        or {
            "chat_id": chat_id,
            "enabled": True,
            "retention_days": retention_days,
            "enabled_at": "2026-05-07T10:00:00+00:00",
        },
    )
    monkeypatch.setattr(ingest, "invalidate_chat_memory_cache", lambda chat_id=None: invalidated.append(chat_id))

    ctx = RequestContext(chat_type="group", chat_id="oc_current", sender_open_id="ou_actor", asker_user_id="profile-1")
    result = await _chat_memory_tool(ctx, "enable_chat_memory")({"retention_days": 120, "chat_id": "oc_attacker"})
    payload = content_payload(result)

    assert calls == [("oc_current", "profile-1", "ou_actor", 120)]
    assert invalidated == ["oc_current"]
    assert payload["status"] == "enabled"
    assert payload["chat_id"] == "oc_current"
    assert "任何群成员" in payload["public_notice"]


@pytest.mark.anyio
async def test_enable_chat_memory_defaults_retention_to_90(monkeypatch):
    from agent.request_context import RequestContext
    from chat_memory import ingest
    from db import queries

    calls: list[int] = []
    monkeypatch.setattr(
        queries,
        "enable_chat_memory",
        lambda chat_id, *, user_id, open_id, retention_days: calls.append(retention_days)
        or {"chat_id": chat_id, "enabled": True, "retention_days": retention_days},
    )
    monkeypatch.setattr(ingest, "invalidate_chat_memory_cache", lambda chat_id=None: None)

    ctx = RequestContext(chat_type="group", chat_id="oc_current", sender_open_id="ou_actor", asker_user_id="profile-1")
    result = await _chat_memory_tool(ctx, "enable_chat_memory")({})
    payload = content_payload(result)

    assert calls == [90]
    assert payload["settings"]["retention_days"] == 90


@pytest.mark.anyio
async def test_disable_chat_memory_uses_current_chat_for_any_member(monkeypatch):
    from agent.request_context import RequestContext
    from chat_memory import ingest
    from db import queries

    calls: list[tuple[str, str | None, str | None]] = []
    invalidated: list[str] = []
    monkeypatch.setattr(
        queries,
        "disable_chat_memory",
        lambda chat_id, *, user_id, open_id: calls.append((chat_id, user_id, open_id))
        or {"chat_id": chat_id, "enabled": False},
    )
    monkeypatch.setattr(ingest, "invalidate_chat_memory_cache", lambda chat_id=None: invalidated.append(chat_id))

    ctx = RequestContext(chat_type="group", chat_id="oc_current", sender_open_id="ou_anyone", asker_user_id=None)
    result = await _chat_memory_tool(ctx, "disable_chat_memory")({"chat_id": "oc_attacker"})
    payload = content_payload(result)

    assert calls == [("oc_current", None, "ou_anyone")]
    assert invalidated == ["oc_current"]
    assert payload["status"] == "disabled"


@pytest.mark.anyio
async def test_chat_memory_status_returns_structured_fields(monkeypatch):
    from agent.request_context import RequestContext
    from db import queries

    monkeypatch.setattr(
        queries,
        "chat_memory_status",
        lambda chat_id: {
            "chat_id": chat_id,
            "enabled": True,
            "enabled_at": "2026-05-07T10:00:00+00:00",
            "enabled_by_user_id": "profile-1",
            "enabled_by_open_id": "ou_actor",
            "retention_days": 90,
            "observer_enabled": True,
        },
    )

    ctx = RequestContext(chat_type="group", chat_id="oc_current")
    result = await _chat_memory_tool(ctx, "chat_memory_status")({"chat_id": "oc_attacker"})
    payload = content_payload(result)

    assert payload == {
        "enabled": True,
        "enabled_at": "2026-05-07T10:00:00+00:00",
        "enabled_by": {"user_id": "profile-1", "open_id": "ou_actor"},
        "retention_days": 90,
        "observer_enabled": True,
        "chat_id": "oc_current",
    }


@pytest.mark.anyio
async def test_get_recent_chat_messages_tool_rejects_chat_id_and_uses_current_chat(monkeypatch):
    from agent.request_context import RequestContext
    from db import queries

    calls: list[tuple[str, int]] = []
    monkeypatch.setattr(queries, "chat_memory_status", lambda chat_id: {"enabled": True})
    monkeypatch.setattr(
        queries,
        "get_recent_chat_messages",
        lambda chat_id, **kwargs: calls.append((chat_id, kwargs["limit"]))
        or [{"message_id": "om_1", "text": "当前群消息", "sent_at": "2026-05-07T10:00:00+08:00"}],
    )

    ctx = RequestContext(chat_type="group", chat_id="oc_current")
    rejected = await _chat_memory_tool(ctx, "get_recent_chat_messages")({"chat_id": "oc_other"})
    result = await _chat_memory_tool(ctx, "get_recent_chat_messages")({"limit": 999})
    payload = content_payload(result)

    assert rejected.get("isError") is True
    assert "chat_id" in content_payload(rejected)["error"]
    assert calls == [("oc_current", 120)]
    assert payload["messages"][0]["message_id"] == "om_1"


@pytest.mark.anyio
async def test_search_chat_messages_with_context_tool_handles_disabled_and_search(monkeypatch):
    from agent.request_context import RequestContext
    from db import queries

    calls: list[dict] = []

    monkeypatch.setattr(queries, "chat_memory_status", lambda chat_id: {"enabled": chat_id == "oc_current"})
    monkeypatch.setattr(
        queries,
        "search_chat_messages_with_context",
        lambda chat_id, **kwargs: calls.append({"chat_id": chat_id, **kwargs})
        or [
            {
                "hit": {"message_id": "om_2", "text": "vibelive 决策"},
                "context": [{"message_id": "om_1", "text": "上一句"}, {"message_id": "om_2", "text": "vibelive 决策"}],
            }
        ],
    )

    disabled = await _chat_memory_tool(RequestContext(chat_type="group", chat_id="oc_disabled"), "search_chat_messages_with_context")({"query": "vibelive"})
    ctx = RequestContext(chat_type="group", chat_id="oc_current")
    result = await _chat_memory_tool(ctx, "search_chat_messages_with_context")(
        {"query": "vibelive", "before": 99, "after": 99, "limit": 99}
    )
    rejected = await _chat_memory_tool(ctx, "search_chat_messages_with_context")({"query": "vibelive", "chat_id": "oc_other"})
    payload = content_payload(result)

    assert disabled.get("isError") is True
    assert "未开启" in content_payload(disabled)["error"]
    assert rejected.get("isError") is True
    assert calls == [
        {
            "chat_id": "oc_current",
            "query": "vibelive",
            "anchor_message_id": None,
            "since": None,
            "until": None,
            "limit": 8,
            "before": 20,
            "after": 20,
        }
    ]
    assert payload["hits"][0]["hit"]["message_id"] == "om_2"


@pytest.mark.anyio
async def test_feishu_webhook_stores_non_at_group_message_without_agent(monkeypatch):
    import app as bot_app
    from feishu import events

    events.set_self_identity(open_id="ou_bot", name="包工头")
    body = _message_body(content={"text": "普通群消息"}, mentions=[])
    body["header"]["event_id"] = "evt_chat_memory_store"
    stored: list[str] = []

    monkeypatch.setattr(bot_app.chat_memory_ingest, "memory_enabled_hint", lambda chat_id: False)
    monkeypatch.setattr(bot_app.chat_memory_ingest, "should_schedule_storage", lambda chat_id: True)

    async def store(parsed):
        stored.append(parsed.message_id)
        return True

    async def fail_handle(parsed):
        raise AssertionError("non-at group messages must not enter the agent")

    monkeypatch.setattr(bot_app.chat_memory_ingest, "store_message_if_enabled", store)
    monkeypatch.setattr(bot_app, "_handle_message", fail_handle)

    response = await bot_app.feishu_webhook(_FakeRequest(body))
    await asyncio.sleep(0)

    assert response.body == b"ok"
    assert stored == ["om_1"]


@pytest.mark.anyio
async def test_feishu_webhook_disabled_non_at_group_does_not_schedule_storage(monkeypatch):
    import app as bot_app
    from feishu import events

    events.set_self_identity(open_id="ou_bot", name="包工头")
    body = _message_body(content={"text": "普通群消息"}, mentions=[])
    body["header"]["event_id"] = "evt_chat_memory_disabled"

    monkeypatch.setattr(bot_app.chat_memory_ingest, "memory_enabled_hint", lambda chat_id: False)
    monkeypatch.setattr(bot_app.chat_memory_ingest, "should_schedule_storage", lambda chat_id: False)
    monkeypatch.setattr(
        bot_app.chat_memory_ingest,
        "store_message_if_enabled",
        lambda parsed: (_ for _ in ()).throw(AssertionError("must not store")),
    )
    monkeypatch.setattr(
        bot_app,
        "_handle_message",
        lambda parsed: (_ for _ in ()).throw(AssertionError("must not answer")),
    )

    response = await bot_app.feishu_webhook(_FakeRequest(body))

    assert response.body == b"group not addressed"


@pytest.mark.anyio
async def test_feishu_webhook_at_group_message_stores_and_answers(monkeypatch):
    import app as bot_app
    from feishu import events

    events.set_self_identity(open_id="ou_bot", name="包工头")
    body = _message_body(content={"text": "hi @_user_1"}, mentions=[{"id": {"open_id": "ou_bot"}, "name": "包工头"}])
    body["header"]["event_id"] = "evt_chat_memory_at"
    stored: list[str] = []
    handled: list[str] = []

    monkeypatch.setattr(bot_app.chat_memory_ingest, "memory_enabled_hint", lambda chat_id: True)
    monkeypatch.setattr(bot_app.chat_memory_ingest, "should_schedule_storage", lambda chat_id: True)

    async def store(parsed):
        stored.append(parsed.message_id)
        return True

    async def handle(parsed):
        handled.append(parsed.message_id)

    monkeypatch.setattr(bot_app.chat_memory_ingest, "store_message_if_enabled", store)
    monkeypatch.setattr(bot_app, "_handle_message", handle)

    response = await bot_app.feishu_webhook(_FakeRequest(body))
    await asyncio.sleep(0)

    assert response.body == b"ok"
    assert stored == ["om_1"]
    assert handled == ["om_1"]


@pytest.mark.anyio
async def test_feishu_webhook_at_group_non_conversational_message_only_stores(monkeypatch):
    import app as bot_app
    from feishu import events

    events.set_self_identity(open_id="ou_bot", name="包工头")
    body = _message_body(
        message_type="file",
        content={"file_name": "vibelive-spec.pdf"},
        mentions=[{"id": {"open_id": "ou_bot"}, "name": "包工头"}],
    )
    body["header"]["event_id"] = "evt_chat_memory_at_file"
    stored: list[str] = []

    monkeypatch.setattr(bot_app.chat_memory_ingest, "should_schedule_storage", lambda chat_id: True)

    async def store(parsed):
        stored.append(parsed.message_id)
        return True

    async def fail_handle(parsed):
        raise AssertionError("non-conversational messages must not enter the agent")

    monkeypatch.setattr(bot_app.chat_memory_ingest, "store_message_if_enabled", store)
    monkeypatch.setattr(bot_app, "_handle_message", fail_handle)

    response = await bot_app.feishu_webhook(_FakeRequest(body))
    await asyncio.sleep(0)

    assert response.body == b"ok"
    assert stored == ["om_1"]


@pytest.mark.anyio
async def test_feishu_webhook_duplicate_event_does_not_schedule_storage(monkeypatch):
    import app as bot_app
    from feishu import events

    events.set_self_identity(open_id="ou_bot", name="包工头")
    body = _message_body(content={"text": "普通群消息"}, mentions=[])
    body["header"]["event_id"] = "evt_chat_memory_duplicate"
    stored: list[str] = []

    monkeypatch.setattr(bot_app.chat_memory_ingest, "memory_enabled_hint", lambda chat_id: True)
    monkeypatch.setattr(bot_app.chat_memory_ingest, "should_schedule_storage", lambda chat_id: True)

    async def store(parsed):
        stored.append(parsed.message_id)
        return True

    monkeypatch.setattr(bot_app.chat_memory_ingest, "store_message_if_enabled", store)

    first = await bot_app.feishu_webhook(_FakeRequest(body))
    await asyncio.sleep(0)
    second = await bot_app.feishu_webhook(_FakeRequest(body))
    await asyncio.sleep(0)

    assert first.body == b"ok"
    assert second.body == b"duplicate"
    assert stored == ["om_1"]


@pytest.mark.anyio
async def test_feishu_webhook_not_ready_group_message_does_not_store_or_answer(monkeypatch):
    import app as bot_app
    from feishu import events

    events.set_self_identity(open_id=None, name="包工头")
    body = _message_body(content={"text": "hi @_user_1"})
    body["header"]["event_id"] = "evt_chat_memory_not_ready"

    monkeypatch.setattr(
        bot_app.chat_memory_ingest,
        "store_message_if_enabled",
        lambda parsed: (_ for _ in ()).throw(AssertionError("must not store")),
    )
    monkeypatch.setattr(
        bot_app,
        "_handle_message",
        lambda parsed: (_ for _ in ()).throw(AssertionError("must not answer")),
    )

    response = await bot_app.feishu_webhook(_FakeRequest(body))

    assert response.body == b"not ready"


@pytest.mark.anyio
async def test_feishu_webhook_applies_message_mutation_without_agent(monkeypatch):
    import app as bot_app

    body = {
        "header": {"event_id": "evt_webhook_recall", "event_type": "im.message.recalled_v1"},
        "event": {
            "message": {
                "message_id": "om_recalled",
                "chat_id": "oc_1",
                "chat_type": "group",
            }
        },
    }
    applied: list[str] = []

    async def apply(mutation):
        applied.append(mutation.message_id)
        return True

    monkeypatch.setattr(bot_app.chat_memory_ingest, "apply_message_mutation", apply)
    monkeypatch.setattr(
        bot_app,
        "_handle_message",
        lambda parsed: (_ for _ in ()).throw(AssertionError("mutation events must not enter agent")),
    )

    response = await bot_app.feishu_webhook(_FakeRequest(body))

    assert response.body == b"ok"
    assert applied == ["om_recalled"]


@pytest.mark.anyio
async def test_feishu_webhook_message_mutation_failure_returns_500(monkeypatch):
    import app as bot_app

    body = {
        "header": {"event_id": "evt_webhook_recall_fail", "event_type": "im.message.recalled_v1"},
        "event": {
            "message": {
                "message_id": "om_recalled",
                "chat_id": "oc_1",
                "chat_type": "group",
            }
        },
    }

    async def fail_apply(mutation):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(bot_app.chat_memory_ingest, "apply_message_mutation", fail_apply)
    monkeypatch.setattr(
        bot_app,
        "_handle_message",
        lambda parsed: (_ for _ in ()).throw(AssertionError("mutation events must not enter agent")),
    )

    response = await bot_app.feishu_webhook(_FakeRequest(body))

    assert response.status_code == 500
    assert response.body == b"mutation error"
