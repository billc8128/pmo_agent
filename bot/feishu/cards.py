"""Feishu interactive-card builders for the bot.

We use the v2 card schema (schema "2.0"). It's simpler than the
legacy v1 ("config" + "i18n_elements" + "header") and supports
Markdown elements directly.

There are two cards we render:

  1. A **progress** card while the agent is thinking — an evolving
     list of "tool calls" so the user can see what's being looked up.
  2. A **final** card with the agent's markdown answer + a small
     footer summarizing how many tools were called.
"""
from __future__ import annotations

from typing import Any


def progress_card(*, question: str, steps: list[dict[str, Any]]) -> dict[str, Any]:
    """A card showing the user's question + a checklist of tool calls.

    steps is a list of {"tool": str, "args_hint": str, "done": bool}.
    Pending steps render with 🔧 (wrench), done with ✓.
    """
    elements: list[dict[str, Any]] = []

    # Show the question as quoted context — useful in groups where
    # multiple bots / threads might be active.
    elements.append({
        "tag": "markdown",
        "content": f"**Q:** {_inline_escape(question)}",
    })

    if steps:
        elements.append({"tag": "hr"})
        lines = []
        for s in steps:
            mark = "✓" if s.get("done") else "🔧"
            tool = s.get("tool", "?")
            hint = s.get("args_hint") or ""
            line = f"{mark} `{tool}`"
            if hint:
                line += f" · {hint}"
            lines.append(line)
        elements.append({
            "tag": "markdown",
            "content": "\n".join(lines),
        })

    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "正在查询…"},
            "template": "blue",
        },
        "body": {"elements": elements},
    }


def final_card(*, question: str, answer_markdown: str, tool_count: int) -> dict[str, Any]:
    """The card we patch the progress card into when done.

    answer_markdown is the LLM's final reply, rendered as a Feishu
    markdown element (supports **bold**, *italic*, `code`, lists, links).
    """
    elements: list[dict[str, Any]] = [
        {
            "tag": "markdown",
            "content": f"**Q:** {_inline_escape(question)}",
        },
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": answer_markdown if answer_markdown.strip() else "_(空回答)_",
        },
    ]

    if tool_count > 0:
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "markdown",
            "content": f"_调用了 {tool_count} 次工具_",
        })

    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "PMO bot"},
            "template": "indigo",
        },
        "body": {"elements": elements},
    }


def error_card(*, question: str, error: str) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "出错了"},
            "template": "red",
        },
        "body": {
            "elements": [
                {"tag": "markdown", "content": f"**Q:** {_inline_escape(question)}"},
                {"tag": "hr"},
                {"tag": "markdown", "content": f"`{_inline_escape(error)}`"},
            ],
        },
    }


def _inline_escape(s: str) -> str:
    """Lightly defang Markdown control characters so user prompts that
    contain `**` or backticks don't accidentally style our card.
    """
    return s.replace("\n", " ").replace("`", "\\`").strip()[:300]
