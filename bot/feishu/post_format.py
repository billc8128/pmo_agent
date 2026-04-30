"""Convert agent-generated markdown into Feishu's `post` rich-text format.

Feishu post supports:
  - text with bold / italic / underline / strikethrough
  - text with hyperlink
  - @-mention (we don't use it here)
  - line break

Things common in markdown that Feishu post does NOT support:
  - inline code (``code``) — we render as bold text so it visually
    stands out, plus wrap in real backticks for clarity
  - fenced code blocks — emit as plain monospace-looking text via the
    same bold + backtick treatment, line by line
  - bullet lists / ordered lists — we prefix lines with "•" or "1."
    as plain text characters
  - blockquotes — strip the leading ">" and keep the line
  - headings — drop the # marker, render as bold

The goal is not pretty — the goal is "readable enough" so post msg_type
isn't worse than plain text. If the user finds B unreadable, we revert
to A1 (slim card) per the user's explicit fallback.

Output format: a list of paragraphs, each paragraph a list of inline
"runs" — exactly the structure Feishu's `post` content takes.
"""
from __future__ import annotations

import re
from typing import Any


def markdown_to_post(md: str) -> dict[str, Any]:
    """Returns a Feishu `post` content dict, ready to JSON-encode.

    Output shape:
      {
        "zh_cn": {
          "title": "",
          "content": [
            [{"tag": "text", "text": "...", "style": ["bold"]}, {"tag": "a", "text": "...", "href": "..."}, ...],
            [...],
          ]
        }
      }
    """
    paragraphs: list[list[dict[str, Any]]] = []

    # Normalize whitespace and split into paragraphs by blank lines.
    md = md.replace("\r\n", "\n")

    lines = md.split("\n")
    in_code_fence = False
    current_para: list[dict[str, Any]] = []

    def flush_para() -> None:
        nonlocal current_para
        if current_para:
            paragraphs.append(current_para)
            current_para = []

    for raw in lines:
        line = raw

        # Code fence toggling — content between fences renders as
        # bold-monospace-ish via the inline-code treatment.
        if line.strip().startswith("```"):
            in_code_fence = not in_code_fence
            flush_para()
            continue

        if in_code_fence:
            # Each code line becomes its own paragraph, displayed via
            # the bold-with-backticks fallback.
            paragraphs.append([_code_run(line)])
            continue

        # Blank line: paragraph break.
        if line.strip() == "":
            flush_para()
            continue

        # Drop heading markers (no real heading support in post).
        m_h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m_h:
            flush_para()
            paragraphs.append(_render_inline("**" + m_h.group(2).strip() + "**"))
            continue

        # Bullet / ordered list: replace marker with a Unicode bullet
        # or keep the digit, then render the rest inline.
        m_ul = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        m_ol = re.match(r"^(\s*)(\d+)\.\s+(.*)$", line)
        if m_ul:
            flush_para()
            indent = m_ul.group(1)
            rest = m_ul.group(2)
            runs = [{"tag": "text", "text": indent + "• "}]
            runs.extend(_render_inline(rest))
            paragraphs.append(runs)
            continue
        if m_ol:
            flush_para()
            indent = m_ol.group(1)
            num = m_ol.group(2)
            rest = m_ol.group(3)
            runs = [{"tag": "text", "text": indent + num + ". "}]
            runs.extend(_render_inline(rest))
            paragraphs.append(runs)
            continue

        # Blockquote: drop ">" prefix.
        m_bq = re.match(r"^>\s?(.*)$", line)
        if m_bq:
            flush_para()
            paragraphs.append(_render_inline(m_bq.group(1)))
            continue

        # Horizontal rule: render as a divider line.
        if re.match(r"^\s*-{3,}\s*$", line) or re.match(r"^\s*\*{3,}\s*$", line):
            flush_para()
            paragraphs.append([{"tag": "text", "text": "──────"}])
            continue

        # Otherwise: regular paragraph line. Multi-line paragraphs are
        # joined with a space, like CommonMark.
        if current_para:
            current_para.append({"tag": "text", "text": " "})
        current_para.extend(_render_inline(line))

    flush_para()

    # Feishu requires non-empty content. If the LLM returned nothing,
    # provide a placeholder.
    if not paragraphs:
        paragraphs = [[{"tag": "text", "text": "(空回答 — 试试换个问法?)"}]]

    return {"zh_cn": {"title": "", "content": paragraphs}}


# ──────────────────────────────────────────────────────────────────────
# Inline parsing — bold / italic / inline code / link
# ──────────────────────────────────────────────────────────────────────

# Order matters: code first (anything inside ` ` is opaque), then
# bold (** **), then italic (* *), then links.
_INLINE_RE = re.compile(
    r"`([^`]+)`"                            # 1: inline code
    r"|\*\*(.+?)\*\*"                       # 2: bold
    r"|\*(.+?)\*"                           # 3: italic
    r"|\[([^\]]+)\]\(([^)]+)\)"             # 4 text, 5 url: link
)


def _render_inline(text: str) -> list[dict[str, Any]]:
    """Walk a single line and emit feishu post 'inline runs'."""
    runs: list[dict[str, Any]] = []
    pos = 0
    for m in _INLINE_RE.finditer(text):
        if m.start() > pos:
            runs.append({"tag": "text", "text": text[pos:m.start()]})
        if m.group(1) is not None:                        # `code`
            runs.append(_code_run("`" + m.group(1) + "`"))
        elif m.group(2) is not None:                      # **bold**
            runs.append({"tag": "text", "text": m.group(2), "style": ["bold"]})
        elif m.group(3) is not None:                      # *italic*
            runs.append({"tag": "text", "text": m.group(3), "style": ["italic"]})
        elif m.group(4) is not None:                      # [text](url)
            runs.append({"tag": "a", "text": m.group(4), "href": m.group(5)})
        pos = m.end()
    if pos < len(text):
        runs.append({"tag": "text", "text": text[pos:]})
    return runs or [{"tag": "text", "text": text}]


def _code_run(text: str) -> dict[str, Any]:
    """Inline code rendered as bold (post has no real code style)."""
    return {"tag": "text", "text": text, "style": ["bold"]}
