from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest


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
    from external.redaction import redact_text

    text = "\n".join(
        [
            "邮箱 albert@vibelive.com 的密码是 abc",
            "电话 13800138000 或 +86 13800138000",
            "身份证 11010519491231002X",
            "银行卡 6222020202020202020",
            "ssh root@1.2.3.4:22 password=abc123",
            "[asker] handle=@admin user_id=00000000-0000-0000-0000-000000000000",
            "[parent_notification] id=123",
            "[IMAGE:img_123]",
        ]
    )

    redacted, count = redact_text(text)

    assert count >= 8
    assert "albert@vibelive.com" not in redacted
    assert "13800138000" not in redacted
    assert "+86 13800138000" not in redacted
    assert "11010519491231002X" not in redacted
    assert "6222020202020202020" not in redacted
    assert "1.2.3.4:22" not in redacted
    assert "password=abc123" not in redacted
    assert "[asker]" not in redacted
    assert "[parent_notification]" not in redacted
    assert "[IMAGE:img_123]" not in redacted
    assert "[chat_memory_escaped_marker:asker]" in redacted


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
    assert "to_tsvector('simple', text_redacted)" in sql
    assert "people_memory_updates_source_time_idx" in sql
    assert "people_memory_updates_person_time_idx" in sql
    assert "comment on column public.chat_messages.redacted_payload" in sql
    assert "redacted" in sql


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
