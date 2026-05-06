from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace


def _github_pr_payload(**overrides):
    payload = {
        "action": "closed",
        "pull_request": {
            "number": 42,
            "title": "Ship RTC media verification",
            "body": "Adds Agora RTC media-flow verification for vibelive.",
            "html_url": "https://github.com/billc8128/vibelive/pull/42",
            "diff_url": "https://github.com/billc8128/vibelive/pull/42.diff",
            "base": {"ref": "main"},
            "head": {"ref": "rtc-media-check"},
            "merged": True,
            "merged_at": "2026-05-06T06:30:00Z",
            "created_at": "2026-05-06T05:00:00Z",
            "changed_files": 3,
            "additions": 120,
            "deletions": 15,
        },
        "repository": {
            "full_name": "billc8128/vibelive",
            "default_branch": "main",
        },
        "sender": {"login": "hellobit", "id": 12345},
    }
    payload.update(overrides)
    return payload


def test_github_pr_normalizer_extracts_merge_signal_and_excludes_raw(monkeypatch):
    from external import normalizer

    lookups: list[tuple[str, str, str | None]] = []

    payload = _github_pr_payload()
    payload["sender"] = {"login": "HelloBit", "id": 12345}

    def fake_lookup(provider, login, external_id=None):
        lookups.append((provider, login, external_id))
        return "profile-hellobit" if (provider, login, external_id) == ("github", "hellobit", "12345") else None

    monkeypatch.setattr(
        normalizer.queries,
        "lookup_profile_by_external_login",
        fake_lookup,
    )
    monkeypatch.setattr(
        normalizer.queries,
        "lookup_project_root_for_repo",
        lambda provider, repo: "/Users/a/Desktop/vibelive"
        if (provider, repo) == ("github", "billc8128/vibelive")
        else None,
    )

    normalized = normalizer.normalize_github("pull_request", payload)

    assert normalized["event_type"] == "pull_request"
    assert normalized["action"] == "merged"
    assert normalized["occurred_at"] == "2026-05-06T06:30:00Z"
    assert normalized["project_root"] == "/Users/a/Desktop/vibelive"
    assert normalized["pr"]["merged"] is True
    assert normalized["pr"]["number"] == 42
    assert normalized["repo"]["full_name"] == "billc8128/vibelive"
    assert normalized["actor"] == {
        "login": "hellobit",
        "id": "12345",
        "profile_id": "profile-hellobit",
    }
    assert ("github", "hellobit", "12345") in lookups
    assert "raw" not in normalized


def test_issue_comment_normalizer_resolves_at_mentions(monkeypatch):
    from external import normalizer

    def fake_lookup(provider, login, external_id=None):
        if (provider, login) == ("github", "hellobit"):
            return "profile-hellobit"
        return None

    monkeypatch.setattr(normalizer.queries, "lookup_profile_by_external_login", fake_lookup)
    monkeypatch.setattr(normalizer.queries, "lookup_project_root_for_repo", lambda *args, **kwargs: "/repo/vibelive")

    normalized = normalizer.normalize_github(
        "issue_comment",
        {
            "action": "created",
            "comment": {
                "body": "cc @HelloBit and @ghost-user",
                "html_url": "https://github.com/billc8128/vibelive/issues/1#issuecomment-1",
                "created_at": "2026-05-06T08:00:00Z",
            },
            "issue": {"number": 1, "title": "RTC check"},
            "repository": {"full_name": "billc8128/vibelive"},
            "sender": {"login": "reviewer", "id": 456},
        },
    )

    assert normalized["mentioned_profile_ids"] == ["profile-hellobit"]


def test_build_judge_event_for_pull_request_projection():
    from agent.decider import build_judge_event

    projected = build_judge_event(
        {
            "event_type": "pull_request",
            "action": "merged",
            "project_root": "/Users/a/Desktop/vibelive",
            "occurred_at": "2026-05-06T06:30:00Z",
            "pr": {
                "number": 42,
                "title": "Ship RTC media verification",
                "body": "Adds Agora RTC media-flow verification for vibelive.",
                "merged": True,
            },
            "repo": {"full_name": "billc8128/vibelive"},
            "actor": {"login": "hellobit", "profile_id": "profile-hellobit"},
            "raw": {"large": "must not leak"},
        }
    )

    assert projected["event_type"] == "pull_request"
    assert projected["merged"] is True
    assert projected["pr_number"] == 42
    assert projected["actor_handle"] == "hellobit"
    assert projected["project_root"] == "/Users/a/Desktop/vibelive"
    assert "Ship RTC media verification" in projected["headline"]
    assert "raw" not in projected


def test_build_judge_event_for_turn_is_unchanged_shape():
    from agent.decider import build_judge_event

    projected = build_judge_event(
        {
            "turn_id": "turn-1",
            "agent": "codex",
            "project_path": "/repo/vibelive/bot",
            "project_root": "/repo/vibelive",
            "user_message_at": "2026-05-06T06:00:00Z",
            "user_message": "帮我看一下播放器方案",
            "agent_summary": "整理 RTC 播放器方案",
            "agent_response_full": "长回复" * 400,
        }
    )

    assert projected["event_type"] == "turn"
    assert projected["turn_id"] == "turn-1"
    assert projected["project_root"] == "/repo/vibelive"
    assert projected["user_message"] == "帮我看一下播放器方案"
    assert projected["agent_summary"] == "整理 RTC 播放器方案"
    assert len(projected["agent_response_excerpt"]) <= 600


def test_build_judge_event_for_unknown_source_falls_back_without_raw():
    from agent.decider import build_judge_event

    projected = build_judge_event(
        {
            "event_type": "workflow_run",
            "project_root": "/repo/vibelive",
            "raw": {"large": "must not leak"},
        }
    )

    assert projected == {
        "event_type": "workflow_run",
        "headline": "(unrecognised event source)",
        "project_root": "/repo/vibelive",
    }


def test_ingest_archives_ignored_event_without_upserting(monkeypatch):
    from external import ingest

    calls: list[tuple[str, object]] = []

    class FakeQueries:
        @staticmethod
        def archive_external_delivery(**kwargs):
            calls.append(("archive", kwargs))
            return 7

        @staticmethod
        def upsert_event(**kwargs):
            calls.append(("upsert", kwargs))
            return 99

        @staticmethod
        def link_archive_to_event(*args):
            calls.append(("link", args))

    monkeypatch.setattr(ingest, "queries", FakeQueries)

    asyncio.run(
        ingest.ingest_external_event(
            provider="github",
            event_type="workflow_run",
            delivery_id="delivery-1",
            payload={"action": "completed"},
            raw_bytes=b'{"action":"completed"}',
            headers={"x-github-delivery": "delivery-1", "x-hub-signature-256": "secret"},
        )
    )

    assert calls == [
        (
            "archive",
            {
                "provider": "github",
                "delivery_id": "delivery-1",
                "event_type": "workflow_run",
                "raw_body": {"action": "completed"},
                "raw_headers": {"x-github-delivery": "delivery-1"},
            },
        )
    ]


def test_ingest_upserts_event_and_links_archive(monkeypatch):
    from external import ingest, normalizer

    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(normalizer.queries, "lookup_profile_by_external_login", lambda *args, **kwargs: None)
    monkeypatch.setattr(normalizer.queries, "lookup_project_root_for_repo", lambda *args, **kwargs: "/repo/vibelive")

    class FakeQueries:
        @staticmethod
        def archive_external_delivery(**kwargs):
            calls.append(("archive", kwargs))
            return 7

        @staticmethod
        def upsert_event(**kwargs):
            calls.append(("upsert", kwargs))
            return 99

        @staticmethod
        def link_archive_to_event(*args):
            calls.append(("link", args))

    monkeypatch.setattr(ingest, "queries", FakeQueries)

    asyncio.run(
        ingest.ingest_external_event(
            provider="github",
            event_type="pull_request",
            delivery_id="delivery-2",
            payload=_github_pr_payload(),
            raw_bytes=b"{}",
            headers={},
        )
    )

    assert calls[0][0] == "archive"
    assert calls[1][0] == "upsert"
    upsert = calls[1][1]
    assert upsert["source"] == "github"
    assert upsert["source_id"] == "pull_request:billc8128/vibelive:42"
    assert upsert["project_root"] == "/repo/vibelive"
    assert upsert["payload"]["event_type"] == "pull_request"
    assert calls[2] == ("link", (7, 99))


def test_ingest_uses_resource_stable_source_id_across_deliveries(monkeypatch):
    from external import ingest, normalizer

    upserts: list[dict[str, object]] = []

    monkeypatch.setattr(normalizer.queries, "lookup_profile_by_external_login", lambda *args, **kwargs: None)
    monkeypatch.setattr(normalizer.queries, "lookup_project_root_for_repo", lambda *args, **kwargs: "/repo/vibelive")

    class FakeQueries:
        @staticmethod
        def archive_external_delivery(**kwargs):
            return 7

        @staticmethod
        def upsert_event(**kwargs):
            upserts.append(kwargs)
            return 99

        @staticmethod
        def link_archive_to_event(*args):
            return None

    monkeypatch.setattr(ingest, "queries", FakeQueries)

    for delivery_id, title in [("delivery-a", "First title"), ("delivery-b", "Edited title")]:
        payload = _github_pr_payload()
        payload["pull_request"]["title"] = title
        asyncio.run(
            ingest.ingest_external_event(
                provider="github",
                event_type="pull_request",
                delivery_id=delivery_id,
                payload=payload,
                raw_bytes=b"{}",
                headers={},
            )
        )

    assert [row["source_id"] for row in upserts] == [
        "pull_request:billc8128/vibelive:42",
        "pull_request:billc8128/vibelive:42",
    ]


def test_source_id_for_external_events_uses_resource_identity_not_delivery():
    from external.ingest import source_id_for_event

    assert (
        source_id_for_event(
            {
                "event_type": "push",
                "repo": {"full_name": "billc8128/vibelive"},
                "ref": "refs/heads/main",
                "after": "abc123",
            }
        )
        == "push:billc8128/vibelive:refs/heads/main:abc123"
    )
    assert (
        source_id_for_event(
            {
                "event_type": "release",
                "repo": {"full_name": "billc8128/vibelive"},
                "release": {"tag_name": "v1.2.3"},
            }
        )
        == "release:billc8128/vibelive:v1.2.3"
    )
    assert (
        source_id_for_event(
            {
                "event_type": "issue_comment",
                "repo": {"full_name": "billc8128/vibelive"},
                "comment": {"id": 98765},
            }
        )
        == "issue_comment:billc8128/vibelive:98765"
    )


def test_stable_payload_fingerprint_ignores_delivery_and_lookup_fields():
    from external.ingest import payload_fingerprint

    base = {
        "event_type": "pull_request",
        "occurred_at": "2026-05-06T06:30:00Z",
        "ingested_at": "2026-05-06T06:31:00Z",
        "delivery_id": "a",
        "project_root": "/repo/vibelive",
        "mentioned_profile_ids": ["profile-a"],
        "pr": {"number": 42, "title": "A"},
        "repo": {"full_name": "billc8128/vibelive", "project_root": "/repo/vibelive"},
        "actor": {"login": "hellobit", "id": "12345", "profile_id": "profile-a"},
    }
    redelivery = {
        **base,
        "ingested_at": "2026-05-06T07:00:00Z",
        "delivery_id": "b",
        "project_root": "/repo/vibelive-new",
        "mentioned_profile_ids": ["profile-b"],
        "repo": {"full_name": "billc8128/vibelive", "project_root": "/repo/vibelive-new"},
        "actor": {"login": "hellobit", "id": "12345", "profile_id": "profile-b"},
    }
    changed = {**base, "pr": {"number": 42, "title": "B"}}

    assert payload_fingerprint(base) == payload_fingerprint(redelivery)
    assert payload_fingerprint(base) != payload_fingerprint(changed)


def test_link_external_identity_normalizes_login_before_write(monkeypatch):
    from db import queries

    class FakeTable:
        def __init__(self, name: str):
            self.name = name
            self.filters: list[tuple[str, object]] = []
            self.upsert_row: dict[str, object] | None = None

        def select(self, *_args):
            return self

        def eq(self, key, value):
            self.filters.append((key, value))
            return self

        def maybe_single(self):
            return self

        def upsert(self, row, on_conflict=None):
            self.upsert_row = row
            return self

        def execute(self):
            if self.upsert_row is not None:
                return SimpleNamespace(data=[self.upsert_row])
            return SimpleNamespace(data=None)

    tables: list[FakeTable] = []

    class FakeClient:
        def table(self, name):
            table = FakeTable(name)
            tables.append(table)
            return table

    monkeypatch.setattr(queries, "sb_admin", lambda: FakeClient())

    row = queries.link_external_identity("profile-1", "GitHub", "HelloBit", external_id=12345)

    assert row["provider"] == "github"
    assert row["external_login"] == "hellobit"
    assert ("external_login", "hellobit") in tables[0].filters


class _FakeRequest:
    def __init__(self, headers: dict[str, str], body: bytes):
        self.headers = headers
        self._body = body
        self.body_read = False

    async def body(self):
        self.body_read = True
        return self._body


def test_webhook_rejects_missing_content_length_without_reading_body(monkeypatch):
    from external import webhooks

    monkeypatch.setattr(webhooks.settings, "github_webhook_secret", "secret")
    body = json.dumps(_github_pr_payload()).encode("utf-8")
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    request = _FakeRequest(
        {
            "x-hub-signature-256": signature,
            "x-github-event": "pull_request",
            "x-github-delivery": "delivery-1",
        },
        body,
    )

    response = asyncio.run(webhooks._handle_webhook(request, "github"))

    assert response.status_code == 411
    assert request.body_read is False


def test_webhook_rejects_invalid_content_length_as_bad_request(monkeypatch):
    from external import webhooks

    monkeypatch.setattr(webhooks.settings, "github_webhook_secret", "secret")
    request = _FakeRequest(
        {
            "content-length": "not-a-number",
            "x-hub-signature-256": "sha256=abc",
            "x-github-event": "pull_request",
            "x-github-delivery": "delivery-1",
        },
        b"{}",
    )

    response = asyncio.run(webhooks._handle_webhook(request, "github"))

    assert response.status_code == 400
    assert request.body_read is False


def test_webhook_missing_secret_fails_before_signature_verification(monkeypatch):
    from external import webhooks

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("signature verifier should not run without a configured secret")

    monkeypatch.setattr(webhooks.settings, "github_webhook_secret", "")
    monkeypatch.setattr(webhooks, "_verify_github_signature", fail_if_called)
    body = json.dumps(_github_pr_payload()).encode("utf-8")
    request = _FakeRequest(
        {
            "content-length": str(len(body)),
            "x-hub-signature-256": "sha256=abc",
            "x-github-event": "pull_request",
            "x-github-delivery": "delivery-1",
        },
        body,
    )

    response = asyncio.run(webhooks._handle_webhook(request, "github"))

    assert response.status_code == 500


def test_gitea_signature_accepts_optional_sha256_prefix():
    from external.webhooks import _verify_gitea_signature

    body = b'{"ok":true}'
    digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()

    assert _verify_gitea_signature(body, digest, "secret") is True
    assert _verify_gitea_signature(body, f"sha256={digest}", "secret") is True


def test_meta_tools_do_not_expose_unverified_external_identity_self_claim():
    from agent.request_context import RequestContext
    from agent.tools_meta import build_meta_tools

    names = {tool_def.name for tool_def in build_meta_tools(RequestContext(asker_user_id="profile-1"))}

    assert "link_external_identity" not in names
    assert "unlink_external_identity" not in names
    assert "list_external_identities" not in names


def test_fetch_pr_files_returns_patch_excerpt_not_content_excerpt(monkeypatch):
    from external import fetch

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "filename": "docs/spec.md",
                    "status": "modified",
                    "additions": 3,
                    "deletions": 1,
                    "changes": 4,
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                }
            ]

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(fetch.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(fetch._fetch_pr_files_remote("github", "billc8128/vibelive", 42))

    assert result["files"][0]["patch_excerpt"] == "@@ -1 +1 @@\n-old\n+new"
    assert "content_excerpt" not in result["files"][0]


def test_renderer_mcp_does_not_expose_fetch_pr_files_tool():
    renderer_source = Path("bot/agent/renderer.py").read_text()

    assert "mcp__pmo_renderer__fetch_pr_files" not in renderer_source


def test_migration_0020_defines_external_sources_contracts():
    sql = Path("backend/supabase/migrations/0020_external_event_sources.sql").read_text()

    for table in [
        "public.external_identities",
        "public.external_repos",
        "public.external_webhook_deliveries",
        "public.external_resource_cache",
    ]:
        assert table in sql
    assert "add column if not exists payload_fingerprint text" in sql
    assert "extid_id_unique" in sql
    assert "where external_id is not null" in sql
    assert "webhook_delivery_unique unique (provider, delivery_id)" in sql
    assert "Nullable by design" in sql
    assert "excluded.payload_fingerprint is not null" in sql
    assert "enable row level security" in sql.lower()
    assert "grant all on public.external_webhook_deliveries to service_role" in sql


def test_register_external_repos_script_exists_with_dry_run_and_apply_modes():
    script = Path("backend/scripts/register_external_repos.mjs").read_text()

    assert "EXTERNAL_REPOS_JSON" in script
    assert "external_repos?on_conflict=provider,repo_full_name" in script
    assert "--apply" in script
    assert "dry-run only" in script
