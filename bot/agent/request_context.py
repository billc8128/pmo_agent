from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RequestContext:
    message_id: str = ""
    chat_id: str = ""
    chat_type: str = ""
    sender_open_id: str = ""
    conversation_key: str = ""
    asker_user_id: str | None = None
    asker_handle: str | None = None
