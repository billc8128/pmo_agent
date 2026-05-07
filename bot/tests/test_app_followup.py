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
async def test_handle_message_times_out_hanging_streaming_agent(monkeypatch):
    fake_feishu = _FakeFeishuClient()
    shutdown_keys: list[str] = []
    async def shutdown_conversation(key: str) -> None:
        shutdown_keys.append(key)
    monkeypatch.setattr(bot_app, "feishu_client", fake_feishu)
    monkeypatch.setattr(bot_app.agent_runner, "answer_streaming", _hanging_answer_streaming)
    monkeypatch.setattr(bot_app.agent_runner, "shutdown_conversation", shutdown_conversation, raising=False)
    monkeypatch.setattr(bot_app.db_queries, "lookup_by_feishu_open_id", lambda open_id: None)
    monkeypatch.setattr(bot_app.settings, "agent_max_duration_seconds", 0.01)

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
    assert "超时" in str(fake_feishu.patched_cards[-1])
    assert shutdown_keys == ["chat-1:ou-user"]
    assert not any(kind == "post" for kind, _payload in fake_feishu.replies)


@pytest.mark.anyio
async def test_group_send_to_this_chat_subscription_uses_fast_path(monkeypatch):
    fake_feishu = _FakeFeishuClient()
    added: list[dict] = []
    monkeypatch.setattr(bot_app, "feishu_client", fake_feishu)
    monkeypatch.setattr(
        bot_app.db_queries,
        "lookup_by_feishu_open_id",
        lambda open_id: {"user_id": "user-1", "handle": "bcc", "display_name": "bcc"},
    )
    monkeypatch.setattr(
        bot_app.db_queries,
        "add_subscription",
        lambda **kwargs: added.append(kwargs) or {"id": "sub-1", **kwargs},
    )

    async def fail_answer_streaming(*args, **kwargs):
        raise AssertionError("fast-path subscription commands must not enter the agent")
        yield

    monkeypatch.setattr(bot_app.agent_runner, "answer_streaming", fail_answer_streaming)

    event = ParsedMessageEvent(
        event_id="evt-group-sub",
        chat_id="chat-group",
        chat_type="group",
        sender_open_id="ou-user",
        sender_chat_member_id=None,
        message_id="om-sub",
        parent_message_id="",
        text="vibelive的dev分支每次有新push的时候，都总结下该次push的改动和技术方案发到这个群里",
        is_at_bot=True,
    )

    await bot_app._handle_message(event)

    assert len(added) == 1
    assert added[0]["scope_kind"] == "chat"
    assert added[0]["scope_id"] == "chat-group"
    assert added[0]["target_kind"] == "chat"
    assert added[0]["target_id"] == "chat-group"
    assert added[0]["created_by"] == "user-1"
    assert "vibelive" in added[0]["description"]
    assert [kind for kind, _payload in fake_feishu.replies] == ["text"]
    assert "发到这个群" in fake_feishu.replies[0][1]


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
