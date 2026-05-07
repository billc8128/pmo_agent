from __future__ import annotations


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
