from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from agent import decider, investigator
from config import settings
from db import queries

logger = logging.getLogger(__name__)


def _delivery_for_subscription(sub: Any) -> tuple[str | None, str | None]:
    scope_kind = getattr(sub, "scope_kind", None)
    scope_id = getattr(sub, "scope_id", None)
    if scope_kind == "chat":
        return "feishu_chat", scope_id
    if scope_kind == "user":
        linked = queries.feishu_link_for_user_id(str(scope_id or ""))
        if linked and linked.get("open_id"):
            return "feishu_user", linked["open_id"]
    return None, None


def _latest_event(bundle: Any) -> dict[str, Any]:
    events = list(getattr(bundle, "events", []) or [])
    if not events:
        return {"id": None, "payload_version": 1}
    return max(events, key=lambda ev: int(ev.get("id") or 0))


def _usage_value(usage: Any, key: str) -> int | None:
    if isinstance(usage, dict):
        return usage.get(key)
    return getattr(usage, key, None)


def _sanitize_brief_subjects(brief: dict[str, Any], bundle: Any) -> dict[str, Any]:
    allowed = {
        str(ev.get("user_id"))
        for ev in list(getattr(bundle, "events", []) or [])
        if ev.get("user_id")
    }
    raw_subjects = brief.get("subject_user_ids") or []
    if not isinstance(raw_subjects, list):
        raw_subjects = []
    seen: set[str] = set()
    subject_user_ids: list[str] = []
    for value in raw_subjects:
        user_id = str(value)
        if user_id in allowed and user_id not in seen:
            subject_user_ids.append(user_id)
            seen.add(user_id)
    return {**brief, "subject_user_ids": subject_user_ids}


def _release_or_suppress_parse_failure(job_id: int, claim_id: str, suppressed_by: str, reason: str) -> None:
    failures = queries.investigation_parse_failure_count(job_id)
    if failures >= 3:
        queries.mark_job_suppressed_if_claimed(
            job_id,
            claim_id,
            {"notify": False, "suppressed_by": suppressed_by, "reason": reason},
            input_tokens=None,
            output_tokens=None,
        )
    else:
        queries.release_job_claim(job_id, claim_id)


async def process_once(limit: int = 5) -> int:
    queries.reap_stale_job_claims()
    claim_id = str(uuid.uuid4())
    bundles = queries.claim_investigatable_jobs(
        claim_id,
        limit,
        window_minutes=settings.aggregation_window_minutes,
    )
    for bundle in bundles:
        job_id = int(bundle.job.id)
        try:
            brief, usage = await asyncio.wait_for(
                investigator.investigate(bundle),
                timeout=settings.investigator_max_duration_seconds,
            )
            brief = _sanitize_brief_subjects(brief, bundle)
            if brief.get("notify"):
                latest = _latest_event(bundle)
                delivery_kind, delivery_target = _delivery_for_subscription(bundle.subscription)
                queries.create_notification_for_investigation_job(
                    job_id=job_id,
                    claim_id=claim_id,
                    event_id=int(latest.get("id")),
                    subscription_id=bundle.job.subscription_id,
                    decided_payload_version=int(latest.get("payload_version") or 1),
                    payload_snapshot=brief,
                    delivery_kind=delivery_kind,
                    delivery_target=delivery_target,
                    input_tokens=_usage_value(usage, "input_tokens"),
                    output_tokens=_usage_value(usage, "output_tokens"),
                )
            else:
                queries.mark_job_suppressed_if_claimed(
                    job_id,
                    claim_id,
                    brief,
                    input_tokens=_usage_value(usage, "input_tokens"),
                    output_tokens=_usage_value(usage, "output_tokens"),
                )
        except asyncio.CancelledError:
            raise
        except decider.DecisionParseError as e:
            queries.bump_investigation_parse_failure(job_id, claim_id, str(e)[:500])
            _release_or_suppress_parse_failure(
                job_id,
                claim_id,
                "investigator_parse_error",
                "investigator output parse failed 3 times",
            )
        except asyncio.TimeoutError:
            queries.bump_investigation_parse_failure(job_id, claim_id, "investigator timeout")
            _release_or_suppress_parse_failure(
                job_id,
                claim_id,
                "investigator_timeout",
                "investigator timed out 3 times",
            )
        except investigator.TransientInvestigatorError:
            queries.release_job_claim(job_id, claim_id)
        except Exception as e:
            logger.exception("investigator crashed job=%s", job_id)
            queries.mark_job_failed_if_claimed(job_id, claim_id, str(e))
    return len(bundles)


async def run_forever() -> None:
    while True:
        try:
            await process_once(limit=5)
            await asyncio.sleep(settings.investigator_loop_interval_seconds)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("investigator loop iteration failed")
            await asyncio.sleep(60)
