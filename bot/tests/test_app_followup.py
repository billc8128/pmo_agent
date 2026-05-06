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
    monkeypatch.setattr(bot_app, "feishu_client", fake_feishu)
    monkeypatch.setattr(bot_app.agent_runner, "answer_streaming", _hanging_answer_streaming)
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
    assert not any(kind == "post" for kind, _payload in fake_feishu.replies)
