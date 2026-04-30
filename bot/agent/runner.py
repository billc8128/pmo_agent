"""Claude Agent SDK runner — answers PMO questions using the supabase tools.

Per-conversation state:
  We keep a ClaudeSDKClient per (chat_id, sender_id) so multi-turn
  follow-ups in the same chat keep context. Idle clients are cleaned
  up after CLIENT_IDLE_TIMEOUT.

Anthropic-compatible backend:
  We export ANTHROPIC_AUTH_TOKEN / BASE_URL / MODEL into the process
  environment before any SDK call. The SDK forwards these to the
  underlying CLI (claude-agent-sdk ships a Node CLI that talks to
  Anthropic-compatible endpoints).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from agent.tools import build_pmo_mcp
from config import settings

logger = logging.getLogger(__name__)


# ── ENV setup for the underlying CLI ────────────────────────────────────


def _inject_anthropic_env() -> None:
    """The claude-agent-sdk Node CLI reads these from the process env."""
    os.environ["ANTHROPIC_AUTH_TOKEN"] = settings.anthropic_auth_token
    os.environ["ANTHROPIC_BASE_URL"] = settings.anthropic_base_url
    os.environ["ANTHROPIC_MODEL"] = settings.anthropic_model
    os.environ["ANTHROPIC_DEFAULT_OPUS_MODEL"] = settings.anthropic_default_opus_model
    os.environ["ANTHROPIC_DEFAULT_SONNET_MODEL"] = settings.anthropic_default_sonnet_model
    os.environ["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = settings.anthropic_default_haiku_model
    os.environ["API_TIMEOUT_MS"] = settings.api_timeout_ms
    os.environ["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = settings.claude_code_disable_nonessential_traffic


_inject_anthropic_env()


# ── System prompt: defines the agent's job + style ─────────────────────


SYSTEM_PROMPT = f"""你是 pmo_agent 的 PMO 助手。pmo_agent 是一个公开记录大家用 AI 编程的工作时间线的项目——daemon 跑在每个人电脑上，把和 Claude Code / Codex 的每一轮对话上传到云端，每条对话都有一句话的 LLM 摘要。

你的工作是回答关于团队成员工作的问题，比如：
- "bcc 昨天做了啥？"
- "albert 这周在干嘛？"
- "谁最近最活跃？"
- "pmo_agent 这个项目最近有什么进展？"

你能调用的工具：
- list_users          看全部已注册的用户
- lookup_user         按 handle 查到 user_id（handle 可带或不带 @）
- get_recent_turns    拉某个用户最近的 turn（可按时间窗口、项目过滤）
- get_project_overview 拉某个用户每个项目的累积摘要（高维信号）
- get_activity_stats  按日 / 按项目聚合 turn 数（"有多忙"类问题）
- today_iso          拿当前时间锚点（今天起、昨天、7天前、30天前 ISO）

工作流：
1. 看清问题问的是谁、什么时间窗口、什么项目维度
2. 时间相关的问题，先调 today_iso 拿准确锚点，不要猜
3. 用 lookup_user 拿到 user_id
4. 决定调 get_recent_turns（要细节）还是 get_project_overview（要大纲）还是 get_activity_stats（要数字）
5. 根据返回的数据写一段简洁的回答

回答风格：
- 直接给结论，不啰嗦"我查了一下"
- 用具体细节：项目名、文件名、决定、问题。避免"做了一些工作"这种空话
- 一段话搞定，2-4 句。除非用户明确要列表才列点
- 中文为主（用户问中文用中文，问英文用英文）
- 如果数据里没有相关 turn（用户没活动 / handle 不存在），直说"过去 X 天没有活动"或"没找到这个用户"

关于链接：每个用户的公开时间线在 {settings.web_base_url}/u/<handle>。如果回答里提到某个用户，可以在最后附一句 "完整时间线：{settings.web_base_url}/u/<handle>"——但只在用户明确想看更多细节时才附。

你不能：
- 写代码、改文件、跑命令——这是只读问答助手
- 透露 user_id (UUID) / token 等数据库内部细节
- 编造 turn 内容——只用工具返回的数据
"""


# ── Per-conversation client pool ───────────────────────────────────────


@dataclass
class _PooledClient:
    client: ClaudeSDKClient
    last_used: float = field(default_factory=time.monotonic)
    busy: bool = False


_pool: dict[str, _PooledClient] = {}
_pool_lock = asyncio.Lock()
_CLIENT_IDLE_TIMEOUT = 30 * 60  # 30 minutes


async def _get_client(conversation_key: str) -> _PooledClient:
    """Get or build the SDK client for this conversation."""
    async with _pool_lock:
        slot = _pool.get(conversation_key)
        if slot is None:
            options = ClaudeAgentOptions(
                system_prompt=SYSTEM_PROMPT,
                # Bot is read-only; the only tools are our supabase MCP.
                # We disable Claude's built-in file/bash/web tools entirely.
                allowed_tools=[
                    "mcp__pmo__list_users",
                    "mcp__pmo__lookup_user",
                    "mcp__pmo__get_recent_turns",
                    "mcp__pmo__get_project_overview",
                    "mcp__pmo__get_activity_stats",
                    "mcp__pmo__today_iso",
                ],
                mcp_servers={"pmo": build_pmo_mcp()},
                # No write/exec tools — explicitly empty.
                disallowed_tools=[
                    "Bash", "Write", "Edit", "NotebookEdit",
                    "WebFetch", "WebSearch", "Task", "TodoWrite",
                ],
                max_turns=settings.agent_max_duration_seconds,
            )
            client = ClaudeSDKClient(options=options)
            await client.connect()
            slot = _PooledClient(client=client)
            _pool[conversation_key] = slot
            logger.info("agent: created client for %s", conversation_key)
        slot.last_used = time.monotonic()
        return slot


async def answer(conversation_key: str, question: str) -> str:
    """Run the agent on a single user message and return the final text.

    conversation_key uniquely identifies one ongoing conversation thread —
    typically (chat_id, sender_open_id). Multiple calls with the same key
    share short-term memory.
    """
    slot = await _get_client(conversation_key)

    # Concurrency guard: refuse a second concurrent question on the same
    # conversation thread. Otherwise the SDK gets confused and the user
    # would step on themselves.
    if slot.busy:
        return "(还在处理上一个问题，稍等几秒再发吧)"
    slot.busy = True
    try:
        await slot.client.query(question)
        final_text_chunks: list[str] = []
        async for msg in slot.client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        final_text_chunks.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        logger.info(
                            "agent: tool=%s input_keys=%s",
                            block.name,
                            list((block.input or {}).keys()),
                        )
            elif isinstance(msg, ResultMessage):
                # The SDK's terminating message; stop reading.
                break
        return "\n".join(final_text_chunks).strip() or "(空回答 — 试试换个问法?)"
    finally:
        slot.busy = False


async def shutdown_all() -> None:
    async with _pool_lock:
        for slot in _pool.values():
            try:
                await slot.client.disconnect()
            except Exception:
                pass
        _pool.clear()


async def gc_idle_clients() -> int:
    """Periodic cleanup. Returns count freed."""
    now = time.monotonic()
    freed = 0
    async with _pool_lock:
        stale = [
            k for k, slot in _pool.items()
            if not slot.busy and now - slot.last_used > _CLIENT_IDLE_TIMEOUT
        ]
        for k in stale:
            try:
                await _pool[k].client.disconnect()
            except Exception:
                pass
            del _pool[k]
            freed += 1
    if freed:
        logger.info("agent: GC freed %d idle clients", freed)
    return freed
