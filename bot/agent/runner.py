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

from agent import tools as agent_tools
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


# Use plain string + str.replace() instead of f-string. The prompt
# contains many literal `{...}` expressions (markdown code blocks,
# tool return shapes like {image_key, image_url}, marker examples
# like [IMAGE:<image_key>]) that f-string would try to evaluate as
# Python expressions and crash on import.
_SYSTEM_PROMPT_TEMPLATE = """你是 pmo_agent 的 PMO 助手。pmo_agent 是一个公开记录大家用 AI 编程的工作时间线的项目——daemon 跑在每个人电脑上，把和 Claude Code / Codex 的每一轮对话上传到云端，每条对话都有一句话的 LLM 摘要。

你的工作是回答关于团队成员工作的问题，比如：
- "bcc 昨天做了啥？"
- "albert 这周在干嘛？"
- "谁最近最活跃？"
- "pmo_agent 这个项目最近有什么进展？"

你能调用的工具：
- list_users          看全部已注册的用户
- lookup_user         按 handle 查到 user_id（handle 可带或不带 @）
- get_recent_turns    拉某个用户最近的 turn（按时间窗口、项目过滤）。
                      返回每条 turn 的 user_message + agent_summary
- get_project_overview 每个项目的累积叙事摘要（最高密度的"在做什么"信号）
- get_activity_stats  按日 / 按项目聚合 turn 数（"有多忙"类问题）
- today_iso          拿当前时间锚点（今天起、昨天、7天前、30天前 ISO）

# 选 tool 的策略（重要）

**先用低密度工具，再用高密度工具**。原始 turn 数据量大，先看高维摘要决定要不要深挖。

- 问"X 在做什么 / 最近什么进展" → **先 get_project_overview**（一段话/项目）。如果回答够清楚就停，不要再去拉 raw turns。
- 问"X 有多忙 / 做了多少" → **get_activity_stats**（数字）
- 问"X 昨天/今天/具体某天做了啥" → 先 get_activity_stats 看哪个项目活跃，再用 get_recent_turns 拉那个项目+那天的细节
- 问"全员 / 大家都做了啥" → **不要**给每个人都拉 raw turns（你现在的常见错误！）。每人调 get_project_overview 拿一段叙事，然后总结
- 一定要细看具体 turn 内容时才用 get_recent_turns，limit 默认给 50 够用，**不要给 20**——会漏

# 时间窗口

时间相关问题先调 today_iso 拿锚点。"今天" = today_start 到 now，"昨天" = yesterday_start 到 yesterday_end，"这周" = seven_days_ago 到 now。**不要**自己造 ISO 字符串。

# 多用户问题模板

问"今天/这周大家都做了啥"的标准流程：
1. today_iso（拿时间锚点）
2. list_users（拿到所有 handle + user_id）
3. **每个 user 调一次 get_project_overview** —— 拿叙事摘要，**不要**调 get_recent_turns
4. 整合：每人一段，列出他们各自在做什么

# 回答风格

- 直接给结论，不啰嗦"我查了一下"
- 用具体细节：项目名、文件名、决定、问题。避免"做了一些工作"这种空话
- 多人问题：每人 1-2 句话，列表展示；单人问题：一段 2-4 句的叙事
- 中文为主（用户问中文用中文，问英文用英文）
- 如果数据里没有相关 turn（用户没活动 / handle 不存在），直说"过去 X 天没有活动"或"没找到这个用户"

关于链接：每个用户的公开时间线在 {WEB_BASE_URL}/u/<handle>。提到某人时如果回答短，可以在末尾附 `[完整时间线]({WEB_BASE_URL}/u/<handle>)`。

# 生图（generate_image）

你有一个 generate_image 工具，可以基于你查到的工作画像画一张图。常见触发：
- "画一下 bcc 的样子"
- "给团队画一张合影"
- "想象一下这个项目的视觉风格"

工作流：
1. 先查相关数据（list_users / get_project_overview）拿到具体细节——这个人在做什么项目、用什么工具、风格如何
2. **基于这些具体细节**写 prompt，**不要**写空泛的"一个程序员"。比如 bcc 在做 pmo_agent（终端 + 飞书 bot），prompt 可以写"a stylized illustration of a developer at a glowing terminal, building a chat bot, retro pixel art style, warm lighting"
3. 调 generate_image，返回 `{image_key, image_url}`
4. 在你的最终答案里**用 marker 嵌入图**：`[IMAGE:<image_key>]`。这个 marker 会被替换成飞书图片消息。**不要**把 image_url 当链接发出去——用户看到的是真图。

风格建议：默认用 stylized illustration（插画/卡通/像素风/水彩等），**不要**用 photorealistic——避免假装是真人照片的伦理问题。

你不能：
- 写代码、改文件、跑命令——这是只读问答助手
- 透露 user_id (UUID) / token 等数据库内部细节
- 编造 turn 内容——只用工具返回的数据
"""

# Resolve the single placeholder via plain string replace (no
# f-string interpolation, no .format(), so braces in the rest of the
# prompt body are left untouched).
SYSTEM_PROMPT = _SYSTEM_PROMPT_TEMPLATE.replace(
    "{WEB_BASE_URL}", settings.web_base_url,
)


# ── Per-conversation client pool ───────────────────────────────────────


@dataclass
class _PooledClient:
    client: ClaudeSDKClient
    last_used: float = field(default_factory=time.monotonic)
    busy: bool = False
    # Per-conversation FIFO so concurrent messages from the same
    # (chat_id, sender_id) get processed in order rather than rejected.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
                    "mcp__pmo__generate_image",
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
    """Run the agent and return only the final answer text.

    Kept for callers that don't care about progress; new code should use
    `answer_streaming` to get tool-call events as they happen.
    """
    answer_text = ""
    tool_count = 0
    async for ev in answer_streaming(conversation_key, question):
        if ev["kind"] == "tool":
            tool_count += 1
        elif ev["kind"] == "final":
            answer_text = ev["text"]
    return answer_text or "(空回答 — 试试换个问法?)"


async def answer_streaming(conversation_key: str, question: str):
    """Run the agent and yield progress events as they happen.

    Yields dicts of one of these shapes:
      {"kind": "tool",  "name": str, "args_hint": str}   — about to call a tool
      {"kind": "final", "text": str}                      — final answer text
      {"kind": "error", "message": str}                   — exception

    Multiple concurrent calls with the same conversation_key are
    SERIALIZED via the slot's lock — the second caller waits for the
    first to finish, then runs. We deliberately don't reject; the
    caller (a webhook handler) has already sent an ack reaction +
    progress card, and silently dropping the message would feel like
    the bot ignored the user.
    """
    slot = await _get_client(conversation_key)

    # FIFO serialization. Note we await BEFORE setting busy so we
    # also queue behind other in-flight calls on the same slot.
    async with slot.lock:
        slot.busy = True
        # Tell the image tool which conversation we're answering for
        # (used for per-conversation rate limiting on image generation).
        agent_tools.set_current_conversation(conversation_key)
        try:
            await slot.client.query(question)
            final_text_chunks: list[str] = []
            async for msg in slot.client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            final_text_chunks.append(block.text)
                        elif isinstance(block, ToolUseBlock):
                            name = block.name
                            # Strip the "mcp__pmo__" prefix the SDK adds.
                            if name.startswith("mcp__pmo__"):
                                display = name[len("mcp__pmo__"):]
                            else:
                                display = name
                            args_hint = _format_args_hint(block.input or {})
                            logger.info(
                                "agent: tool=%s input_keys=%s",
                                name,
                                list((block.input or {}).keys()),
                            )
                            yield {
                                "kind": "tool",
                                "name": display,
                                "args_hint": args_hint,
                            }
                elif isinstance(msg, ResultMessage):
                    # The SDK's terminating message; stop reading.
                    break
            final_text = "\n".join(final_text_chunks).strip()
            yield {"kind": "final", "text": final_text}
        except Exception as e:
            logger.exception("agent failed: %s", conversation_key)
            yield {"kind": "error", "message": f"{type(e).__name__}: {e}"}
        finally:
            slot.busy = False


def _format_args_hint(args: dict) -> str:
    """One-line summary of a tool call's interesting args.

    Skips long fields (anything >40 chars), prefers human-meaningful
    keys when present (handle / user_id / since / until / project_root),
    truncates the rest.
    """
    if not args:
        return ""
    preferred = ("handle", "user_id", "days", "limit", "since", "until", "project_root")
    parts: list[str] = []
    for k in preferred:
        if k in args and args[k] is not None and args[k] != "":
            v = args[k]
            sv = str(v)
            # ISO timestamps are ugly — show just the date for readability.
            if k in ("since", "until") and "T" in sv:
                sv = sv[:10]
            # user_id is a UUID — too noisy.
            if k == "user_id":
                sv = sv[:8] + "…"
            if len(sv) > 40:
                sv = sv[:37] + "…"
            parts.append(f"{k}={sv}")
            if len(parts) >= 3:
                break
    return " · ".join(parts)


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
