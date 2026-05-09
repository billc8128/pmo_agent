from __future__ import annotations

import asyncio

import pytest

import app as bot_app
from feishu.events import ParsedMessageEvent


class _FakeFeishuClient:
    def __init__(self) -> None:
        self.patched_cards: list[dict] = []
        self.replies: list[tuple[str, object]] = []

    async def add_reaction(self, message_id: str, reaction: str) -> None:
        return None

    async def reply_card(self, parent_message_id: str, card: dict) -> str:
        self.replies.append(("card", card))
        return "card-1"

    async def patch_card(self, message_id: str, card: dict) -> bool:
        self.patched_cards.append(card)
        return True

    async def reply_post(self, parent_message_id: str, post_content: dict) -> str:
        self.replies.append(("post", post_content))
        return "post-1"

    async def reply_text(self, parent_message_id: str, text: str) -> str:
        self.replies.append(("text", text))
        return "text-1"

    async def reply_image(self, parent_message_id: str, image_key: str) -> str:
        self.replies.append(("image", image_key))
        return "image-1"


async def _hanging_answer_streaming(*args, **kwargs):
    yield {"kind": "tool", "name": "Read", "args_hint": "file_path=spec.md"}
    await asyncio.Event().wait()


@pytest.mark.anyio
async def test_handle_message_idle_times_out_hanging_streaming_agent(monkeypatch):
    fake_feishu = _FakeFeishuClient()
    shutdown_keys: list[str] = []
    async def shutdown_conversation(key: str) -> None:
        shutdown_keys.append(key)
    monkeypatch.setattr(bot_app, "feishu_client", fake_feishu)
    monkeypatch.setattr(bot_app.agent_runner, "answer_streaming", _hanging_answer_streaming)
    monkeypatch.setattr(bot_app.agent_runner, "shutdown_conversation", shutdown_conversation, raising=False)
    monkeypatch.setattr(bot_app.db_queries, "lookup_by_feishu_open_id", lambda open_id: None)
    monkeypatch.setattr(bot_app.settings, "agent_idle_timeout_seconds", 0.01)
    monkeypatch.setattr(bot_app.settings, "agent_max_wall_seconds", 10)

    event = ParsedMessageEvent(
        event_id="evt-1",
        chat_id="chat-1",
        chat_type="p2p",
        sender_open_id="ou-user",
        sender_chat_member_id=None,
        message_id="om-user",
        parent_message_id="",
        text="这次改动大不大",
        is_at_bot=False,
    )

    await asyncio.wait_for(bot_app._handle_message(event), timeout=1.0)

    assert fake_feishu.patched_cards
    assert "没有进展" in str(fake_feishu.patched_cards[-1])
    assert shutdown_keys == ["chat-1:ou-user"]
    assert not any(kind == "post" for kind, _payload in fake_feishu.replies)


async def _slow_but_progressing_answer_streaming(*args, **kwargs):
    yield {"kind": "tool", "name": "get_project_overview", "args_hint": "handle=bcc"}
    await asyncio.sleep(0.01)
    yield {"kind": "status", "phase": "stream", "event_type": "content_block_delta"}
    await asyncio.sleep(0.01)
    yield {"kind": "final", "text": "持续有进展，所以不应被 idle watchdog 中断。"}


@pytest.mark.anyio
async def test_handle_message_allows_slow_agent_when_events_keep_arriving(monkeypatch):
    fake_feishu = _FakeFeishuClient()
    shutdown_keys: list[str] = []

    async def shutdown_conversation(key: str) -> None:
        shutdown_keys.append(key)

    monkeypatch.setattr(bot_app, "feishu_client", fake_feishu)
    monkeypatch.setattr(bot_app.agent_runner, "answer_streaming", _slow_but_progressing_answer_streaming)
    monkeypatch.setattr(bot_app.agent_runner, "shutdown_conversation", shutdown_conversation, raising=False)
    monkeypatch.setattr(bot_app.db_queries, "lookup_by_feishu_open_id", lambda open_id: None)
    monkeypatch.setattr(bot_app.settings, "agent_idle_timeout_seconds", 0.05)
    monkeypatch.setattr(bot_app.settings, "agent_max_wall_seconds", 1)

    event = ParsedMessageEvent(
        event_id="evt-3",
        chat_id="chat-1",
        chat_type="p2p",
        sender_open_id="ou-user",
        sender_chat_member_id=None,
        message_id="om-user",
        parent_message_id="",
        text="分析一下 bcc 的 coding agent 技巧",
        is_at_bot=False,
    )

    await asyncio.wait_for(bot_app._handle_message(event), timeout=1.0)

    assert shutdown_keys == []
    assert any(kind == "post" for kind, _payload in fake_feishu.replies)
    assert "没有进展" not in str(fake_feishu.patched_cards)


@pytest.mark.anyio
async def test_handle_message_consumes_target_consent_reply(monkeypatch):
    fake_feishu = _FakeFeishuClient()
    granted: list[tuple[str, str]] = []
    resolved: list[tuple[str, str]] = []
    monkeypatch.setattr(bot_app, "feishu_client", fake_feishu)
    monkeypatch.setattr(
        bot_app.db_queries,
        "lookup_by_feishu_open_id",
        lambda open_id: {"user_id": "target-user", "handle": "bcc", "display_name": "bcc"},
    )
    monkeypatch.setattr(
        bot_app.db_queries,
        "pending_target_consent_by_message",
        lambda message_id: {
            "id": "pending-1",
            "source_user_id": "source-user",
            "target_user_id": "target-user",
            "source": {"handle": "alice", "display_name": "Alice"},
        },
    )
    monkeypatch.setattr(bot_app.db_queries, "add_target_consent", lambda target, source: granted.append((target, source)) or {})
    monkeypatch.setattr(bot_app.db_queries, "resolve_pending_target_consent", lambda pending_id, status: resolved.append((pending_id, status)) or {})

    async def fail_answer(*args, **kwargs):
        raise AssertionError("consent replies must not enter the agent")

    monkeypatch.setattr(bot_app.agent_runner, "answer_streaming", fail_answer)

    event = ParsedMessageEvent(
        event_id="evt-2",
        chat_id="chat-1",
        chat_type="p2p",
        sender_open_id="ou-target",
        sender_chat_member_id=None,
        message_id="om-reply",
        parent_message_id="om-consent",
        text="同意",
        is_at_bot=False,
    )

    await bot_app._handle_message(event)

    assert granted == [("target-user", "source-user")]
    assert resolved == [("pending-1", "granted")]
    assert fake_feishu.replies[-1][0] == "text"
    assert "已同意" in fake_feishu.replies[-1][1]
