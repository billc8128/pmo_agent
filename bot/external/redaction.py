from __future__ import annotations

import re
from typing import Any

_REDACTED = "[REDACTED]"

_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_TOKEN_RES = [
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b"),
    re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b"),
    re.compile(r"(?i)\bpostgres(?:ql)?://[^\s\"'<>]+"),
    re.compile(r"https://open\.(?:feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook/[A-Za-z0-9_-]+"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?:\+86[\s-]*)?1[3-9]\d[\s-]*\d{4}[\s-]*\d{4}\b")
_ID_CARD_RE = re.compile(r"\b[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx]\b")
_PAYMENT_CARD_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SENSITIVE_HOST_RE = re.compile(
    r"(?i)\b((?:ssh|password|passwd|secret|token|database|db|redis|postgres)[^\n]{0,80}?)"
    r"((?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?)"
)
_HOST_MARKER_RES = [
    (re.compile(r"\[asker\]"), "[chat_memory_escaped_marker:asker]"),
    (re.compile(r"\[parent_notification\]"), "[chat_memory_escaped_marker:parent_notification]"),
    (re.compile(r"\[IMAGE:[^\]]+\]"), "[chat_memory_escaped_marker:image]"),
]
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?token)\s*[:=]\s*([^\s,;\"'`<>]+)"
)


def _bump(categories: dict[str, int], category: str, count: int) -> None:
    if count:
        categories[category] = categories.get(category, 0) + count


def _luhn_valid(digits: str) -> bool:
    if len(digits) < 13 or len(digits) > 19 or not digits.isdigit():
        return False
    if len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        value = ord(ch) - ord("0")
        if i % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def redact_text_with_categories(value: str) -> tuple[str, dict[str, int]]:
    redacted = value
    categories: dict[str, int] = {}

    redacted, n = _PRIVATE_KEY_RE.subn(_REDACTED, redacted)
    _bump(categories, "private_key", n)
    for pattern in _TOKEN_RES:
        redacted, n = pattern.subn(_REDACTED, redacted)
        _bump(categories, "token", n)
    redacted, n = _EMAIL_RE.subn(_REDACTED, redacted)
    _bump(categories, "email", n)
    redacted, n = _PHONE_RE.subn(_REDACTED, redacted)
    _bump(categories, "phone", n)
    redacted, n = _ID_CARD_RE.subn(_REDACTED, redacted)
    _bump(categories, "id_card", n)

    def replace_payment_card(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if not _luhn_valid(digits):
            return match.group(0)
        categories["payment_card"] = categories.get("payment_card", 0) + 1
        return _REDACTED

    redacted = _PAYMENT_CARD_CANDIDATE_RE.sub(replace_payment_card, redacted)

    def replace_sensitive_host(match: re.Match[str]) -> str:
        return f"{match.group(1)}{_REDACTED}"

    redacted, n = _SENSITIVE_HOST_RE.subn(replace_sensitive_host, redacted)
    _bump(categories, "sensitive_host", n)
    for pattern, replacement in _HOST_MARKER_RES:
        redacted, n = pattern.subn(replacement, redacted)
        _bump(categories, "host_marker", n)

    def replace_assignment(match: re.Match[str]) -> str:
        return f"{match.group(1)}={_REDACTED}"

    redacted, n = _ASSIGNMENT_RE.subn(replace_assignment, redacted)
    _bump(categories, "assignment", n)
    return redacted, categories


def redact_text(value: str) -> tuple[str, int]:
    redacted, categories = redact_text_with_categories(value)
    count = sum(categories.values())
    return redacted, count


def redact_payload(value: Any) -> tuple[Any, int]:
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        total = 0
        out = []
        for item in value:
            redacted, count = redact_payload(item)
            out.append(redacted)
            total += count
        return out, total
    if isinstance(value, dict):
        total = 0
        out: dict[str, Any] = {}
        for key, item in value.items():
            redacted, count = redact_payload(item)
            out[key] = redacted
            total += count
        return out, total
    return value, 0
