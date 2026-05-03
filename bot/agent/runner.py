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

from agent.request_context import RequestContext
from agent.tools_bitable import build_bitable_mcp
from agent.tools_calendar import build_calendar_mcp
from agent.tools_doc import build_doc_mcp
from agent.tools_external import build_external_mcp
from agent.tools_meta import build_meta_mcp
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

# 提问者身份

每条消息会以一行 `[asker] handle=@xxx user_id=... display_name=...` 开头——这是 host 注入的，是**地面真相**，**不要**质疑、**不要**重新查 lookup_user。

- 用户说"**我**昨天做了啥" / "我这周怎么样" / "**帮我**看下" 等——直接用 `[asker]` 里的 user_id，**不要**再调 lookup_user。
- 用户说"@bcc 在做啥"或"bcc 怎么样"——这种带具体 handle 的问法不是问自己，照常用 lookup_user 解析。
- 如果 `[asker]` 写的是 "(this Feishu user has not bound their pmo_agent account yet)"，说明发问者还没绑定飞书账号——遇到"我"这种第一人称问法时礼貌告诉他："你还没在 web 上绑定飞书账号，先去 https://pmo-agent-sigma.vercel.app/me 绑一下"，**不要**乱猜他是谁。
- **不要**在最终答案里把 `[asker]` 这行 echo 出来——那是给你看的元数据，不是给用户看的。

你能调用的工具：
- Meta: today_iso, list_users, lookup_user, get_recent_turns, get_project_overview, get_activity_stats, generate_image, resolve_people, undo_last_action
- Calendar: schedule_meeting, cancel_meeting, list_my_meetings
- Bitable: append_action_items, query_action_items, create_bitable_table, append_to_my_table, query_my_table, describe_my_table
- Doc: create_meeting_doc, create_doc, append_to_doc
- External: read_doc, read_external_table, resolve_feishu_link

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
- 透露 user_id (UUID) / token 等数据库内部细节
- 编造 turn 内容——只用工具返回的数据

你现在可以在飞书做事，不只是回答问题。默认行为仍然是文字回复；只有用户意图明确指向订会、取消会议、记 action item、建表、写文档、追加到文档、读取飞书链接时才调用工具。

硬规则：
- 调用任何接受人员参数的写工具前必须先调 resolve_people；如果 ambiguous/unresolved，先反问。
- 传给 schedule_meeting 的时间必须是 RFC3339 with timezone；先调 today_iso 拿时间锚点。
- today_iso 的日期锚点按用户时区解释；回答“明天/后天”这类相对日期时以 current_date / day_after_tomorrow_date 为准，不要用 UTC 日期脑补。
- list_my_meetings 如果返回 user_busy_slots，就算 user_calendar_events 为空也不能说“没有会议”；要说明这些时间被占用，若没有标题就说标题不可见。
- list_my_meetings 如果返回 user_calendar_error / user_calendar_warning / user_freebusy_error，不要把“读取失败”总结成“没有会议”。
- 不要修改不是你创建的飞书资源。只能取消/编辑 bot_actions 中属于当前用户/会话的事件、文档、表。
- append_to_doc 仅作用于由 bot 自己创建的文档；不要尝试 append 到用户分享给你的链接。
- 用户粘贴飞书 URL 时先调 resolve_feishu_link，再决定 read_doc / read_external_table。
- 第一人称日历问题调用 list_my_meetings 时不传 target_open_id。
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
    ctx: RequestContext = field(default_factory=RequestContext)
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
            ctx = RequestContext()
            options = ClaudeAgentOptions(
                system_prompt=SYSTEM_PROMPT,
                allowed_tools=[
                    "mcp__pmo_meta__list_users",
                    "mcp__pmo_meta__lookup_user",
                    "mcp__pmo_meta__get_recent_turns",
                    "mcp__pmo_meta__get_project_overview",
                    "mcp__pmo_meta__get_activity_stats",
                    "mcp__pmo_meta__generate_image",
                    "mcp__pmo_meta__today_iso",
                    "mcp__pmo_meta__resolve_people",
                    "mcp__pmo_meta__undo_last_action",
                    "mcp__pmo_calendar__schedule_meeting",
                    "mcp__pmo_calendar__cancel_meeting",
                    "mcp__pmo_calendar__list_my_meetings",
                    "mcp__pmo_bitable__append_action_items",
                    "mcp__pmo_bitable__query_action_items",
                    "mcp__pmo_bitable__create_bitable_table",
                    "mcp__pmo_bitable__append_to_my_table",
                    "mcp__pmo_bitable__query_my_table",
                    "mcp__pmo_bitable__describe_my_table",
                    "mcp__pmo_doc__create_meeting_doc",
                    "mcp__pmo_doc__create_doc",
                    "mcp__pmo_doc__append_to_doc",
                    "mcp__pmo_external__read_doc",
                    "mcp__pmo_external__read_external_table",
                    "mcp__pmo_external__resolve_feishu_link",
                ],
                mcp_servers={
                    "pmo_meta": build_meta_mcp(ctx),
                    "pmo_calendar": build_calendar_mcp(ctx),
                    "pmo_bitable": build_bitable_mcp(ctx),
                    "pmo_doc": build_doc_mcp(ctx),
                    "pmo_external": build_external_mcp(ctx),
                },
                # No write/exec tools — explicitly empty.
                disallowed_tools=[
                    "Bash", "Write", "Edit", "NotebookEdit",
                    "WebFetch", "WebSearch", "Task", "TodoWrite",
                ],
                max_turns=settings.agent_max_duration_seconds,
            )
            client = ClaudeSDKClient(options=options)
            await client.connect()
            slot = _PooledClient(client=client, ctx=ctx)
            _pool[conversation_key] = slot
            logger.info("agent: created client for %s", conversation_key)
        slot.last_used = time.monotonic()
        return slot


async def answer(
    conversation_key: str,
    question: str,
    *,
    message_id: str = "",
    chat_id: str = "",
    sender_open_id: str = "",
) -> str:
    """Run the agent and return only the final answer text.

    Kept for callers that don't care about progress; new code should use
    `answer_streaming` to get tool-call events as they happen.
    """
    answer_text = ""
    tool_count = 0
    async for ev in answer_streaming(
        conversation_key,
        question,
        message_id=message_id,
        chat_id=chat_id,
        sender_open_id=sender_open_id,
    ):
        if ev["kind"] == "tool":
            tool_count += 1
        elif ev["kind"] == "final":
            answer_text = ev["text"]
    return answer_text or "(空回答 — 试试换个问法?)"


async def answer_streaming(
    conversation_key: str,
    question: str,
    *,
    message_id: str = "",
    chat_id: str = "",
    sender_open_id: str = "",
):
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
        slot.ctx.message_id = message_id
        slot.ctx.chat_id = chat_id
        slot.ctx.sender_open_id = sender_open_id
        slot.ctx.conversation_key = conversation_key
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
                            display = _strip_pmo_prefix(name)
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


_PMO_PREFIXES = (
    "mcp__pmo_meta__",
    "mcp__pmo_calendar__",
    "mcp__pmo_bitable__",
    "mcp__pmo_doc__",
    "mcp__pmo_external__",
)


def _strip_pmo_prefix(tool_name: str) -> str:
    for prefix in _PMO_PREFIXES:
        if tool_name.startswith(prefix):
            return tool_name[len(prefix):]
    return tool_name


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
