from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from agent import lockout
from db import queries
from external.logging import safe_log_value
from external.normalizer import normalize_github, normalize_gitea
from external.redaction import redact_payload

logger = logging.getLogger(__name__)

_TOP_LEVEL_FINGERPRINT_EXCLUDES = {
    "delivery_id",
    "ingested_at",
    "received_at",
    "project_root",
    "mentioned_profile_ids",
}
_SENSITIVE_HEADER_PARTS = ("signature", "authorization", "token", "secret")


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _stable_payload(value: dict[str, Any]) -> dict[str, Any]:
    stable = {
        k: v
        for k, v in value.items()
        if k not in _TOP_LEVEL_FINGERPRINT_EXCLUDES
    }
    actor = stable.get("actor")
    if isinstance(actor, dict):
        stable["actor"] = {k: v for k, v in actor.items() if k != "profile_id"}
    repo = stable.get("repo")
    if isinstance(repo, dict):
        stable["repo"] = {k: v for k, v in repo.items() if k != "project_root"}
    return stable


def source_id_for_event(normalized: dict[str, Any]) -> str | None:
    event_type = _text(normalized.get("event_type"))
    repo_name = _text(_as_dict(normalized.get("repo")).get("full_name"))
    if not event_type or not repo_name:
        return None

    if event_type == "pull_request":
        pr_number = _text(_as_dict(normalized.get("pr")).get("number"))
        return f"pull_request:{repo_name}:{pr_number}" if pr_number else None
    if event_type == "push":
        ref = _text(normalized.get("ref"))
        after = _text(normalized.get("after"))
        return f"push:{repo_name}:{ref}:{after}" if ref and after else None
    if event_type == "release":
        tag = _text(_as_dict(normalized.get("release")).get("tag_name"))
        return f"release:{repo_name}:{tag}" if tag else None
    if event_type == "issue_comment":
        comment_id = _text(_as_dict(normalized.get("comment")).get("id"))
        return f"issue_comment:{repo_name}:{comment_id}" if comment_id else None
    return None


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
    log_provider = safe_log_value(provider)
    log_event_type = safe_log_value(event_type)
    log_delivery_id = safe_log_value(delivery_id)
    payload, redaction_hits = redact_payload(payload)
    archive_id = queries.archive_external_delivery(
        provider=provider,
        delivery_id=delivery_id,
        event_type=event_type,
        raw_body=payload,
        raw_headers=_safe_headers(headers or {}),
    )
    logger.info(
        "webhook.archived provider=%s event_type=%s delivery_id=%s archive_id=%s redaction_hits=%s",
        log_provider,
        log_event_type,
        log_delivery_id,
        archive_id,
        redaction_hits,
    )

    if provider == "github":
        normalized = normalize_github(event_type, payload)
    elif provider == "gitea":
        normalized = normalize_gitea(event_type, payload)
    else:
        queries.mark_archive_ignored(archive_id, "unsupported_provider")
        logger.info(
            "webhook.archive_ignored provider=%s event_type=%s delivery_id=%s archive_id=%s reason=unsupported_provider",
            log_provider,
            log_event_type,
            log_delivery_id,
            archive_id,
        )
        return
    if normalized is None:
        queries.mark_archive_ignored(archive_id, "unsupported_event_type")
        logger.info(
            "webhook.archive_ignored provider=%s event_type=%s delivery_id=%s archive_id=%s reason=unsupported_event_type",
            log_provider,
            log_event_type,
            log_delivery_id,
            archive_id,
        )
        return

    actor = _as_dict(normalized.get("actor"))
    if actor.get("is_bot"):
        queries.mark_archive_ignored(archive_id, "bot_actor")
        logger.info(
            "webhook.archive_ignored provider=%s event_type=%s delivery_id=%s archive_id=%s reason=bot_actor actor=%s",
            log_provider,
            log_event_type,
            log_delivery_id,
            archive_id,
            safe_log_value(actor.get("login") or ""),
        )
        return

    source_id = source_id_for_event(normalized)
    if source_id is None:
        queries.mark_archive_ignored(archive_id, "missing_source_identity")
        logger.info(
            "webhook.archive_ignored provider=%s event_type=%s delivery_id=%s archive_id=%s reason=missing_source_identity",
            log_provider,
            log_event_type,
            log_delivery_id,
            archive_id,
        )
        return

    event_id = queries.upsert_event(
        source=provider,
        source_id=source_id,
        user_id=None,
        project_root=normalized.get("project_root") or (normalized.get("repo") or {}).get("project_root"),
        occurred_at=normalized.get("occurred_at"),
        payload=normalized,
        payload_fingerprint=payload_fingerprint(normalized),
    )
    if event_id is not None:
        queries.link_archive_to_event(archive_id, event_id)
        lockout.invalidate_project_tokens_cache()
    logger.info(
        "webhook.event_upserted provider=%s event_type=%s delivery_id=%s archive_id=%s event_id=%s source_id=%s project_root=%s",
        log_provider,
        log_event_type,
        log_delivery_id,
        archive_id,
        event_id,
        safe_log_value(source_id),
        safe_log_value(normalized.get("project_root") or ""),
    )
