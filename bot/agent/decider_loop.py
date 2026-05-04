from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, time as dt_time, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent import decider
from config import settings
from db import queries

logger = logging.getLogger(__name__)


def group_by_scope(subscriptions: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for sub in subscriptions:
        grouped[(sub.get("scope_kind"), sub.get("scope_id"))].append(sub)
    return dict(grouped)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "Asia/Shanghai")
    except ZoneInfoNotFoundError:
        return ZoneInfo("Asia/Shanghai")


def _local_midnight_iso(zone: ZoneInfo) -> str:
    now = datetime.now(zone)
    midnight = datetime.combine(now.date(), dt_time.min, tzinfo=zone)
    return midnight.astimezone(timezone.utc).isoformat()


def _scope_timezone(scope_kind: str, scope_id: str) -> str:
    if scope_kind != "user":
        return "Asia/Shanghai"
    linked = queries.feishu_link_for_user_id(scope_id)
    return (linked or {}).get("timezone") or "Asia/Shanghai"


def build_scope_context(scope_kind: str, scope_id: str) -> decider.ScopeContext:
    tz_name = _scope_timezone(scope_kind, scope_id)
    zone = _zone(tz_name)
    return decider.ScopeContext(
        owner_local_time=datetime.now(zone).isoformat(),
        owner_timezone=tz_name,
        owner_today_sent_count=queries.daily_sent_count_for_scope(
            scope_kind,
            scope_id,
            _local_midnight_iso(zone),
        ),
        recent_notifications=queries.recent_notifications_for_scope(scope_kind, scope_id, since_minutes=30),
    )


def _delivery_for_subscription(sub: dict[str, Any]) -> tuple[str | None, str | None]:
    scope_kind = sub.get("scope_kind")
    scope_id = sub.get("scope_id")
    if scope_kind == "chat":
        return "feishu_chat", scope_id
    if scope_kind == "user":
        linked = queries.feishu_link_for_user_id(scope_id)
        if linked and linked.get("open_id"):
            return "feishu_user", linked["open_id"]
    return None, None


def _parse_failure_output(exc: decider.DecisionParseError, previous_failures: int) -> dict[str, Any]:
    return {
        "send": False,
        "suppressed_by": "judge_parse_error",
        "reason": str(exc),
        "raw_text": exc.raw_text[:2000],
        "consecutive_failure_count": previous_failures + 1,
    }


async def process_event(
    event: dict[str, Any],
    subs_by_scope: dict[tuple[str, str], list[dict[str, Any]]],
) -> None:
    event_id = int(event["id"])
    if event.get("user_id") and not event.get("subject_profile"):
        event = {**event, "subject_profile": queries.lookup_profile_by_user_id(event["user_id"])}
    decided_version = int(event.get("payload_version") or 1)
    had_unhandled_error = False
    had_blocking_claim = False
    context_cache: dict[tuple[str, str], decider.ScopeContext] = {}

    for scope_key, scope_subs in subs_by_scope.items():
        scope_kind, scope_id = scope_key
        for candidate in scope_subs:
            sub_id = candidate.get("id")
            existing = queries.get_notification(event_id, sub_id)
            if existing and _row_value(existing, "status") == "sent":
                continue
            if existing and _row_value(existing, "status") == "claimed":
                if int(_row_value(existing, "decided_payload_version") or 0) < decided_version:
                    had_blocking_claim = True
                continue
            if existing and int(_row_value(existing, "decided_payload_version") or 0) >= decided_version:
                continue

            try:
                if scope_key not in context_cache:
                    context_cache[scope_key] = build_scope_context(scope_kind, scope_id)
                siblings = [s for s in scope_subs if s.get("id") != sub_id]
                decision = await decider.decide(event, candidate, siblings, context_cache[scope_key])
                delivery_kind, delivery_target = None, None
                if decision.send:
                    delivery_kind, delivery_target = _delivery_for_subscription(candidate)
                    if not delivery_target:
                        decision.send = False
                        decision.suppressed_by = "no_delivery_target"
                        decision.reason = "订阅 owner 当前没有可用的飞书投递目标"
                        decision.raw_output = {
                            **decision.raw_output,
                            "send": False,
                            "suppressed_by": "no_delivery_target",
                            "reason": decision.reason,
                        }
                        delivery_kind, delivery_target = None, None
                queries.write_decision_log(
                    event_id=event_id,
                    subscription_id=sub_id,
                    payload_version=decided_version,
                    judge_input=decision.raw_input,
                    judge_output=decision.raw_output,
                    model=decision.model,
                    latency_ms=decision.latency_ms,
                    input_tokens=decision.input_tokens,
                    output_tokens=decision.output_tokens,
                )
                queries.upsert_notification_row(
                    event_id=event_id,
                    subscription_id=sub_id,
                    decision=decision,
                    decided_payload_version=decided_version,
                    payload_snapshot=event.get("payload") or {},
                    delivery_kind=delivery_kind,
                    delivery_target=delivery_target,
                )
            except asyncio.CancelledError:
                raise
            except decider.DecisionParseError as e:
                previous_failures = queries.judge_parse_failure_count(event_id, sub_id, decided_version)
                judge_output = _parse_failure_output(e, previous_failures)
                queries.write_decision_log(
                    event_id=event_id,
                    subscription_id=sub_id,
                    payload_version=decided_version,
                    judge_input=e.raw_input,
                    judge_output=judge_output,
                    model=settings.anthropic_model,
                    latency_ms=e.latency_ms,
                )
                if previous_failures >= 2:
                    queries.upsert_notification_row(
                        event_id=event_id,
                        subscription_id=sub_id,
                        decision={
                            "send": False,
                            "suppressed_by": "judge_failure",
                            "reason": "judge output parse failed 3 consecutive times",
                        },
                        decided_payload_version=decided_version,
                        payload_snapshot=event.get("payload") or {},
                        delivery_kind=None,
                        delivery_target=None,
                    )
                    continue
                had_unhandled_error = True
            except Exception:
                logger.exception("decider error event=%s sub=%s", event_id, sub_id)
                had_unhandled_error = True

    if not had_unhandled_error and not had_blocking_claim:
        queries.mark_event_processed(event_id, decided_version)


async def process_once(limit: int = 100) -> int:
    events = queries.fetch_events_needing_decision(limit=limit)
    if not events:
        return 0
    subs_by_scope = group_by_scope(queries.fetch_all_enabled_subscriptions())
    if not subs_by_scope:
        return len(events)
    for ev in events:
        await process_event(ev, subs_by_scope)
    return len(events)


async def run_forever() -> None:
    while True:
        try:
            await process_once(limit=100)
            await asyncio.sleep(settings.decider_loop_interval_seconds)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("decider loop iteration failed")
            await asyncio.sleep(60)
