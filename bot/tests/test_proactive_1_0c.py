from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from types import SimpleNamespace

import pytest


def test_subscription_dataclass_preserves_metadata_and_archived_at():
    from db import queries

    sub = queries._dataclass_from_row(
        queries.Subscription,
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "scope_kind": "user",
            "scope_id": "22222222-2222-2222-2222-222222222222",
            "description": "vibelive 播放器方案进展告诉我",
            "enabled": True,
            "created_by": "22222222-2222-2222-2222-222222222222",
            "chat_id": None,
            "created_at": "2026-05-06T00:00:00+00:00",
            "updated_at": "2026-05-06T00:00:00+00:00",
            "archived_at": None,
            "metadata": {
                "matched_projects": ["vibelive"],
                "project_tokens_hash": "abc123",
                "indexed_at": "2026-05-06T00:00:00+00:00",
            },
        },
    )

    assert sub.archived_at is None
    assert sub.metadata["matched_projects"] == ["vibelive"]


def test_lockout_last_segment_treats_trailing_slash_as_unknown():
    from agent import lockout

    assert lockout.last_segment(None) == ""
    assert lockout.last_segment("") == ""
    assert lockout.last_segment("/Users/a/vibelive") == "vibelive"
    assert lockout.last_segment("/Users/a/vibelive/") == ""


def test_lockout_refetches_metadata_when_project_token_hash_changes(monkeypatch):
    from agent import lockout

    calls: list[tuple[str, str]] = []
    sub = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        metadata={"matched_projects": ["vibelive"], "project_tokens_hash": "old"},
    )

    monkeypatch.setattr(lockout, "known_project_tokens", lambda: ({"vibelive"}, "new"))
    monkeypatch.setattr(
        lockout.queries,
        "index_subscription_metadata",
        lambda subscription_id: calls.append(("index", subscription_id)),
    )
    monkeypatch.setattr(
        lockout.queries,
        "get_subscription",
        lambda subscription_id: SimpleNamespace(
            id=subscription_id,
            metadata={"matched_projects": ["vibelive"], "project_tokens_hash": "new"},
        ),
    )

    assert lockout.is_project_mismatch({"project_root": "/Users/a/oneship"}, sub) is True
    assert calls == [("index", "11111111-1111-1111-1111-111111111111")]


def test_lockout_does_not_skip_when_subscription_has_no_project_lock(monkeypatch):
    from agent import lockout

    sub = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        metadata={"matched_projects": [], "project_tokens_hash": "hash"},
    )
    monkeypatch.setattr(lockout, "known_project_tokens", lambda: ({"vibelive"}, "hash"))

    assert lockout.is_project_mismatch({"project_root": "/Users/a/oneship"}, sub) is False


def test_lockout_accepts_payload_project_path_leaf_as_project_token(monkeypatch):
    from agent import lockout

    sub = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        metadata={"matched_projects": ["vibelive"], "project_tokens_hash": "hash"},
    )
    monkeypatch.setattr(lockout, "known_project_tokens", lambda: ({"vibe", "vibelive"}, "hash"))

    event = {
        "project_root": "/Users/castheart/Documents/vibe",
        "payload": {"project_path": "/Users/castheart/Documents/vibe/vibelive"},
    }

    assert lockout.is_project_mismatch(event, sub) is False


def test_lockout_still_rejects_unmatched_project_path_leaf(monkeypatch):
    from agent import lockout

    sub = SimpleNamespace(
        id="11111111-1111-1111-1111-111111111111",
        metadata={"matched_projects": ["vibelive"], "project_tokens_hash": "hash"},
    )
    monkeypatch.setattr(lockout, "known_project_tokens", lambda: ({"oneship", "vibelive"}, "hash"))

    event = {
        "project_root": "/Users/a/Desktop/oneship",
        "payload": {"project_path": "/Users/a/Desktop/oneship"},
    }

    assert lockout.is_project_mismatch(event, sub) is True


class _RpcStub:
    def __init__(self, data):
        self.calls: list[tuple[str, dict]] = []
        self._data = data

    def rpc(self, name, args):
        self.calls.append((name, args))
        return self

    def execute(self):
        return SimpleNamespace(data=self._data)

    def table(self, name):
        raise AssertionError(f"unexpected table call: {name}")


def test_investigation_rpc_wrappers_use_expected_names_and_args(monkeypatch):
    from db import queries

    rpc = _RpcStub(data=42)
    monkeypatch.setattr(queries, "sb_admin", lambda: rpc)

    assert queries.append_to_or_open_investigation_job(
        "11111111-1111-1111-1111-111111111111",
        9,
        "播放器方案",
        "订阅和事件相关",
        window_minutes=30,
    ) == 42
    assert queries.bump_investigation_parse_failure(42, "claim-1", "bad json") == 42
    queries.index_subscription_metadata("11111111-1111-1111-1111-111111111111")

    assert rpc.calls == [
        (
            "append_to_or_open_investigation_job",
            {
                "p_subscription_id": "11111111-1111-1111-1111-111111111111",
                "p_event_id": 9,
                "p_initial_focus": "播放器方案",
                "p_decider_reason": "订阅和事件相关",
                "p_window_minutes": 30,
            },
        ),
        (
            "bump_investigation_parse_failure",
            {"p_id": 42, "p_claim_id": "claim-1", "p_error": "bad json"},
        ),
        (
            "index_subscription_metadata",
            {"p_subscription_id": "11111111-1111-1111-1111-111111111111"},
        ),
    ]


def test_migration_0017_defines_required_1_0c_rpcs():
    from pathlib import Path

    sql = Path("backend/supabase/migrations/0017_investigation_jobs.sql").read_text()

    for name in [
        "append_to_or_open_investigation_job",
        "claim_investigatable_jobs",
        "create_notification_for_investigation_job",
        "mark_job_suppressed_if_claimed",
        "release_job_claim",
        "mark_job_failed_if_claimed",
        "reap_stale_job_claims",
        "bump_investigation_parse_failure",
        "index_subscription_metadata",
    ]:
        assert f"create or replace function public.{name}" in sql
        assert f"grant execute on function public.{name}" in sql


def _migration_sql() -> str:
    from pathlib import Path

    return Path("backend/supabase/migrations/0017_investigation_jobs.sql").read_text()


def test_migration_escapes_regex_metacharacters_for_project_tokens():
    sql = _migration_sql()

    assert "regexp_replace(token, '([\\\\^$.|?*+(){}\\[\\]])', E'\\\\\\\\\\\\1', 'g')" in sql
    assert "regexp_replace(token, '([\\\\\\^\\$\\.\\|\\?\\*\\+\\(\\)\\[\\]\\{\\}])', '\\\\\\1', 'g')" not in sql


def test_migration_notification_upsert_version_guard_is_strictly_newer():
    sql = _migration_sql()

    assert "excluded.decided_payload_version\n                  > public.notifications.decided_payload_version" in sql
    assert ">= public.notifications.decided_payload_version" not in sql


def test_migration_short_token_patterns_match_spec_contract():
    sql = _migration_sql()
    body = sql.split("create or replace function public.index_subscription_metadata", 1)[1]
    body = body.split("select coalesce((select jsonb_agg", 1)[0]

    for expected in [
        "'\\mproject[\\s\\-_:]*' || token_re || '\\M'",
        "'项目[\\s\\-_:''`\"]*' || token_re",
        "'`' || token_re || '`'",
        "'/' || token_re || '(/|$|[^a-z0-9])'",
        "'\"' || token_re || '\"'",
    ]:
        assert expected in body

    for widened_keyword in ["repo", "仓库", "代码库", "service", "服务", "应用", "app", "工程", "目录"]:
        assert widened_keyword not in body


def test_migration_qualifies_pgcrypto_digest_for_pinned_search_path():
    sql = _migration_sql()

    assert "extensions.digest(" in sql
    assert "encode(digest(" not in sql


def test_migration_0018_ignores_negative_project_examples():
    from pathlib import Path

    sql = Path("backend/supabase/migrations/0018_subscription_metadata_exclusions.sql").read_text()

    assert "match_desc_lower := regexp_replace" in sql
    assert "不要|别|禁止|排除|不通知|不关注|不看|除了" in sql
    assert "match_desc_lower ~ ('\\m' || token_re || '\\M')" in sql
    assert "perform public.index_subscription_metadata(sub.id)" in sql


def test_migration_0019_indexes_payload_project_path_tokens():
    from pathlib import Path

    sql = Path("backend/supabase/migrations/0019_project_path_tokens_for_lockout.sql").read_text()

    assert "payload->>'project_path'" in sql
    assert "payload->>'project_root'" in sql
    assert "project_root is the canonical grouping root" in sql
    assert "perform public.index_subscription_metadata(sub.id)" in sql


def _run_optional_psql(sql: str) -> str:
    db_url = os.environ.get("PROACTIVE_SQL_TEST_DATABASE_URL")
    if not db_url:
        pytest.skip("PROACTIVE_SQL_TEST_DATABASE_URL is not set")
    if not shutil.which("psql"):
        pytest.skip("psql is not installed")
    proc = subprocess.run(
        ["psql", db_url, "-v", "ON_ERROR_STOP=1", "-At", "-c", sql],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(proc.stderr)
    return proc.stdout.strip()


def test_sql_token_with_regex_metacharacters_matches_literally():
    out = _run_optional_psql(
        textwrap.dedent(
            """
            begin;
            insert into public.events (source, source_id, project_root, occurred_at, payload)
            values ('test', 'regex-meta-1', '/tmp/c++.proj', now(), '{}'::jsonb);

            insert into public.subscriptions (id, scope_kind, scope_id, description)
            values (
                'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1',
                'user',
                '22222222-2222-2222-2222-222222222222',
                'c++.proj 进展告诉我'
            );

            select public.index_subscription_metadata('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1'::uuid);
            select metadata->'matched_projects'
              from public.subscriptions
             where id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1'::uuid;
            rollback;
            """
        )
    )

    assert '["c++.proj"]' in out


def test_sql_project_metadata_ignores_negative_project_examples():
    out = _run_optional_psql(
        textwrap.dedent(
            """
            begin;
            insert into public.events (source, source_id, project_root, occurred_at, payload)
            values
              ('test', 'negative-example-vibelive', '/tmp/vibelive', now(), '{}'::jsonb),
              ('test', 'negative-example-oneship', '/tmp/oneship', now(), '{}'::jsonb);

            insert into public.subscriptions (id, scope_kind, scope_id, description)
            values (
                'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3',
                'user',
                '22222222-2222-2222-2222-222222222222',
                '只通知 vibelive 项目的进展，不要通知其他项目（如 oneship 等）'
            );

            select public.index_subscription_metadata('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3'::uuid);
            select metadata->'matched_projects'
              from public.subscriptions
             where id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa3'::uuid;
            rollback;
            """
        )
    )

    assert '["vibelive"]' in out
    assert "oneship" not in out


def test_sql_project_metadata_uses_payload_project_path_leaf():
    out = _run_optional_psql(
        textwrap.dedent(
            """
            begin;
            insert into public.events (source, source_id, project_root, occurred_at, payload)
            values (
                'test',
                'payload-project-path-leaf',
                '/Users/castheart/Documents/vibe',
                now(),
                '{"project_path": "/Users/castheart/Documents/vibe/vibelive"}'::jsonb
            );

            insert into public.subscriptions (id, scope_kind, scope_id, description)
            values (
                'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa4',
                'user',
                '22222222-2222-2222-2222-222222222222',
                'vibelive 项目进展'
            );

            select public.index_subscription_metadata('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa4'::uuid);
            select metadata->'matched_projects'
              from public.subscriptions
             where id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa4'::uuid;
            rollback;
            """
        )
    )

    assert '["vibelive"]' in out


def test_sql_project_tokens_hash_matches_sorted_token_hash():
    out = _run_optional_psql(
        textwrap.dedent(
            """
            begin;
            insert into public.events (source, source_id, project_root, occurred_at, payload)
            values
              ('test', 'hash-1', '/tmp/zeta', now(), '{}'::jsonb),
              ('test', 'hash-2', '/tmp/alpha', now(), '{}'::jsonb),
              ('test', 'hash-3', '/tmp/', now(), '{}'::jsonb);

            insert into public.subscriptions (id, scope_kind, scope_id, description)
            values (
                'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2',
                'user',
                '22222222-2222-2222-2222-222222222222',
                'alpha 和 zeta 进展'
            );

            select public.index_subscription_metadata('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2'::uuid);
            with tokens as (
                select distinct lower(regexp_replace(project_root, '^.*/', '')) as token
                  from public.events
                 where project_root is not null and project_root <> ''
            ), clean as (
                select token from tokens where token <> ''
            )
            select
                (select metadata->>'project_tokens_hash'
                   from public.subscriptions
                  where id = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2'::uuid)
                =
                substr(
                    encode(extensions.digest(coalesce(string_agg(token, '|' order by token), ''), 'sha256'), 'hex'),
                    1,
                    16
                )
              from clean;
            rollback;
            """
        )
    )

    assert "t" in out.splitlines()


def test_renderer_detects_1_0c_brief_shape():
    from agent import renderer

    assert renderer._is_1_0c_brief({"headline": "进展", "key_facts": ["事实"]}) is True
    assert renderer._is_1_0c_brief({"agent_summary": "旧事件摘要"}) is False


def test_renderer_1_0c_prompt_forbids_new_facts():
    from agent import renderer

    prompt = renderer._prompt_for_payload({"headline": "进展", "key_facts": ["事实"]})

    assert "investigator brief" in prompt
    assert "不能加入新事实" in prompt


def test_investigator_loop_filters_hallucinated_subject_user_ids():
    from agent import investigator_loop

    bundle = _investigation_bundle()
    brief = {
        "notify": True,
        "headline": "进展",
        "key_facts": ["事实"],
        "evidence_event_ids": [9],
        "subject_user_ids": [
            "33333333-3333-3333-3333-333333333333",
            "99999999-9999-9999-9999-999999999999",
        ],
    }

    assert investigator_loop._sanitize_brief_subjects(brief, bundle)["subject_user_ids"] == [
        "33333333-3333-3333-3333-333333333333"
    ]


def _subscription_row(**overrides):
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "scope_kind": "user",
        "scope_id": "22222222-2222-2222-2222-222222222222",
        "description": "vibelive 播放器方案进展告诉我",
        "enabled": True,
        "created_at": "2026-05-06T00:00:00+00:00",
        "metadata": {"matched_projects": [], "project_tokens_hash": "hash"},
    }
    row.update(overrides)
    return row


def _event_row(**overrides):
    row = {
        "id": 9,
        "payload_version": 2,
        "ingested_at": "2026-05-06T00:01:00+00:00",
        "project_root": "/Users/a/vibelive",
        "user_id": "33333333-3333-3333-3333-333333333333",
        "payload": {"turn_id": 99, "project_root": "/Users/a/vibelive"},
    }
    row.update(overrides)
    return row


@pytest.mark.anyio
async def test_decider_opens_investigation_job_instead_of_notification(monkeypatch):
    from agent import decider_loop

    logs: list[dict] = []
    jobs: list[dict] = []
    marked: list[tuple[int, int]] = []

    monkeypatch.setattr(decider_loop, "build_scope_context", lambda scope_kind, scope_id: object())
    monkeypatch.setattr(decider_loop.queries, "lookup_profile_by_user_id", lambda user_id: None)
    monkeypatch.setattr(decider_loop.queries, "get_notification", lambda event_id, sub_id: None)
    monkeypatch.setattr(decider_loop.queries, "write_decision_log", lambda **kwargs: logs.append(kwargs))
    monkeypatch.setattr(
        decider_loop.queries,
        "append_to_or_open_investigation_job",
        lambda *args, **kwargs: jobs.append({"args": args, "kwargs": kwargs}) or 77,
    )
    monkeypatch.setattr(
        decider_loop.queries,
        "mark_event_processed",
        lambda event_id, version: marked.append((event_id, version)),
    )
    monkeypatch.setattr(
        decider_loop.queries,
        "upsert_notification_row",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("decider must not write notifications in 1.0c")),
    )
    monkeypatch.setattr(
        decider_loop,
        "lockout",
        SimpleNamespace(is_project_mismatch=lambda event, sub: False),
        raising=False,
    )

    async def fake_decide(*args, **kwargs):
        return SimpleNamespace(
            investigate=True,
            initial_focus="播放器方案",
            reason="事件和订阅相关",
            raw_input={"event": {"id": 9}},
            raw_output={"investigate": True, "initial_focus": "播放器方案", "reason": "事件和订阅相关"},
            latency_ms=12,
            model="test-model",
            input_tokens=10,
            output_tokens=4,
        )

    monkeypatch.setattr(decider_loop.decider, "decide", fake_decide)

    await decider_loop.process_event(
        _event_row(),
        {("user", "22222222-2222-2222-2222-222222222222"): [_subscription_row()]},
    )

    assert jobs[0]["args"][:4] == (
        "11111111-1111-1111-1111-111111111111",
        9,
        "播放器方案",
        "事件和订阅相关",
    )
    assert logs[0]["investigation_job_id"] == 77
    assert logs[0]["judge_output"]["investigate"] is True
    assert marked == [(9, 2)]


@pytest.mark.anyio
async def test_decider_project_lockout_short_circuits_before_llm(monkeypatch):
    from agent import decider_loop

    logs: list[dict] = []
    marked: list[tuple[int, int]] = []

    monkeypatch.setattr(decider_loop.queries, "lookup_profile_by_user_id", lambda user_id: None)
    monkeypatch.setattr(decider_loop.queries, "get_notification", lambda event_id, sub_id: None)
    monkeypatch.setattr(decider_loop.queries, "write_decision_log", lambda **kwargs: logs.append(kwargs))
    monkeypatch.setattr(
        decider_loop.queries,
        "append_to_or_open_investigation_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("lockout must not open jobs")),
    )
    monkeypatch.setattr(
        decider_loop.queries,
        "mark_event_processed",
        lambda event_id, version: marked.append((event_id, version)),
    )
    monkeypatch.setattr(
        decider_loop,
        "lockout",
        SimpleNamespace(is_project_mismatch=lambda event, sub: True),
        raising=False,
    )

    async def should_not_decide(*args, **kwargs):
        raise AssertionError("lockout must run before LLM")

    monkeypatch.setattr(decider_loop.decider, "decide", should_not_decide)

    await decider_loop.process_event(
        _event_row(project_root="/Users/a/oneship"),
        {("user", "22222222-2222-2222-2222-222222222222"): [_subscription_row()]},
    )

    assert logs[0]["model"] == "deterministic_project_lockout"
    assert logs[0]["judge_output"]["reason"] == "project_root_lockout"
    assert logs[0]["judge_output"]["investigate"] is False
    assert logs[0]["input_tokens"] is None
    assert logs[0]["investigation_job_id"] is None
    assert marked == [(9, 2)]


@pytest.mark.anyio
async def test_decider_investigate_false_only_logs_and_settles(monkeypatch):
    from agent import decider_loop

    logs: list[dict] = []
    marked: list[tuple[int, int]] = []

    monkeypatch.setattr(decider_loop, "build_scope_context", lambda scope_kind, scope_id: object())
    monkeypatch.setattr(decider_loop.queries, "lookup_profile_by_user_id", lambda user_id: None)
    monkeypatch.setattr(decider_loop.queries, "get_notification", lambda event_id, sub_id: None)
    monkeypatch.setattr(decider_loop.queries, "write_decision_log", lambda **kwargs: logs.append(kwargs))
    monkeypatch.setattr(
        decider_loop.queries,
        "append_to_or_open_investigation_job",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("investigate=false must not open jobs")),
    )
    monkeypatch.setattr(
        decider_loop.queries,
        "mark_event_processed",
        lambda event_id, version: marked.append((event_id, version)),
    )
    monkeypatch.setattr(
        decider_loop,
        "lockout",
        SimpleNamespace(is_project_mismatch=lambda event, sub: False),
        raising=False,
    )

    async def fake_decide(*args, **kwargs):
        return SimpleNamespace(
            investigate=False,
            initial_focus="",
            reason="不是订阅要看的主题",
            raw_input={"event": {"id": 9}},
            raw_output={"investigate": False, "initial_focus": "", "reason": "不是订阅要看的主题"},
            latency_ms=12,
            model="test-model",
            input_tokens=10,
            output_tokens=4,
        )

    monkeypatch.setattr(decider_loop.decider, "decide", fake_decide)

    await decider_loop.process_event(
        _event_row(),
        {("user", "22222222-2222-2222-2222-222222222222"): [_subscription_row()]},
    )

    assert logs[0]["investigation_job_id"] is None
    assert logs[0]["judge_output"]["investigate"] is False
    assert marked == [(9, 2)]


def _investigation_bundle():
    from db.queries import InvestigatableJobBundle, InvestigationJob, Subscription

    return InvestigatableJobBundle(
        job=InvestigationJob(
            id=42,
            subscription_id="11111111-1111-1111-1111-111111111111",
            status="investigating",
            seed_event_ids=[9, 10],
        ),
        subscription=Subscription(
            id="11111111-1111-1111-1111-111111111111",
            scope_kind="user",
            scope_id="22222222-2222-2222-2222-222222222222",
            description="vibelive 播放器方案进展告诉我",
            enabled=True,
        ),
        events=[
            {
                "id": 9,
                "user_id": "33333333-3333-3333-3333-333333333333",
                "payload_version": 1,
                "project_root": "/Users/a/vibelive",
                "payload": {"agent_summary": "播放器方案开始调研"},
            },
            {
                "id": 10,
                "user_id": "33333333-3333-3333-3333-333333333333",
                "payload_version": 2,
                "project_root": "/Users/a/vibelive",
                "payload": {"agent_summary": "播放器方案进入实现"},
            },
        ],
    )


@pytest.mark.anyio
async def test_investigator_writes_notification_for_notify_true(monkeypatch):
    from agent import investigator_loop

    created: list[dict] = []
    monkeypatch.setattr(investigator_loop.queries, "reap_stale_job_claims", lambda: 0)
    monkeypatch.setattr(investigator_loop.queries, "claim_investigatable_jobs", lambda claim_id, limit, window_minutes=30: [_investigation_bundle()])
    monkeypatch.setattr(investigator_loop.queries, "feishu_link_for_user_id", lambda user_id: {"open_id": "ou_123"})
    monkeypatch.setattr(
        investigator_loop.queries,
        "create_notification_for_investigation_job",
        lambda **kwargs: created.append(kwargs) or 88,
    )

    async def fake_investigate(bundle):
        return (
            {
                "notify": True,
                "headline": "vibelive 播放器方案有进展",
                "key_facts": ["方案已经从调研进入实现"],
                "evidence_event_ids": [9, 10],
                "subject_user_ids": ["33333333-3333-3333-3333-333333333333"],
            },
            SimpleNamespace(input_tokens=100, output_tokens=20),
        )

    monkeypatch.setattr(investigator_loop.investigator, "investigate", fake_investigate)

    assert await investigator_loop.process_once(limit=5) == 1

    assert created[0]["job_id"] == 42
    assert created[0]["event_id"] == 10
    assert created[0]["decided_payload_version"] == 2
    assert created[0]["delivery_kind"] == "feishu_user"
    assert created[0]["delivery_target"] == "ou_123"
    assert created[0]["payload_snapshot"]["headline"] == "vibelive 播放器方案有进展"
    assert created[0]["input_tokens"] == 100
    assert created[0]["output_tokens"] == 20


@pytest.mark.anyio
async def test_investigator_suppresses_notify_false(monkeypatch):
    from agent import investigator_loop

    suppressed: list[dict] = []
    monkeypatch.setattr(investigator_loop.queries, "reap_stale_job_claims", lambda: 0)
    monkeypatch.setattr(investigator_loop.queries, "claim_investigatable_jobs", lambda claim_id, limit, window_minutes=30: [_investigation_bundle()])
    monkeypatch.setattr(
        investigator_loop.queries,
        "mark_job_suppressed_if_claimed",
        lambda *args, **kwargs: suppressed.append({"args": args, "kwargs": kwargs}) or True,
    )

    async def fake_investigate(bundle):
        return (
            {"notify": False, "suppressed_by": "not_enough_signal", "reason": "还没有实质进展"},
            SimpleNamespace(input_tokens=80, output_tokens=12),
        )

    monkeypatch.setattr(investigator_loop.investigator, "investigate", fake_investigate)

    assert await investigator_loop.process_once(limit=5) == 1

    assert suppressed[0]["args"][0] == 42
    assert suppressed[0]["args"][2] == {
        "notify": False,
        "suppressed_by": "not_enough_signal",
        "reason": "还没有实质进展",
        "subject_user_ids": [],
    }
    assert suppressed[0]["kwargs"] == {"input_tokens": 80, "output_tokens": 12}


@pytest.mark.anyio
async def test_investigator_parse_failure_budget_suppresses_after_three(monkeypatch):
    from agent import decider, investigator_loop

    bumped: list[tuple[int, str, str]] = []
    released: list[tuple[int, str]] = []
    suppressed: list[dict] = []

    monkeypatch.setattr(investigator_loop.queries, "reap_stale_job_claims", lambda: 0)
    monkeypatch.setattr(investigator_loop.queries, "claim_investigatable_jobs", lambda claim_id, limit, window_minutes=30: [_investigation_bundle()])
    monkeypatch.setattr(
        investigator_loop.queries,
        "bump_investigation_parse_failure",
        lambda job_id, claim_id, error: bumped.append((job_id, claim_id, error)) or 3,
    )
    monkeypatch.setattr(investigator_loop.queries, "investigation_parse_failure_count", lambda job_id: 3)
    monkeypatch.setattr(
        investigator_loop.queries,
        "release_job_claim",
        lambda job_id, claim_id: released.append((job_id, claim_id)) or True,
    )
    monkeypatch.setattr(
        investigator_loop.queries,
        "mark_job_suppressed_if_claimed",
        lambda *args, **kwargs: suppressed.append({"args": args, "kwargs": kwargs}) or True,
    )

    async def bad_investigate(bundle):
        raise decider.DecisionParseError("bad json", raw_text="x", raw_input={})

    monkeypatch.setattr(investigator_loop.investigator, "investigate", bad_investigate)

    assert await investigator_loop.process_once(limit=5) == 1

    assert bumped[0][0] == 42
    assert released == []
    assert suppressed[0]["args"][2]["suppressed_by"] == "investigator_parse_error"
