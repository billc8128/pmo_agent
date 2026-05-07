from __future__ import annotations

from copy import deepcopy
import inspect
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

    def neq(self, column, value):
        self.filters.append(("neq", column, value))
        return self

    def gte(self, column, value):
        self.filters.append(("gte", column, value))
        return self

    def gt(self, column, value):
        self.filters.append(("gt", column, value))
        return self

    def in_(self, column, value):
        self.filters.append(("in", column, set(value or [])))
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

    def delete(self):
        self._op = "delete"
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
            return SimpleNamespace(data=deepcopy(rows))
        if self._op == "delete":
            rows = self._filtered()
            self.client.tables[self.name] = [
                row for row in self.client.tables[self.name] if row not in rows
            ]
            return SimpleNamespace(data=deepcopy(rows))
        rows = self._filtered()
        if self._maybe_single:
            return SimpleNamespace(data=deepcopy(rows[0]) if rows else None)
        return SimpleNamespace(data=deepcopy(rows))

    def _filtered(self):
        rows = list(self.client.tables[self.name])
        for op, column, value in self.filters:
            if op == "eq":
                rows = [row for row in rows if row.get(column) == value]
            elif op == "neq":
                rows = [row for row in rows if row.get(column) != value]
            elif op == "gte":
                rows = [row for row in rows if str(row.get(column) or "") >= str(value)]
            elif op == "gt":
                rows = [row for row in rows if str(row.get(column) or "") > str(value)]
            elif op == "in":
                rows = [row for row in rows if row.get(column) in value]
            elif op == "is" and value is None:
                rows = [row for row in rows if row.get(column) is None]
        for column, desc in reversed(self._orders):
            rows.sort(key=lambda row: str(row.get(column) or ""), reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return rows


def _people_tool(ctx, name: str):
    from agent.tools_people_memory import build_people_memory_tools

    return next(t for t in build_people_memory_tools(ctx) if t.name == name).handler


def test_people_memory_tools_registered_without_raw_writer():
    from agent import runner
    from agent.request_context import RequestContext
    from agent.tools_meta import build_meta_tools

    tool_defs = build_meta_tools(RequestContext())
    names = {tool_def.name for tool_def in tool_defs}
    schemas = {tool_def.name: tool_def.input_schema for tool_def in tool_defs}

    assert "summarize_people_signal" in names
    assert "suggest_people_for_topic" in names
    assert "update_people_memory_note" not in names
    assert "get_people_memory" not in names
    assert "chat_id" not in schemas["summarize_people_signal"]
    assert "chat_id" not in schemas["suggest_people_for_topic"]
    runner_source = inspect.getsource(runner._get_client)
    assert "mcp__pmo_meta__summarize_people_signal" in runner_source
    assert "mcp__pmo_meta__suggest_people_for_topic" in runner_source
    assert "不要逐字暴露 people memory" in runner.SYSTEM_PROMPT


def test_people_memory_for_chat_scopes_to_current_chat(monkeypatch):
    from db import queries

    fake = _FakeSupabase()
    fake.tables["chat_messages"] = [
        {
            "chat_id": "oc_current",
            "sender_open_id": "ou_alice",
            "sender_user_id": "profile-alice",
            "sender_display_name": "Alice",
            "occurred_at": "2026-05-07T10:00:00+00:00",
            "deleted_at": None,
            "sender_is_bot": False,
        },
        {
            "chat_id": "oc_other",
            "sender_open_id": "ou_bob",
            "sender_user_id": None,
            "sender_display_name": "Bob",
            "occurred_at": "2026-05-07T10:01:00+00:00",
            "deleted_at": None,
            "sender_is_bot": False,
        },
    ]
    fake.tables["people_memory"] = [
        {
            "person_key": "profile:profile-alice",
            "profile_id": "profile-alice",
            "feishu_open_id": "ou_alice",
            "display_name": "Alice",
            "handle": "alice",
            "pmo_notes": "Alice 擅长播放器和前端排查。",
            "last_observed_at": "2026-05-07T10:00:00+00:00",
        },
        {
            "person_key": "feishu:ou_bob",
            "profile_id": None,
            "feishu_open_id": "ou_bob",
            "display_name": "Bob",
            "handle": None,
            "pmo_notes": "Bob 擅长后端。",
            "last_observed_at": "2026-05-07T10:01:00+00:00",
        },
    ]
    monkeypatch.setattr(queries, "sb_admin", lambda: fake)

    rows = queries.people_memory_for_chat("oc_current", query="播放器", limit=10)

    assert [row["person_key"] for row in rows] == ["profile:profile-alice"]


@pytest.mark.anyio
async def test_summarize_people_signal_scopes_and_hides_raw_note(monkeypatch):
    from agent.request_context import RequestContext
    from db import queries

    monkeypatch.setattr(queries, "chat_memory_status", lambda chat_id: {"enabled": True})
    monkeypatch.setattr(
        queries,
        "people_memory_for_chat",
        lambda chat_id, **kwargs: [
            {
                "person_key": "profile:alice",
                "display_name": "Alice",
                "handle": "alice",
                "pmo_notes": "Alice 经常处理 vibelive 播放器问题。RAW_PRIVATE_NOTE: 不要逐字输出。",
                "last_observed_at": "2026-05-07T10:00:00+00:00",
            }
        ],
    )

    ctx = RequestContext(chat_type="group", chat_id="oc_current")
    result = await _people_tool(ctx, "summarize_people_signal")(
        {"query": "Alice", "topic": "vibelive 播放器"}
    )
    payload = content_payload(result)

    assert result.get("isError") is not True
    assert payload["count"] == 1
    assert payload["people"][0]["person"] == "Alice / @alice"
    assert "pmo_notes" not in payload["people"][0]
    assert "RAW_PRIVATE_NOTE" not in str(payload)


@pytest.mark.anyio
async def test_suggest_people_for_topic_does_not_cross_chat_and_is_conservative(monkeypatch):
    from agent.request_context import RequestContext
    from db import queries

    monkeypatch.setattr(queries, "chat_memory_status", lambda chat_id: {"enabled": True})
    seen_calls: list[dict] = []

    def fake_people(chat_id, **kwargs):
        seen_calls.append({"chat_id": chat_id, **kwargs})
        return [
            {
                "person_key": "profile:alice",
                "display_name": "Alice",
                "handle": "alice",
                "pmo_notes": "Alice 擅长 vibelive 播放器、Agora RTC、前端调试。",
                "last_observed_at": "2026-05-07T10:00:00+00:00",
            },
            {
                "person_key": "profile:thin",
                "display_name": "Thin",
                "handle": "thin",
                "pmo_notes": "",
                "last_observed_at": "2026-05-07T10:00:00+00:00",
            },
        ]

    monkeypatch.setattr(queries, "people_memory_for_chat", fake_people)

    ctx = RequestContext(chat_type="group", chat_id="oc_current")
    result = await _people_tool(ctx, "suggest_people_for_topic")({"topic": "vibelive 播放器", "limit": 5})
    rejected = await _people_tool(ctx, "suggest_people_for_topic")({"topic": "vibelive", "chat_id": "oc_other"})
    payload = content_payload(result)

    assert rejected.get("isError") is True
    assert seen_calls[0]["chat_id"] == "oc_current"
    assert payload["candidates"][0]["person"] == "Alice / @alice"
    assert payload["candidates"][0]["confidence"] in {"medium", "high"}
    assert all("pmo_notes" not in candidate for candidate in payload["candidates"])
    assert all(candidate["person"] != "Thin / @thin" for candidate in payload["candidates"])


def test_merge_people_memory_identity_moves_feishu_note_to_profile_and_backfills(monkeypatch):
    from chat_memory import identity
    from db import queries

    fake = _FakeSupabase()
    fake.tables["people_memory"] = [
        {
            "person_key": "feishu:ou_alice",
            "profile_id": None,
            "feishu_open_id": "ou_alice",
            "display_name": "Alice",
            "handle": None,
            "pmo_notes": "Alice 经常处理播放器问题。",
            "metadata": {"source": "chat"},
        }
    ]
    fake.tables["chat_messages"] = [
        {"feishu_message_id": "om_1", "sender_open_id": "ou_alice", "sender_user_id": None}
    ]
    monkeypatch.setattr(queries, "sb_admin", lambda: fake)

    merged = identity.merge_people_memory_identity(
        profile_id="profile-alice",
        feishu_open_id="ou_alice",
        display_name="Alice",
        handle="alice",
    )

    assert merged["person_key"] == "profile:profile-alice"
    assert merged["pmo_notes"] == "Alice 经常处理播放器问题。"
    assert [row["person_key"] for row in fake.tables["people_memory"]] == ["profile:profile-alice"]
    assert fake.tables["chat_messages"][0]["sender_user_id"] == "profile-alice"
    assert fake.tables["people_memory_updates"][0]["update_source"] == "identity_merge"


def test_people_loop_updates_notes_once_and_advances_cursor(monkeypatch):
    from chat_memory import people_loop
    from db import queries

    fake = _FakeSupabase()
    fake.tables["chat_memory_settings"] = [
        {
            "chat_id": "oc_current",
            "enabled": True,
            "people_loop_cursor": "2026-05-07T09:00:00+00:00",
        }
    ]
    fake.tables["chat_messages"] = [
        {
            "feishu_message_id": f"om_{i}",
            "chat_id": "oc_current",
            "sender_open_id": "ou_alice",
            "sender_user_id": "profile-alice",
            "sender_display_name": "Alice",
            "text_redacted": f"vibelive 播放器调试消息 {i}",
            "occurred_at": f"2026-05-07T10:{i:02d}:00+00:00",
            "deleted_at": None,
            "sender_is_bot": False,
        }
        for i in range(10)
    ]
    monkeypatch.setattr(queries, "sb_admin", lambda: fake)

    result = people_loop.run_once(max_chats=5, min_messages=5, daily_cap=10)

    assert result["updated_people"] == 1
    assert fake.tables["people_memory"][0]["person_key"] == "profile:profile-alice"
    assert "vibelive 播放器" in fake.tables["people_memory"][0]["pmo_notes"]
    assert fake.tables["people_memory_updates"][0]["update_source"] == "background_loop"
    assert fake.tables["chat_memory_settings"][0]["people_loop_cursor"] == "2026-05-07T10:09:00+00:00"
