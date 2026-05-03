from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


def ok(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def err(message: str, **extra: Any) -> dict[str, Any]:
    payload = {"error": message, **extra}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": True,
    }


def content_payload(result: dict[str, Any]) -> dict[str, Any]:
    return json.loads(result["content"][0]["text"])


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def logical_key(*, chat_id: str, sender_open_id: str, action_type: str, args: dict[str, Any]) -> str:
    from agent.canonical_args import canonicalize_args

    digest = hashlib.sha256(stable_json(canonicalize_args(action_type, args)).encode("utf-8")).hexdigest()
    return f"{chat_id}:{sender_open_id}:{action_type}:{digest}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
