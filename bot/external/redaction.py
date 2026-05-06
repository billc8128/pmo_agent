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
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?token)\s*[:=]\s*([^\s,;\"'`<>]+)"
)


def redact_text(value: str) -> tuple[str, int]:
    redacted = value
    count = 0

    redacted, n = _PRIVATE_KEY_RE.subn(_REDACTED, redacted)
    count += n
    for pattern in _TOKEN_RES:
        redacted, n = pattern.subn(_REDACTED, redacted)
        count += n

    def replace_assignment(match: re.Match[str]) -> str:
        return f"{match.group(1)}={_REDACTED}"

    redacted, n = _ASSIGNMENT_RE.subn(replace_assignment, redacted)
    count += n
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
