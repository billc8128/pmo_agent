from __future__ import annotations

import re
from typing import Any

from external.redaction import redact_text_with_categories


_WORD_RE = re.compile(r"[a-zA-Z0-9_./:-]{2,}|[\u4e00-\u9fff]{2,}")


def person_key(*, profile_id: str | None = None, feishu_open_id: str | None = None) -> str:
    if profile_id:
        return f"profile:{profile_id}"
    if feishu_open_id:
        return f"feishu:{feishu_open_id}"
    raise ValueError("profile_id or feishu_open_id is required")


def person_label(row: dict[str, Any]) -> str:
    display = (row.get("display_name") or "").strip()
    handle = (row.get("handle") or "").strip()
    if display and handle:
        return f"{display} / @{handle}"
    if handle:
        return f"@{handle}"
    if display:
        return display
    return "这位成员"


def sanitize_note(note: str, *, max_chars: int = 1200) -> str:
    redacted, _ = redact_text_with_categories(note or "")
    redacted = re.sub(r"\s+", " ", redacted).strip()
    return redacted[:max_chars]


def topic_terms(topic: str) -> list[str]:
    raw = (topic or "").strip().lower()
    terms = [m.group(0).lower() for m in _WORD_RE.finditer(raw)]
    if raw and len(raw) <= 40:
        terms.insert(0, raw)
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        if term and term not in seen:
            seen.add(term)
            out.append(term)
    return out


def score_note_for_topic(note: str, topic: str) -> tuple[int, list[str]]:
    safe_note = sanitize_note(note).lower()
    matched: list[str] = []
    score = 0
    for term in topic_terms(topic):
        if term in safe_note:
            matched.append(term)
            score += max(1, min(len(term), 8))
    return score, matched[:6]


def confidence_for_score(score: int) -> str:
    if score >= 12:
        return "high"
    if score >= 3:
        return "medium"
    return "low"


def people_signal_summary(row: dict[str, Any], *, topic: str) -> dict[str, Any] | None:
    note = sanitize_note(row.get("pmo_notes") or "")
    if not note:
        return None
    score, matched = score_note_for_topic(note, topic)
    if topic and score <= 0:
        return None
    label = person_label(row)
    confidence = confidence_for_score(score or min(len(note) // 80, 2))
    if matched:
        signal = "、".join(matched[:4])
        summary = f"和 {signal} 有相关工作信号；适合先请 TA 看这个方向。"
    else:
        summary = "有一定历史协作信号，但当前 topic 的证据不够强，适合先轻量确认。"
    return {
        "person": label,
        "summary": summary,
        "confidence": confidence,
        "last_observed_at": row.get("last_observed_at"),
    }


def compose_background_note(
    *,
    display_name: str,
    existing_note: str,
    messages: list[dict[str, Any]],
    max_chars: int = 900,
) -> str:
    snippets: list[str] = []
    for msg in messages[:8]:
        text = sanitize_note(msg.get("text_redacted") or "", max_chars=120)
        if text:
            snippets.append(text)
    if not snippets:
        return sanitize_note(existing_note, max_chars=max_chars)
    prefix = f"{display_name or '这位成员'} 最近在群聊中反复出现的工作上下文："
    new_context = "；".join(snippets)
    return sanitize_note(f"{prefix}{new_context}", max_chars=max_chars)
