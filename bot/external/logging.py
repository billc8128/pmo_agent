from __future__ import annotations

import re
from typing import Any

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe_log_value(value: Any, *, max_len: int = 160) -> str:
    text = str(value or "")
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = _CONTROL_RE.sub("?", text)
    if len(text) > max_len:
        return f"{text[: max_len - 1]}..."
    return text
