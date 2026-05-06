from __future__ import annotations

import hashlib
import json
from typing import Any

from db import queries
from external.normalizer import normalize_github, normalize_gitea

_VOLATILE_FINGERPRINT_FIELDS = {"delivery_id", "ingested_at", "received_at"}
_SENSITIVE_HEADER_PARTS = ("signature", "authorization", "token", "secret")


def _stable_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _stable_payload(v)
            for k, v in sorted(value.items())
            if k not in _VOLATILE_FINGERPRINT_FIELDS
        }
    if isinstance(value, list):
        return [_stable_payload(v) for v in value]
    return value


def payload_fingerprint(payload: dict[str, Any]) -> str:
    stable = json.dumps(_stable_payload(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.md5(stable.encode("utf-8")).hexdigest()


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        if any(part in lower for part in _SENSITIVE_HEADER_PARTS):
            continue
        safe[lower] = value
    return safe


async def ingest_external_event(
    *,
    provider: str,
    event_type: str,
    delivery_id: str,
    payload: dict[str, Any],
    raw_bytes: bytes,
    headers: dict[str, str] | None = None,
) -> None:
    archive_id = queries.archive_external_delivery(
        provider=provider,
        delivery_id=delivery_id,
        event_type=event_type,
        raw_body=payload,
        raw_headers=_safe_headers(headers or {}),
    )

    if provider == "github":
        normalized = normalize_github(event_type, payload)
    elif provider == "gitea":
        normalized = normalize_gitea(event_type, payload)
    else:
        return
    if normalized is None:
        return

    event_id = queries.upsert_event(
        source=provider,
        source_id=f"{event_type}:{delivery_id}",
        user_id=(normalized.get("actor") or {}).get("profile_id"),
        project_root=normalized.get("project_root") or (normalized.get("repo") or {}).get("project_root"),
        occurred_at=normalized.get("occurred_at"),
        payload=normalized,
        payload_fingerprint=payload_fingerprint(normalized),
    )
    if event_id is not None:
        queries.link_archive_to_event(archive_id, event_id)
