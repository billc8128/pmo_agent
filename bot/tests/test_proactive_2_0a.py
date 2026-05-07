from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest


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
            "head": {"ref": "rtc-media-check", "sha": "head-sha-42"},
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
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("webhook project_root must not use global path mapping")),
    )
    monkeypatch.setattr(
        normalizer,
        "_clamp_occurred_at",
        lambda value, **_kwargs: str(value).replace("Z", "+00:00"),
    )

    normalized = normalizer.normalize_github("pull_request", payload)

    assert normalized["event_type"] == "pull_request"
    assert normalized["action"] == "merged"
    assert normalized["occurred_at"] == "2026-05-06T06:30:00+00:00"
    assert normalized["project_root"] == "github:billc8128/vibelive"
    assert normalized["pr"]["merged"] is True
    assert normalized["pr"]["number"] == 42
    assert normalized["repo"]["full_name"] == "billc8128/vibelive"
    assert normalized["repo"]["project_root"] == "github:billc8128/vibelive"
    assert normalized["actor"] == {
        "login": "hellobit",
        "id": "12345",
        "profile_id": "profile-hellobit",
        "is_bot": False,
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


def test_normalizer_clamps_untrusted_occurred_at_to_safe_window():
    from datetime import datetime, timezone
    from external.normalizer import _clamp_occurred_at

    now = datetime(2026, 5, 6, 12, 0, tzinfo=timezone.utc)

    assert _clamp_occurred_at("2026-05-06T12:30:00Z", now=now) == "2026-05-06T12:30:00+00:00"
    assert _clamp_occurred_at("2026-06-01T00:00:00Z", now=now) == "2026-05-06T13:00:00+00:00"
    assert _clamp_occurred_at("2026-04-01T00:00:00Z", now=now) == "2026-04-29T12:00:00+00:00"


def test_bot_actor_pull_request_is_ignored_before_event_upsert(monkeypatch):
    from external import ingest, normalizer

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(normalizer.queries, "lookup_profile_by_external_login", lambda *args, **kwargs: None)

    class FakeQueries:
        @staticmethod
        def archive_external_delivery(**kwargs):
            calls.append(("archive", kwargs))
            return 7

        @staticmethod
        def mark_archive_ignored(archive_id, reason):
            calls.append(("ignored", (archive_id, reason)))

        @staticmethod
        def upsert_event(**kwargs):
            calls.append(("upsert", kwargs))
            return 99

        @staticmethod
        def link_archive_to_event(*args):
            calls.append(("link", args))

    monkeypatch.setattr(ingest, "queries", FakeQueries)
    payload = _github_pr_payload(sender={"login": "dependabot[bot]", "id": 49699333, "type": "Bot"})

    asyncio.run(
        ingest.ingest_external_event(
            provider="github",
            event_type="pull_request",
            delivery_id="delivery-bot",
            payload=payload,
            raw_bytes=b"{}",
            headers={},
        )
    )

    assert calls == [
        ("archive", {
            "provider": "github",
            "delivery_id": "delivery-bot",
            "event_type": "pull_request",
            "raw_body": payload,
            "raw_headers": {},
        }),
        ("ignored", (7, "bot_actor")),
    ]


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

        @staticmethod
        def mark_archive_ignored(archive_id, reason):
            calls.append(("ignored", (archive_id, reason)))

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
        ),
        ("ignored", (7, "unsupported_event_type")),
    ]


def test_ingest_redacts_secret_like_strings_before_archive_and_event_upsert(monkeypatch):
    from external import ingest, normalizer

    calls: list[tuple[str, object]] = []
    secret = "ghp_1234567890abcdefghijklmnopqrstuvwxyz1234"
    payload = _github_pr_payload()
    payload["pull_request"]["body"] = f"token={secret}\npassword=hunter2"

    monkeypatch.setattr(normalizer.queries, "lookup_profile_by_external_login", lambda *args, **kwargs: None)

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

        @staticmethod
        def mark_archive_ignored(*args):
            calls.append(("ignored", args))

    monkeypatch.setattr(ingest, "queries", FakeQueries)

    asyncio.run(
        ingest.ingest_external_event(
            provider="github",
            event_type="pull_request",
            delivery_id="delivery-redact",
            payload=payload,
            raw_bytes=b"{}",
            headers={},
        )
    )

    archive_body = calls[0][1]["raw_body"]
    event_payload = calls[1][1]["payload"]
    serialized = json.dumps({"archive": archive_body, "event": event_payload})
    assert secret not in serialized
    assert "hunter2" not in serialized
    assert "[REDACTED]" in serialized


def test_redaction_covers_common_external_secret_shapes():
    from external.redaction import redact_text

    stripe_secret = "sk_" + "live_" + "1234567890abcdefghijklmnopqrstuvwxyz"
    samples = [
        "jwt=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        f"stripe={stripe_secret}",
        "auth=Bearer ghp_1234567890abcdefghijklmnopqrstuvwxyz1234",
        "db=postgres://user:pass@example.internal:5432/app",
        "hook=https://open.feishu.cn/open-apis/bot/v2/hook/12345678-1234-1234-1234-1234567890ab",
    ]

    redacted, count = redact_text("\n".join(samples))

    assert count >= len(samples)
    assert "eyJhbGciOi" not in redacted
    assert "sk_" + "live_" not in redacted
    assert "Bearer ghp_" not in redacted
    assert "postgres://user:pass" not in redacted
    assert "open-apis/bot/v2/hook" not in redacted


def test_ingest_upserts_event_and_links_archive(monkeypatch):
    from external import ingest, normalizer

    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(normalizer.queries, "lookup_profile_by_external_login", lambda *args, **kwargs: None)
    monkeypatch.setattr(normalizer.queries, "lookup_project_root_for_repo", lambda *args, **kwargs: "/repo/vibelive")
    monkeypatch.setattr(ingest.lockout, "invalidate_project_tokens_cache", lambda: calls.append(("lockout_invalidate", None)))

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

        @staticmethod
        def mark_archive_ignored(archive_id, reason):
            calls.append(("ignored", (archive_id, reason)))

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
    assert upsert["user_id"] is None
    assert upsert["project_root"] == "github:billc8128/vibelive"
    assert upsert["payload"]["event_type"] == "pull_request"
    assert upsert["payload"]["actor"]["profile_id"] is None
    assert calls[2] == ("link", (7, 99))
    assert calls[3] == ("lockout_invalidate", None)


def test_legitimate_dash_bot_login_is_not_treated_as_bot(monkeypatch):
    from external import normalizer

    monkeypatch.setattr(normalizer.queries, "lookup_profile_by_external_login", lambda *args, **kwargs: None)
    payload = _github_pr_payload(sender={"login": "happy-bot", "id": 12345, "type": "User"})

    normalized = normalizer.normalize_github("pull_request", payload)

    assert normalized["actor"]["login"] == "happy-bot"
    assert normalized["actor"]["is_bot"] is False


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

        @staticmethod
        def mark_archive_ignored(*args):
            raise AssertionError("valid resource events should not be marked ignored")

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


def test_ingest_marks_archive_ignored_when_resource_identity_is_missing(monkeypatch):
    from external import ingest, normalizer

    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(normalizer.queries, "lookup_profile_by_external_login", lambda *args, **kwargs: None)

    class FakeQueries:
        @staticmethod
        def archive_external_delivery(**kwargs):
            calls.append(("archive", kwargs))
            return 7

        @staticmethod
        def mark_archive_ignored(archive_id, reason):
            calls.append(("ignored", (archive_id, reason)))

        @staticmethod
        def upsert_event(**kwargs):
            calls.append(("upsert", kwargs))
            return 99

        @staticmethod
        def link_archive_to_event(*args):
            calls.append(("link", args))

    monkeypatch.setattr(ingest, "queries", FakeQueries)
    payload = _github_pr_payload()
    payload["pull_request"].pop("number")

    asyncio.run(
        ingest.ingest_external_event(
            provider="github",
            event_type="pull_request",
            delivery_id="delivery-missing-pr-number",
            payload=payload,
            raw_bytes=b"{}",
            headers={},
        )
    )

    assert calls[0][0] == "archive"
    assert calls[1] == ("ignored", (7, "missing_source_identity"))
    assert [call[0] for call in calls] == ["archive", "ignored"]


def test_ingest_logs_archive_only_reason(monkeypatch, caplog):
    from external import ingest

    class FakeQueries:
        @staticmethod
        def archive_external_delivery(**kwargs):
            return 7

        @staticmethod
        def mark_archive_ignored(*args):
            return None

        @staticmethod
        def upsert_event(**kwargs):
            raise AssertionError("unsupported events should not be upserted")

        @staticmethod
        def link_archive_to_event(*args):
            raise AssertionError("unsupported events should not be linked")

    monkeypatch.setattr(ingest, "queries", FakeQueries)

    with caplog.at_level(logging.INFO, logger="external.ingest"):
        asyncio.run(
            ingest.ingest_external_event(
                provider="github",
                event_type="workflow_run",
                delivery_id="delivery-log",
                payload={"action": "completed"},
                raw_bytes=b"{}",
                headers={},
            )
        )

    messages = [record.getMessage() for record in caplog.records]
    assert any("webhook.archive_ignored" in msg and "unsupported_event_type" in msg for msg in messages)


def test_ingest_marks_archive_ignored_on_processing_exception(monkeypatch, caplog):
    from external import ingest

    calls: list[tuple[str, object]] = []

    class FakeQueries:
        @staticmethod
        def archive_external_delivery(**kwargs):
            calls.append(("archive", kwargs))
            return 7

        @staticmethod
        def mark_archive_ignored(*args):
            calls.append(("ignored", args))

        @staticmethod
        def upsert_event(**kwargs):
            calls.append(("upsert", kwargs))
            return 99

        @staticmethod
        def link_archive_to_event(*args):
            calls.append(("link", args))

    monkeypatch.setattr(ingest, "queries", FakeQueries)
    monkeypatch.setattr(ingest, "normalize_github", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    with caplog.at_level(logging.ERROR, logger="external.ingest"):
        asyncio.run(
            ingest.ingest_external_event(
                provider="github",
                event_type="pull_request",
                delivery_id="delivery-processing-error",
                payload=_github_pr_payload(),
                raw_bytes=b"{}",
                headers={},
            )
        )

    assert calls[0][0] == "archive"
    assert calls[1] == ("ignored", (7, "ingest_error"))
    assert [call[0] for call in calls] == ["archive", "ignored"]
    assert any("webhook.archive_ignored" in record.getMessage() and "ingest_error" in record.getMessage() for record in caplog.records)


def test_ingest_swallows_audit_mark_failure_after_processing_exception(monkeypatch, caplog):
    from external import ingest

    calls: list[tuple[str, object]] = []

    class FakeQueries:
        @staticmethod
        def archive_external_delivery(**kwargs):
            calls.append(("archive", kwargs))
            return 7

        @staticmethod
        def mark_archive_ignored(*args):
            calls.append(("ignored", args))
            raise RuntimeError("db still down")

        @staticmethod
        def upsert_event(**kwargs):
            calls.append(("upsert", kwargs))
            return 99

        @staticmethod
        def link_archive_to_event(*args):
            calls.append(("link", args))

    monkeypatch.setattr(ingest, "queries", FakeQueries)
    monkeypatch.setattr(ingest, "normalize_github", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))

    with caplog.at_level(logging.WARNING, logger="external.ingest"):
        asyncio.run(
            ingest.ingest_external_event(
                provider="github",
                event_type="pull_request",
                delivery_id="delivery-processing-error",
                payload=_github_pr_payload(),
                raw_bytes=b"{}",
                headers={},
            )
        )

    assert calls[0][0] == "archive"
    assert calls[1] == ("ignored", (7, "ingest_error"))
    assert any("webhook.archive_ignore_mark_failed" in record.getMessage() for record in caplog.records)


def test_ingest_logs_sanitize_attacker_controlled_identifiers(monkeypatch, caplog):
    from external import ingest

    class FakeQueries:
        @staticmethod
        def archive_external_delivery(**kwargs):
            return 7

        @staticmethod
        def mark_archive_ignored(*args):
            return None

        @staticmethod
        def upsert_event(**kwargs):
            raise AssertionError("unsupported events should not be upserted")

        @staticmethod
        def link_archive_to_event(*args):
            raise AssertionError("unsupported events should not be linked")

    monkeypatch.setattr(ingest, "queries", FakeQueries)

    with caplog.at_level(logging.INFO, logger="external.ingest"):
        asyncio.run(
            ingest.ingest_external_event(
                provider="github",
                event_type="workflow_run\nforged",
                delivery_id="delivery-1\x1b[31m\nforged",
                payload={"action": "completed"},
                raw_bytes=b"{}",
                headers={},
            )
        )

    for record in caplog.records:
        message = record.getMessage()
        assert "\n" not in message
        assert "\x1b" not in message


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


def test_source_id_for_external_events_rejects_non_integer_resource_ids():
    from external.ingest import source_id_for_event

    assert source_id_for_event(
        {
            "event_type": "pull_request",
            "repo": {"full_name": "billc8128/vibelive"},
            "pr": {"number": "42/../../meta"},
        }
    ) is None
    assert source_id_for_event(
        {
            "event_type": "issue_comment",
            "repo": {"full_name": "billc8128/vibelive"},
            "comment": {"id": "98765\nforged"},
        }
    ) is None
    assert source_id_for_event(
        {
            "event_type": "pull_request",
            "repo": {"full_name": "billc8128/vibelive"},
            "pr": {"number": "42"},
        }
    ) == "pull_request:billc8128/vibelive:42"


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


def test_archive_external_delivery_uses_ignore_duplicates_to_preserve_raw_body(monkeypatch):
    from db import queries

    class FakeTable:
        def __init__(self):
            self.upsert_kwargs: dict[str, object] = {}

        def upsert(self, row, **kwargs):
            self.upsert_kwargs = kwargs
            return self

        def select(self, *_args):
            return self

        def execute(self):
            return SimpleNamespace(data=[{"id": 77}])

    table = FakeTable()

    class FakeClient:
        def table(self, name):
            assert name == "external_webhook_deliveries"
            return table

    monkeypatch.setattr(queries, "sb_admin", lambda: FakeClient())

    assert queries.archive_external_delivery(
        provider="github",
        delivery_id="delivery-1",
        event_type="pull_request",
        raw_body={"first": True},
        raw_headers={},
    ) == 77
    assert table.upsert_kwargs["on_conflict"] == "provider,delivery_id"
    assert table.upsert_kwargs["ignore_duplicates"] is True


def test_mark_archive_ignored_writes_audit_reason(monkeypatch):
    from db import queries

    calls: list[tuple[str, object]] = []

    class FakeTable:
        def update(self, row):
            calls.append(("update", row))
            return self

        def eq(self, key, value):
            calls.append(("eq", (key, value)))
            return self

        def is_(self, key, value):
            calls.append(("is", (key, value)))
            return self

        def execute(self):
            calls.append(("execute", None))
            return SimpleNamespace(data=None)

    class FakeClient:
        def table(self, name):
            assert name == "external_webhook_deliveries"
            return FakeTable()

    monkeypatch.setattr(queries, "sb_admin", lambda: FakeClient())

    queries.mark_archive_ignored(7, "missing_source_identity")

    assert calls[0][0] == "update"
    assert calls[0][1]["ignored_reason"] == "missing_source_identity"
    assert calls[1] == ("eq", ("id", 7))
    assert calls[2] == ("is", ("event_id", "null"))


def test_write_external_resource_reaps_expired_rows_and_rejects_large_content(monkeypatch):
    from db import queries

    calls: list[tuple[str, object]] = []

    class FakeTable:
        def delete(self):
            calls.append(("delete", None))
            return self

        def lt(self, key, value):
            calls.append(("lt", (key, value)))
            return self

        def upsert(self, row, **kwargs):
            calls.append(("upsert", (row, kwargs)))
            return self

        def execute(self):
            calls.append(("execute", None))
            return SimpleNamespace(data=None)

    class FakeClient:
        def table(self, name):
            assert name == "external_resource_cache"
            return FakeTable()

    monkeypatch.setattr(queries, "sb_admin", lambda: FakeClient())

    queries.write_external_resource("github", "pr_files", "repo#1", {"files": []})

    assert calls[0] == ("delete", None)
    assert calls[1][0] == "lt"
    assert calls[2][0] == "execute"
    assert calls[3][0] == "upsert"

    with pytest.raises(ValueError, match="external resource content too large"):
        queries.write_external_resource("github", "pr_files", "repo#big", {"patch": "x" * (1024 * 1024 + 1)})


def test_build_judge_event_strips_feishu_mentions_and_html_from_external_body():
    from agent.decider import build_judge_event

    projected = build_judge_event(
        {
            "event_type": "pull_request",
            "action": "opened",
            "project_root": "github:billc8128/vibelive",
            "repo": {"full_name": "billc8128/vibelive"},
            "actor": {"login": "hellobit"},
            "pr": {
                "number": 42,
                "title": "Media check",
                "body": 'hello <at user_id="ou_fake"></at> <b>ship</b> &amp; verify',
            },
        }
    )

    assert "<at" not in projected["body_excerpt"]
    assert "ou_fake" not in projected["body_excerpt"]
    assert "<b>" not in projected["body_excerpt"]
    assert "ship & verify" in projected["body_excerpt"]


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


def test_lookup_profile_by_external_login_falls_back_when_external_id_query_returns_none(monkeypatch):
    from db import queries

    class FakeTable:
        def __init__(self):
            self.filters: list[tuple[str, object]] = []

        def select(self, *_args):
            return self

        def eq(self, key, value):
            self.filters.append((key, value))
            return self

        def maybe_single(self):
            return self

        def execute(self):
            if ("external_id", "12345") in self.filters:
                return None
            if ("external_login", "hellobit") in self.filters:
                return SimpleNamespace(data={"profile_id": "profile-hellobit"})
            return SimpleNamespace(data=None)

    tables: list[FakeTable] = []

    class FakeClient:
        def table(self, name):
            assert name == "external_identities"
            table = FakeTable()
            tables.append(table)
            return table

    monkeypatch.setattr(queries, "sb_admin", lambda: FakeClient())

    assert queries.lookup_profile_by_external_login("Gitea", "HelloBit", external_id="12345") == "profile-hellobit"
    assert ("provider", "gitea") in tables[0].filters
    assert ("external_id", "12345") in tables[0].filters
    assert ("external_login", "hellobit") in tables[1].filters


def test_external_repo_helpers_normalize_repo_full_name(monkeypatch):
    from db import queries

    class FakeTable:
        def __init__(self):
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
            return SimpleNamespace(data={"project_root": "/repo/vibelive"})

    table = FakeTable()

    class FakeClient:
        def table(self, name):
            assert name == "external_repos"
            return table

    monkeypatch.setattr(queries, "sb_admin", lambda: FakeClient())

    row = queries.register_external_repo("GitHub", "BillC8128/VibeLive", "/repo/vibelive")
    assert row["provider"] == "github"
    assert row["repo_full_name"] == "billc8128/vibelive"

    table.upsert_row = None
    assert queries.lookup_project_root_for_repo("GitHub", "BillC8128/VibeLive") == "/repo/vibelive"
    assert ("provider", "github") in table.filters
    assert ("repo_full_name", "billc8128/vibelive") in table.filters


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


def test_webhook_logs_accepted_delivery(monkeypatch, caplog):
    from external import webhooks

    async def fake_ingest(**kwargs):
        return None

    monkeypatch.setattr(webhooks.settings, "github_webhook_secret", "secret")
    monkeypatch.setattr(webhooks, "ingest_external_event", fake_ingest)
    body = json.dumps(_github_pr_payload()).encode("utf-8")
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    request = _FakeRequest(
        {
            "content-length": str(len(body)),
            "x-hub-signature-256": signature,
            "x-github-event": "pull_request",
            "x-github-delivery": "delivery-log",
        },
        body,
    )

    with caplog.at_level(logging.INFO, logger="external.webhooks"):
        response = asyncio.run(webhooks._handle_webhook(request, "github"))

    assert response.status_code == 200
    assert any("webhook.accepted" in record.getMessage() for record in caplog.records)


def test_webhook_logs_sanitize_attacker_controlled_headers(monkeypatch, caplog):
    from external import webhooks

    async def fake_ingest(**kwargs):
        return None

    monkeypatch.setattr(webhooks.settings, "github_webhook_secret", "secret")
    monkeypatch.setattr(webhooks, "ingest_external_event", fake_ingest)
    body = json.dumps(_github_pr_payload()).encode("utf-8")
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    request = _FakeRequest(
        {
            "content-length": str(len(body)),
            "x-hub-signature-256": signature,
            "x-github-event": "pull_request\nforged",
            "x-github-delivery": "delivery-log\x1b[31m\nforged",
        },
        body,
    )

    with caplog.at_level(logging.INFO, logger="external.webhooks"):
        response = asyncio.run(webhooks._handle_webhook(request, "github"))

    assert response.status_code == 200
    for record in caplog.records:
        message = record.getMessage()
        assert "\n" not in message
        assert "\x1b" not in message


def test_webhook_catches_unexpected_ingest_exceptions(monkeypatch, caplog):
    from external import webhooks

    async def fake_ingest(**kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(webhooks.settings, "github_webhook_secret", "secret")
    monkeypatch.setattr(webhooks, "ingest_external_event", fake_ingest)
    body = json.dumps(_github_pr_payload()).encode("utf-8")
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    request = _FakeRequest(
        {
            "content-length": str(len(body)),
            "x-hub-signature-256": signature,
            "x-github-event": "pull_request",
            "x-github-delivery": "delivery-ingest-fail",
        },
        body,
    )

    with caplog.at_level(logging.ERROR, logger="external.webhooks"):
        response = asyncio.run(webhooks._handle_webhook(request, "github"))

    assert response.status_code == 500
    assert any("webhook.ingest_failed" in record.getMessage() for record in caplog.records)


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

    secret = "ghp_1234567890abcdefghijklmnopqrstuvwxyz1234"

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
                    "patch": f"@@ -1 +1 @@\n-old\n+token={secret}",
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

    assert secret not in result["files"][0]["patch_excerpt"]
    assert result["files"][0]["patch_excerpt"] == "@@ -1 +1 @@\n-old\n+token=[REDACTED]"
    assert "content_excerpt" not in result["files"][0]


def test_fetch_pr_files_redacts_full_patch_before_truncating(monkeypatch):
    from external import fetch

    secret = "ghp_1234567890abcdefghijklmnopqrstuvwxyz1234"
    prefix = ("x" * 174) + "token="

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return [{"filename": "src/app.ts", "patch": prefix + secret}]

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

    patch_excerpt = result["files"][0]["patch_excerpt"]
    assert "ghp_" not in patch_excerpt
    assert patch_excerpt == prefix + "[REDACTED]"


def test_fetch_pr_files_rejects_malformed_repo_before_building_url(monkeypatch):
    from external import fetch

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("invalid repo names must not reach http client")

    monkeypatch.setattr(fetch.httpx, "AsyncClient", FakeAsyncClient)

    with pytest.raises(ValueError, match="repo_full_name"):
        asyncio.run(fetch._fetch_pr_files_remote("github", "billc8128/vibelive/../../meta", 42))


def test_fetch_pr_files_cache_key_includes_head_sha(monkeypatch):
    from external import fetch

    lookups: list[str] = []
    writes: list[str] = []

    async def fake_remote(*_args, **_kwargs):
        return {"files": [], "count": 0}

    monkeypatch.setattr(fetch, "_fetch_pr_files_remote", fake_remote)
    monkeypatch.setattr(
        fetch.queries,
        "lookup_external_resource",
        lambda provider, kind, key: lookups.append(key) or None,
    )
    monkeypatch.setattr(
        fetch.queries,
        "write_external_resource",
        lambda provider, kind, key, content, ttl_seconds=86400: writes.append(key),
    )

    asyncio.run(fetch.fetch_pr_files("github", "billc8128/vibelive", 42, head_sha="abc123"))

    assert lookups == ["billc8128/vibelive/42/abc123"]
    assert writes == ["billc8128/vibelive/42/abc123"]


def test_fetch_pr_files_remote_retries_transient_http_errors(monkeypatch):
    from external import fetch

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return []

    class FakeAsyncClient:
        attempts = 0

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            type(self).attempts += 1
            if type(self).attempts == 1:
                raise fetch.httpx.ConnectError("temporary network error")
            return FakeResponse()

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(fetch.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(fetch.asyncio, "sleep", no_sleep)

    result = asyncio.run(fetch._fetch_pr_files_remote("github", "billc8128/vibelive", 42))

    assert result == {"files": [], "count": 0}
    assert FakeAsyncClient.attempts == 2


def test_fetch_pr_files_remote_retries_rate_limit(monkeypatch):
    from external import fetch

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise fetch.httpx.HTTPStatusError("rate limited", request=None, response=self)

        def json(self):
            return []

    class FakeAsyncClient:
        attempts = 0

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            type(self).attempts += 1
            return FakeResponse(429 if type(self).attempts == 1 else 200)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(fetch.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(fetch.asyncio, "sleep", no_sleep)

    result = asyncio.run(fetch._fetch_pr_files_remote("github", "billc8128/vibelive", 42))

    assert result == {"files": [], "count": 0}
    assert FakeAsyncClient.attempts == 2


def test_list_pull_requests_remote_normalizes_gitea_response(monkeypatch):
    from external import fetch

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return [
                {
                    "number": 88,
                    "title": "WIP: Keep Space Chat portable across IM backends",
                    "state": "open",
                    "html_url": "https://git.hoxigames.xyz/ailab/vibelive/pulls/88",
                    "created_at": "2026-05-07T04:00:00Z",
                    "updated_at": "2026-05-07T05:00:00Z",
                    "user": {"login": "bichenchen"},
                    "head": {"ref": "portable-chat", "sha": "head-sha"},
                    "base": {"ref": "main"},
                }
            ]

    class FakeAsyncClient:
        calls: list[dict[str, object]] = []

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, **kwargs):
            type(self).calls.append({"url": url, "kwargs": kwargs})
            return FakeResponse()

    monkeypatch.setattr(fetch.httpx, "AsyncClient", FakeAsyncClient)

    result = asyncio.run(fetch.list_pull_requests("Gitea", "AILAB/VibeLive", state="all", limit=1))

    assert result == {
        "provider": "gitea",
        "repo_full_name": "ailab/vibelive",
        "pull_requests": [
            {
                "number": 88,
                "title": "WIP: Keep Space Chat portable across IM backends",
                "state": "open",
                "url": "https://git.hoxigames.xyz/ailab/vibelive/pulls/88",
                "created_at": "2026-05-07T04:00:00Z",
                "updated_at": "2026-05-07T05:00:00Z",
                "merged_at": None,
                "closed_at": None,
                "actor_login": "bichenchen",
                "head_ref": "portable-chat",
                "head_sha": "head-sha",
                "base_ref": "main",
            }
        ],
        "count": 1,
    }
    assert FakeAsyncClient.calls[0]["url"].endswith("/repos/ailab/vibelive/pulls")
    assert FakeAsyncClient.calls[0]["kwargs"]["params"] == {
        "state": "all",
        "sort": "updated",
        "direction": "desc",
        "limit": 1,
    }


def test_investigator_fetch_pr_files_passes_head_sha_from_event(monkeypatch):
    source = Path("bot/agent/investigator.py").read_text()

    assert "head_sha=payload[\"pr\"].get(\"head_sha\")" in source


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
    assert "ignored_reason text" not in sql


def test_migration_0021_uses_repo_identifier_project_tokens():
    sql = Path("backend/supabase/migrations/0021_webhook_event_semantics.sql").read_text()

    assert "github:owner/repo" in sql
    assert "set user_id = null" in sql
    assert "where source in ('github', 'gitea')" in sql
    assert "lower(source || ':' || (payload #>> '{repo,full_name}'))" in sql
    assert "position(token in desc_lower) > 0" in sql
    assert "set search_path = public, pg_temp" in sql


def test_migration_0022_adds_delivery_ignore_audit_fields():
    sql = Path("backend/supabase/migrations/0022_external_delivery_audit.sql").read_text()

    assert "comment on column public.external_webhook_deliveries.raw_body" in sql
    assert "redacted parsed payload" in sql
    assert "row_number() over" in sql
    assert "partition by provider, lower(repo_full_name)" in sql
    assert "ranked_external_repos" in sql
    assert "ignored_reason text" in sql
    assert "ignored_at timestamptz" in sql
    assert "webhook_deliveries_ignored_idx" in sql
    assert "set repo_full_name = lower(repo_full_name)" in sql
    assert "external_repos_repo_full_name_lower" in sql
    assert "repo_full_name = lower(repo_full_name)" in sql


def test_register_external_repos_script_exists_with_dry_run_and_apply_modes():
    script = Path("backend/scripts/register_external_repos.mjs").read_text()

    assert "EXTERNAL_REPOS_JSON" in script
    assert "external_repos?on_conflict=provider,repo_full_name" in script
    assert "--apply" in script
    assert "dry-run only" in script
    assert "/Users/a/Desktop/vibelive" not in script
    assert "EXTERNAL_REPOS_JSON is required" in script
    assert "repoFullName = String(row.repo_full_name || row.repo || '').trim().toLowerCase()" in script
