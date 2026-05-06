from __future__ import annotations

import hashlib
import time
from typing import Any

from db import queries

_CACHE_TTL_SECONDS = 60
_cached_at = 0.0
_cached_tokens: set[str] = set()
_cached_hash = hashlib.sha256(b"").hexdigest()[:16]


def _token_hash(tokens: set[str]) -> str:
    joined = "|".join(sorted(tokens))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def known_project_tokens() -> tuple[set[str], str]:
    global _cached_at, _cached_hash, _cached_tokens
    now = time.monotonic()
    if now - _cached_at < _CACHE_TTL_SECONDS:
        return set(_cached_tokens), _cached_hash
    tokens = {t for t in queries.distinct_project_root_tokens() if t}
    _cached_tokens = tokens
    _cached_hash = _token_hash(tokens)
    _cached_at = now
    return set(_cached_tokens), _cached_hash


def last_segment(project_root: str | None) -> str:
    if not project_root:
        return ""
    root = project_root.strip()
    if not root or root.endswith("/"):
        return ""
    return root.rsplit("/", 1)[-1].lower()


def project_tokens_for_event(event: Any) -> set[str]:
    payload = _value(event, "payload", {}) or {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        token
        for token in [
            last_segment(_value(event, "project_root")),
            last_segment(payload.get("project_path")),
            last_segment(payload.get("project_root")),
        ]
        if token
    }


def _value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _metadata(row: Any) -> dict[str, Any]:
    meta = _value(row, "metadata", {}) or {}
    return meta if isinstance(meta, dict) else {}


def is_project_mismatch(event: Any, sub: Any) -> bool:
    event_tokens = project_tokens_for_event(event)
    if not event_tokens:
        return False

    _, current_hash = known_project_tokens()
    metadata = _metadata(sub)
    if metadata.get("project_tokens_hash") != current_hash:
        sub_id = str(_value(sub, "id") or "")
        if sub_id:
            queries.index_subscription_metadata(sub_id)
            refreshed = queries.get_subscription(sub_id)
            if refreshed is not None:
                metadata = _metadata(refreshed)

    matched_projects = metadata.get("matched_projects") or []
    if not matched_projects:
        return False
    matched = {str(p).lower() for p in matched_projects if p}
    return event_tokens.isdisjoint(matched)
