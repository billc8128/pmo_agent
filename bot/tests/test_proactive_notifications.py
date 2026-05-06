from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from feishu import events as feishu_events


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_stable_uuid_from_notif_is_stable_and_versioned():
    from feishu.client import stable_uuid_from_notif

    assert stable_uuid_from_notif(42, 1) == stable_uuid_from_notif(42, 1)
    assert stable_uuid_from_notif(42, 1) != stable_uuid_from_notif(42, 2)
    uuid.UUID(stable_uuid_from_notif(42, 1))


def test_claim_pending_notifications_parses_rpc_rows(monkeypatch):
    from db import queries

    calls: list[tuple[str, dict]] = []

    class _Rpc:
        def rpc(self, name: str, args: dict):
            calls.append((name, args))
            return self

        def execute(self):
            return SimpleNamespace(
                data=[
                    {
                        "notification": {
                            "id": 7,
                            "event_id": 12,
                            "subscription_id": "11111111-1111-1111-1111-111111111111",
                            "status": "claimed",
                            "decided_payload_version": 2,
                            "delivery_kind": "feishu_user",
                            "delivery_target": "ou_123",
                            "suppressed_by": None,
                            "payload_snapshot": {"turn_id": 99},
                        },
                        "notif_payload_snapshot": {"turn_id": 99},
                        "notif_payload_version": 2,
                        "subscription": {
                            "id": "11111111-1111-1111-1111-111111111111",
                            "scope_kind": "user",
                            "scope_id": "22222222-2222-2222-2222-222222222222",
                            "description": "vibelive 进展告诉我",
                            "enabled": True,
                        },
                    }
                ]
            )

    monkeypatch.setattr(queries, "sb_admin", lambda: _Rpc())

    bundles = queries.claim_pending_notifications("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", 20)

    assert calls == [
        (
            "claim_pending_notifications",
            {"p_claim_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", "p_limit": 20},
        )
    ]
    assert bundles[0].notification.id == 7
    assert bundles[0].notification.decided_payload_version == 2
    assert bundles[0].notif_payload_snapshot == {"turn_id": 99}
    assert bundles[0].subscription.description == "vibelive 进展告诉我"


def test_parse_message_event_exposes_parent_message_id():
    body = {
        "header": {"event_type": "im.message.receive_v1", "event_id": "evt_1"},
        "event": {
            "sender": {"sender_id": {"open_id": "ou_sender"}},
            "message": {
                "chat_id": "oc_chat",
                "chat_type": "p2p",
                "message_type": "text",
                "message_id": "om_child",
                "parent_id": "om_parent",
                "content": '{"text":"这次改动大不大?"}',
            },
        },
    }

    parsed = feishu_events.parse_message_event(body)

    assert parsed is not None
    assert parsed.parent_message_id == "om_parent"


def test_subscription_scope_defaults_to_chat_in_group():
    from agent.request_context import RequestContext
    from agent.tools_meta import _infer_subscription_scope

    ctx = RequestContext(
        chat_type="group",
        chat_id="oc_group",
        asker_user_id="22222222-2222-2222-2222-222222222222",
    )

    assert _infer_subscription_scope(ctx, None) == ("chat", "oc_group")


def test_fetch_subscriptions_for_scope_only_lists_enabled(monkeypatch):
    from db import queries

    eq_calls: list[tuple[str, object]] = []
    is_calls: list[tuple[str, object]] = []

    class _Table:
        def table(self, name):
            return self

        def select(self, *args, **kwargs):
            return self

        def eq(self, name, value):
            eq_calls.append((name, value))
            return self

        def is_(self, name, value):
            is_calls.append((name, value))
            return self

        def order(self, *args, **kwargs):
            return self

        def execute(self):
            return SimpleNamespace(data=[])

    monkeypatch.setattr(queries, "sb_admin", lambda: _Table())

    assert queries.fetch_subscriptions_for_scope("user", "22222222-2222-2222-2222-222222222222") == []
    assert ("enabled", True) in eq_calls
    assert ("archived_at", "null") in is_calls


def test_daily_sent_count_uses_exact_count_instead_of_limited_rows(monkeypatch):
    from db import queries

    select_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    filters: list[tuple[str, object]] = []

    class _Table:
        def table(self, name):
            assert name == "notifications"
            return self

        def select(self, *args, **kwargs):
            select_calls.append((args, kwargs))
            return self

        def eq(self, name, value):
            filters.append((name, value))
            return self

        def gte(self, name, value):
            filters.append((name, value))
            return self

        def execute(self):
            return SimpleNamespace(data=[], count=12345)

    monkeypatch.setattr(queries, "sb_admin", lambda: _Table())

    count = queries.daily_sent_count_for_scope("user", "profile-1", "2026-05-07T00:00:00+08:00")

    assert count == 12345
    assert select_calls[0][1]["count"] == "exact"
    assert ("subscriptions.scope_kind", "user") in filters
    assert ("subscriptions.scope_id", "profile-1") in filters
    assert ("status", "sent") in filters
    assert ("sent_at", "2026-05-07T00:00:00+08:00") in filters


def test_judge_prompt_states_self_events_send_by_default():
    from agent import decider

    prompt = decider._JUDGE_SYSTEM_PROMPT

    assert "is_subject_the_owner=true" in prompt
    assert "照常发" in prompt


def test_decider_usage_from_result_message_object_and_dict():
    from agent import decider

    assert decider._usage_from_result_message(
        SimpleNamespace(usage=SimpleNamespace(input_tokens=12, output_tokens=4))
    ) == (12, 4)
    assert decider._usage_from_result_message(
        SimpleNamespace(usage={"input_tokens": 20, "output_tokens": 5})
    ) == (20, 5)


def test_recent_decision_logs_batch_fetches_current_notifications(monkeypatch):
    from db import queries

    table_calls: list[str] = []

    class _Query:
        def __init__(self, table_name: str):
            self.table_name = table_name

        def select(self, *args, **kwargs):
            return self

        def eq(self, *args, **kwargs):
            return self

        def gte(self, *args, **kwargs):
            return self

        def in_(self, *args, **kwargs):
            return self

        def order(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

        def execute(self):
            if self.table_name == "decision_logs":
                return SimpleNamespace(
                    data=[
                        {"event_id": 1, "subscription_id": "sub-a", "judge_input": {}, "judge_output": {}},
                        {"event_id": 2, "subscription_id": "sub-b", "judge_input": {}, "judge_output": {}},
                    ]
                )
            if self.table_name == "notifications":
                return SimpleNamespace(
                    data=[
                        {"event_id": 1, "subscription_id": "sub-a", "status": "sent"},
                        {"event_id": 2, "subscription_id": "sub-b", "status": "suppressed"},
                    ]
                )
            raise AssertionError(f"unexpected table {self.table_name}")

    class _Client:
        def table(self, name: str):
            table_calls.append(name)
            return _Query(name)

    monkeypatch.setattr(queries, "sb_admin", lambda: _Client())
    monkeypatch.setattr(
        queries,
        "get_notification",
        lambda event_id, subscription_id: (_ for _ in ()).throw(AssertionError("N+1 lookup")),
    )

    rows = queries.recent_decision_logs_for_scope("user", "22222222-2222-2222-2222-222222222222")

    assert table_calls == ["decision_logs", "notifications"]
    assert [row["current_notification"]["status"] for row in rows] == ["sent", "suppressed"]


@pytest.mark.anyio
async def test_decider_prefetches_current_notifications_for_event_candidates(monkeypatch):
    from agent import decider_loop

    decided: list[str] = []
    marked: list[tuple[int, int]] = []
    fetched_pairs: list[set[tuple[int, str]]] = []

    monkeypatch.setattr(decider_loop, "build_scope_context", lambda scope_kind, scope_id: object())
    monkeypatch.setattr(decider_loop.queries, "lookup_profile_by_user_id", lambda user_id: None)
    monkeypatch.setattr(
        decider_loop.queries,
        "get_notification",
        lambda event_id, sub_id: (_ for _ in ()).throw(AssertionError("N+1 notification lookup")),
    )

    def fake_fetch(pairs):
        fetched_pairs.append(set(pairs))
        return {(9, "sub-sent"): {"status": "sent", "decided_payload_version": 1}}

    monkeypatch.setattr(decider_loop.queries, "fetch_notifications_for_event_subscription_pairs", fake_fetch)
    monkeypatch.setattr(decider_loop.lockout, "is_project_mismatch", lambda event, candidate: False)
    monkeypatch.setattr(decider_loop.queries, "write_decision_log", lambda **kwargs: None)
    monkeypatch.setattr(decider_loop.queries, "append_to_or_open_investigation_job", lambda *args, **kwargs: 55)
    monkeypatch.setattr(
        decider_loop.queries,
        "mark_event_processed",
        lambda event_id, version: marked.append((event_id, version)),
    )

    async def fake_decide(event, candidate, siblings, context):
        decided.append(candidate["id"])
        return SimpleNamespace(
            investigate=False,
            initial_focus="",
            reason="not relevant",
            raw_input={},
            raw_output={"investigate": False},
            model="test-model",
            latency_ms=1,
            input_tokens=None,
            output_tokens=None,
        )

    monkeypatch.setattr(decider_loop.decider, "decide", fake_decide)

    await decider_loop.process_event(
        {"id": 9, "payload_version": 1, "payload": {}},
        {
            ("user", "22222222-2222-2222-2222-222222222222"): [
                {
                    "id": "sub-sent",
                    "scope_kind": "user",
                    "scope_id": "22222222-2222-2222-2222-222222222222",
                    "description": "already sent",
                },
                {
                    "id": "sub-new",
                    "scope_kind": "user",
                    "scope_id": "22222222-2222-2222-2222-222222222222",
                    "description": "needs decision",
                },
            ]
        },
    )

    assert fetched_pairs == [{(9, "sub-sent"), (9, "sub-new")}]
    assert decided == ["sub-new"]
    assert marked == [(9, 1)]


@pytest.mark.anyio
async def test_decider_keeps_stale_claimed_event_unprocessed(monkeypatch):
    from agent import decider_loop

    marked: list[tuple[int, int]] = []

    monkeypatch.setattr(
        decider_loop.queries,
        "get_notification",
        lambda event_id, sub_id: {"status": "claimed", "decided_payload_version": 1},
    )
    monkeypatch.setattr(
        decider_loop.queries,
        "fetch_notifications_for_event_subscription_pairs",
        lambda pairs: {
            (9, "11111111-1111-1111-1111-111111111111"): {
                "status": "claimed",
                "decided_payload_version": 1,
            }
        },
    )
    monkeypatch.setattr(
        decider_loop.queries,
        "mark_event_processed",
        lambda event_id, version: marked.append((event_id, version)),
    )

    await decider_loop.process_event(
        {"id": 9, "payload_version": 2, "payload": {}},
        {
            ("user", "22222222-2222-2222-2222-222222222222"): [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "scope_kind": "user",
                    "scope_id": "22222222-2222-2222-2222-222222222222",
                    "description": "vibelive 进展告诉我",
                }
            ]
        },
    )

    assert marked == []


@pytest.mark.anyio
async def test_decider_caps_repeated_parse_failures(monkeypatch):
    from agent import decider, decider_loop

    logs: list[dict] = []
    upserts: list[dict] = []
    marked: list[tuple[int, int]] = []

    monkeypatch.setattr(decider_loop, "build_scope_context", lambda scope_kind, scope_id: object())
    monkeypatch.setattr(decider_loop.queries, "lookup_profile_by_user_id", lambda user_id: None)
    monkeypatch.setattr(decider_loop.queries, "get_notification", lambda event_id, sub_id: None)
    monkeypatch.setattr(decider_loop.queries, "fetch_notifications_for_event_subscription_pairs", lambda pairs: {})
    monkeypatch.setattr(decider_loop.queries, "judge_parse_failure_count", lambda event_id, sub_id, version: 2)
    monkeypatch.setattr(decider_loop.queries, "write_decision_log", lambda **kwargs: logs.append(kwargs))
    monkeypatch.setattr(
        decider_loop.queries,
        "upsert_notification_row",
        lambda **kwargs: upserts.append(kwargs) or "inserted",
    )
    monkeypatch.setattr(
        decider_loop.queries,
        "mark_event_processed",
        lambda event_id, version: marked.append((event_id, version)),
    )

    async def bad_decide(*args, **kwargs):
        raise decider.DecisionParseError("bad json", raw_text="not-json", raw_input={"event": {"id": 9}})

    monkeypatch.setattr(decider_loop.decider, "decide", bad_decide)

    await decider_loop.process_event(
        {"id": 9, "payload_version": 1, "payload": {"turn_id": 99}},
        {
            ("user", "22222222-2222-2222-2222-222222222222"): [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "scope_kind": "user",
                    "scope_id": "22222222-2222-2222-2222-222222222222",
                    "description": "vibelive 进展告诉我",
                }
            ]
        },
    )

    assert logs[0]["judge_output"]["suppressed_by"] == "gatekeeper_parse_error"
    assert logs[-1]["judge_output"]["suppressed_by"] == "gatekeeper_failure"
    assert upserts == []
    assert marked == [(9, 1)]


@pytest.mark.anyio
async def test_decider_does_not_mark_events_processed_when_no_subscriptions(monkeypatch):
    from agent import decider_loop

    marked: list[tuple[int, int]] = []

    monkeypatch.setattr(
        decider_loop.queries,
        "fetch_events_needing_decision",
        lambda limit=100: [{"id": 9, "payload_version": 1, "payload": {}}],
    )
    monkeypatch.setattr(decider_loop.queries, "fetch_all_enabled_subscriptions", lambda: [])
    monkeypatch.setattr(
        decider_loop.queries,
        "mark_event_processed",
        lambda event_id, version: marked.append((event_id, version)),
    )

    assert await decider_loop.process_once(limit=100) == 1
    assert marked == []


@pytest.mark.anyio
async def test_decider_skips_subscriptions_created_after_event(monkeypatch):
    from agent import decider_loop

    marked: list[tuple[int, int]] = []

    async def should_not_decide(*args, **kwargs):
        raise AssertionError("new subscriptions must not fan out to old events")

    monkeypatch.setattr(decider_loop.decider, "decide", should_not_decide)
    monkeypatch.setattr(decider_loop.queries, "lookup_profile_by_user_id", lambda user_id: None)
    monkeypatch.setattr(decider_loop.queries, "get_notification", lambda event_id, sub_id: None)
    monkeypatch.setattr(decider_loop.queries, "fetch_notifications_for_event_subscription_pairs", lambda pairs: {})
    monkeypatch.setattr(
        decider_loop.queries,
        "mark_event_processed",
        lambda event_id, version: marked.append((event_id, version)),
    )

    await decider_loop.process_event(
        {
            "id": 9,
            "payload_version": 2,
            "ingested_at": "2026-05-04T10:00:00+00:00",
            "payload": {},
        },
        {
            ("user", "22222222-2222-2222-2222-222222222222"): [
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "scope_kind": "user",
                    "scope_id": "22222222-2222-2222-2222-222222222222",
                    "description": "vibelive 进展告诉我",
                    "created_at": "2026-05-04T10:01:00+00:00",
                }
            ]
        },
    )

    assert marked == [(9, 2)]


@pytest.mark.anyio
async def test_delivery_releases_claim_on_unexpected_row_error(monkeypatch):
    from agent import delivery_loop
    from db.queries import ClaimedBundle, Notification, Subscription

    released: list[tuple[int, str]] = []
    claim_ids: list[str] = []

    bundle = ClaimedBundle(
        notification=Notification(
            id=7,
            event_id=9,
            subscription_id="11111111-1111-1111-1111-111111111111",
            status="claimed",
            decided_payload_version=1,
            delivery_kind="feishu_user",
            delivery_target="ou_123",
        ),
        notif_payload_snapshot={"turn_id": 99},
        notif_payload_version=1,
        subscription=Subscription(
            id="11111111-1111-1111-1111-111111111111",
            scope_kind="user",
            scope_id="22222222-2222-2222-2222-222222222222",
            description="vibelive 进展告诉我",
            enabled=True,
        ),
    )

    monkeypatch.setattr(delivery_loop.queries, "reap_stale_claims", lambda stale_after_minutes=5: 0)

    def fake_claim(claim_id: str, limit: int):
        claim_ids.append(claim_id)
        return [bundle]

    monkeypatch.setattr(delivery_loop.queries, "claim_pending_notifications", fake_claim)
    monkeypatch.setattr(
        delivery_loop.queries,
        "release_claim",
        lambda notification_id, claim_id: released.append((notification_id, claim_id)) or True,
    )

    async def boom(*args, **kwargs):
        raise RuntimeError("renderer broke")

    monkeypatch.setattr(delivery_loop.renderer, "render_notification", boom)

    await delivery_loop.process_once(limit=20)

    assert len(claim_ids) == 1
    assert released == [(7, claim_ids[0])]


@pytest.mark.anyio
async def test_delivery_transient_error_releases_without_inline_retry(monkeypatch):
    from agent import delivery_loop
    from db.queries import ClaimedBundle, Notification, Subscription
    from feishu.client import FeishuSendError

    released: list[tuple[int, str]] = []
    claim_ids: list[str] = []
    attempts = 0
    sleep_calls: list[float] = []

    bundle = ClaimedBundle(
        notification=Notification(
            id=9,
            event_id=9,
            subscription_id="11111111-1111-1111-1111-111111111111",
            status="claimed",
            decided_payload_version=1,
            delivery_kind="feishu_user",
            delivery_target="ou_123",
        ),
        notif_payload_snapshot={"turn_id": 99},
        notif_payload_version=1,
        subscription=Subscription(
            id="11111111-1111-1111-1111-111111111111",
            scope_kind="user",
            scope_id="22222222-2222-2222-2222-222222222222",
            description="vibelive 进展告诉我",
            enabled=True,
        ),
    )

    monkeypatch.setattr(delivery_loop.queries, "reap_stale_claims", lambda stale_after_minutes=5: 0)
    monkeypatch.setattr(
        delivery_loop.queries,
        "claim_pending_notifications",
        lambda claim_id, limit: claim_ids.append(claim_id) or [bundle],
    )
    monkeypatch.setattr(
        delivery_loop.queries,
        "release_claim",
        lambda notification_id, claim_id: released.append((notification_id, claim_id)) or True,
    )

    async def render(*args, **kwargs):
        return "notification text"

    async def transient_send(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        raise FeishuSendError("temporary", transient=True)

    async def no_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(delivery_loop.renderer, "render_notification", render)
    monkeypatch.setattr(delivery_loop, "_send_notification", transient_send)
    monkeypatch.setattr(delivery_loop.asyncio, "sleep", no_sleep)

    await delivery_loop.process_once(limit=20)

    assert attempts == 1
    assert sleep_calls == []
    assert released == [(9, claim_ids[0])]


@pytest.mark.anyio
async def test_delivery_marks_renderer_permanent_error_failed(monkeypatch):
    from agent import delivery_loop
    from db.queries import ClaimedBundle, Notification, Subscription

    failed: list[tuple[int, str, str]] = []
    released: list[tuple[int, str]] = []
    claim_ids: list[str] = []

    bundle = ClaimedBundle(
        notification=Notification(
            id=8,
            event_id=9,
            subscription_id="11111111-1111-1111-1111-111111111111",
            status="claimed",
            decided_payload_version=1,
            delivery_kind="feishu_user",
            delivery_target="ou_123",
        ),
        notif_payload_snapshot={"turn_id": 99},
        notif_payload_version=1,
        subscription=Subscription(
            id="11111111-1111-1111-1111-111111111111",
            scope_kind="user",
            scope_id="22222222-2222-2222-2222-222222222222",
            description="vibelive 进展告诉我",
            enabled=True,
        ),
    )

    monkeypatch.setattr(delivery_loop.queries, "reap_stale_claims", lambda stale_after_minutes=5: 0)

    def fake_claim(claim_id: str, limit: int):
        claim_ids.append(claim_id)
        return [bundle]

    monkeypatch.setattr(delivery_loop.queries, "claim_pending_notifications", fake_claim)
    monkeypatch.setattr(
        delivery_loop.queries,
        "mark_failed_if_claimed",
        lambda notification_id, claim_id, error: failed.append((notification_id, claim_id, error)) or True,
    )
    monkeypatch.setattr(
        delivery_loop.queries,
        "release_claim",
        lambda notification_id, claim_id: released.append((notification_id, claim_id)) or True,
    )

    async def empty_render(*args, **kwargs):
        raise delivery_loop.renderer.RenderError("renderer returned empty text")

    monkeypatch.setattr(delivery_loop.renderer, "render_notification", empty_render)

    await delivery_loop.process_once(limit=20)

    assert len(claim_ids) == 1
    assert failed == [(8, claim_ids[0], "renderer returned empty text")]
    assert released == []


@pytest.mark.anyio
async def test_delivery_marks_renderer_timeout_failed(monkeypatch):
    from agent import delivery_loop
    from db.queries import ClaimedBundle, Notification, Subscription

    failed: list[tuple[int, str, str]] = []
    released: list[tuple[int, str]] = []
    claim_ids: list[str] = []

    bundle = ClaimedBundle(
        notification=Notification(
            id=10,
            event_id=9,
            subscription_id="11111111-1111-1111-1111-111111111111",
            status="claimed",
            decided_payload_version=1,
            delivery_kind="feishu_user",
            delivery_target="ou_123",
        ),
        notif_payload_snapshot={"turn_id": 99},
        notif_payload_version=1,
        subscription=Subscription(
            id="11111111-1111-1111-1111-111111111111",
            scope_kind="user",
            scope_id="22222222-2222-2222-2222-222222222222",
            description="vibelive 进展告诉我",
            enabled=True,
        ),
    )

    monkeypatch.setattr(delivery_loop.queries, "reap_stale_claims", lambda stale_after_minutes=5: 0)
    monkeypatch.setattr(
        delivery_loop.queries,
        "claim_pending_notifications",
        lambda claim_id, limit: claim_ids.append(claim_id) or [bundle],
    )
    monkeypatch.setattr(
        delivery_loop.queries,
        "mark_failed_if_claimed",
        lambda notification_id, claim_id, error: failed.append((notification_id, claim_id, error)) or True,
    )
    monkeypatch.setattr(
        delivery_loop.queries,
        "release_claim",
        lambda notification_id, claim_id: released.append((notification_id, claim_id)) or True,
    )

    async def timeout_render(*args, **kwargs):
        raise asyncio.TimeoutError("renderer timed out")

    monkeypatch.setattr(delivery_loop.renderer, "render_notification", timeout_render)

    await delivery_loop.process_once(limit=20)

    assert len(claim_ids) == 1
    assert failed == [(10, claim_ids[0], "renderer timed out")]
    assert released == []
