from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from config import settings

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    send: bool
    matched_aspect: str
    preview_hint: str | None
    suppressed_by: str | None
    reason: str
    raw_input: dict[str, Any]
    raw_output: dict[str, Any]
    latency_ms: int
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class DecisionParseError(Exception):
    def __init__(
        self,
        message: str,
        *,
        raw_text: str,
        raw_input: dict[str, Any],
        latency_ms: int = 0,
    ) -> None:
        super().__init__(message)
        self.raw_text = raw_text
        self.raw_input = raw_input
        self.latency_ms = latency_ms


@dataclass
class ScopeContext:
    owner_local_time: str
    owner_today_sent_count: int
    recent_notifications: list[dict[str, Any]]
    owner_timezone: str = "Asia/Shanghai"


def _inject_anthropic_env() -> None:
    os.environ["ANTHROPIC_AUTH_TOKEN"] = settings.anthropic_auth_token
    os.environ["ANTHROPIC_BASE_URL"] = settings.anthropic_base_url
    os.environ["ANTHROPIC_MODEL"] = settings.anthropic_model
    os.environ["ANTHROPIC_DEFAULT_OPUS_MODEL"] = settings.anthropic_default_opus_model
    os.environ["ANTHROPIC_DEFAULT_SONNET_MODEL"] = settings.anthropic_default_sonnet_model
    os.environ["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = settings.anthropic_default_haiku_model
    os.environ["API_TIMEOUT_MS"] = settings.api_timeout_ms
    os.environ["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = settings.claude_code_disable_nonessential_traffic


def build_judge_event(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "turn_id": payload.get("turn_id"),
        "agent": payload.get("agent"),
        "project_path": payload.get("project_path"),
        "project_root": payload.get("project_root"),
        "user_message_at": payload.get("user_message_at"),
        "user_message": (payload.get("user_message") or "")[:800],
        "agent_summary": payload.get("agent_summary"),
        "agent_response_excerpt": (payload.get("agent_response_full") or "")[:600] or None,
    }


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def parse_decision_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    match = _JSON_FENCE_RE.search(raw)
    if match:
        raw = match.group(1).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        raw = raw[start:end + 1]
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("judge output must be a JSON object")
    return parsed


_JUDGE_SYSTEM_PROMPT = """你是 pmo_agent 的通知决策器。你只输出 JSON，不输出解释性文字。

给你一条新事件、候选订阅、同一 owner 的 sibling 偏好、最近通知和 owner 当前时间。
判断是否应该给候选订阅发通知。

规则：
1. sibling 里的排除/静音规则优先，例如“项目 X 不要”“凌晨别打扰”“今晚别发”。
2. 5 分钟内同主题的 sent/claimed/pending 通知要去重；同一 event_id 永远不要挡自己。
3. suppressed/mismatch、failed 不占用去重窗口。
4. owner_today_sent_count 很高时可以 daily_cap 抑制，但不要过度保守。
5. agent_summary 为空时可以参考 agent_response_excerpt；仍无法判断就 send=false, suppressed_by="mismatch"。
6. 默认即使 is_subject_the_owner=true 也照常发；只有订阅 description 或 sibling rules 明确说“不要发我自己的/自己的不用提醒”时才 suppress。

输出严格 JSON：
{
  "send": true | false,
  "matched_aspect": "命中的订阅点；没有则空字符串",
  "preview_hint": "给渲染器的一句话重点；不发送则 null",
  "suppressed_by": null | "mismatch" | "duplicate_in_window" | "quiet_hours" | "daily_cap" | "explicit_exclude",
  "reason": "一句可审计的中文原因"
}
"""


def _build_raw_input(
    event: dict[str, Any],
    candidate: dict[str, Any],
    siblings: list[dict[str, Any]],
    scope_ctx: ScopeContext,
) -> dict[str, Any]:
    payload = event.get("payload") or {}
    scope_kind = candidate.get("scope_kind")
    scope_id = candidate.get("scope_id")
    return {
        "candidate": {
            "id": candidate.get("id"),
            "scope_kind": scope_kind,
            "scope_id": scope_id,
            "description": candidate.get("description"),
        },
        "siblings": [
            {
                "id": s.get("id"),
                "scope_kind": s.get("scope_kind"),
                "scope_id": s.get("scope_id"),
                "description": s.get("description"),
            }
            for s in siblings
        ],
        "scope_context": {
            "owner_local_time": scope_ctx.owner_local_time,
            "owner_timezone": scope_ctx.owner_timezone,
            "owner_today_sent_count": scope_ctx.owner_today_sent_count,
            "recent_notifications": scope_ctx.recent_notifications,
        },
        "event": {
            "id": event.get("id"),
            "source": event.get("source"),
            "source_id": event.get("source_id"),
            "occurred_at": event.get("occurred_at"),
            "project_root": event.get("project_root"),
            "user_id": event.get("user_id"),
            "subject_user": event.get("subject_profile") or {},
            "payload_version": event.get("payload_version"),
            "is_subject_the_owner": scope_kind == "user" and str(event.get("user_id") or "") == str(scope_id or ""),
            "payload": build_judge_event(payload),
        },
    }


def _usage_from_result_message(msg: Any) -> tuple[int | None, int | None]:
    usage = getattr(msg, "usage", None)
    if usage is None:
        return None, None
    if isinstance(usage, dict):
        return usage.get("input_tokens"), usage.get("output_tokens")
    return getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None)


async def decide(
    event: dict[str, Any],
    candidate: dict[str, Any],
    siblings: list[dict[str, Any]],
    scope_ctx: ScopeContext,
) -> Decision:
    _inject_anthropic_env()
    raw_input = _build_raw_input(event, candidate, siblings, scope_ctx)
    options = ClaudeAgentOptions(
        system_prompt=_JUDGE_SYSTEM_PROMPT,
        allowed_tools=[],
        mcp_servers={},
        disallowed_tools=[
            "Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Task", "TodoWrite",
        ],
        max_turns=1,
    )
    client = ClaudeSDKClient(options=options)
    started = time.monotonic()
    text_chunks: list[str] = []
    input_tokens: int | None = None
    output_tokens: int | None = None
    try:
        await client.connect()
        await client.query(json.dumps(raw_input, ensure_ascii=False, default=str))
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        text_chunks.append(block.text)
            elif isinstance(msg, ResultMessage):
                input_tokens, output_tokens = _usage_from_result_message(msg)
                break
    finally:
        await client.disconnect()

    latency_ms = int((time.monotonic() - started) * 1000)
    raw_text = "\n".join(text_chunks)
    try:
        raw_output = parse_decision_json(raw_text)
    except (json.JSONDecodeError, ValueError) as e:
        raise DecisionParseError(
            str(e),
            raw_text=raw_text,
            raw_input=raw_input,
            latency_ms=latency_ms,
        ) from e
    send = bool(raw_output.get("send"))
    suppressed_by = raw_output.get("suppressed_by")
    if send:
        suppressed_by = None
    elif not suppressed_by:
        suppressed_by = "mismatch"
    raw_output = {**raw_output, "send": send, "suppressed_by": suppressed_by}
    return Decision(
        send=send,
        matched_aspect=str(raw_output.get("matched_aspect") or ""),
        preview_hint=raw_output.get("preview_hint"),
        suppressed_by=suppressed_by,
        reason=str(raw_output.get("reason") or ""),
        raw_input=raw_input,
        raw_output=raw_output,
        latency_ms=latency_ms,
        model=settings.anthropic_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
