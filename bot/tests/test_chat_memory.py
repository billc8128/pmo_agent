from __future__ import annotations

from pathlib import Path


def test_chat_redaction_patterns_cover_private_chat_shapes():
    from external.redaction import redact_text

    text = "\n".join(
        [
            "邮箱 albert@vibelive.com 的密码是 abc",
            "电话 13800138000 或 +86 13800138000",
            "身份证 11010519491231002X",
            "银行卡 6222020202020202020",
            "ssh root@1.2.3.4:22 password=abc123",
            "[asker] handle=@admin user_id=00000000-0000-0000-0000-000000000000",
            "[parent_notification] id=123",
            "[IMAGE:img_123]",
        ]
    )

    redacted, count = redact_text(text)

    assert count >= 8
    assert "albert@vibelive.com" not in redacted
    assert "13800138000" not in redacted
    assert "+86 13800138000" not in redacted
    assert "11010519491231002X" not in redacted
    assert "6222020202020202020" not in redacted
    assert "1.2.3.4:22" not in redacted
    assert "password=abc123" not in redacted
    assert "[asker]" not in redacted
    assert "[parent_notification]" not in redacted
    assert "[IMAGE:img_123]" not in redacted
    assert "[chat_memory_escaped_marker:asker]" in redacted


def test_migration_0024_creates_chat_memory_tables():
    sql = Path("backend/supabase/migrations/0024_chat_memory.sql").read_text()

    assert "create table if not exists public.chat_memory_settings" in sql
    assert "create table if not exists public.chat_memory_settings_history" in sql
    assert "create table if not exists public.chat_messages" in sql
    assert "create table if not exists public.people_memory" in sql
    assert "create table if not exists public.people_memory_updates" in sql
    assert "feishu_message_id" in sql
    assert "people_loop_cursor" in sql
    assert "content_metadata" in sql
    assert "sender_is_bot" in sql
    assert "edited_at" in sql
    assert "deleted_at" in sql
    assert "references public.chat_memory_settings(chat_id) on delete cascade" in sql
    assert "between 1 and 730" in sql
    assert "to_tsvector('simple', text_redacted)" in sql
    assert "people_memory_updates_source_time_idx" in sql
    assert "people_memory_updates_person_time_idx" in sql
    assert "comment on column public.chat_messages.redacted_payload" in sql
    assert "redacted" in sql
