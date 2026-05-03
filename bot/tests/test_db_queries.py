from __future__ import annotations

from db import queries
from db.queries import _phone_variants


class _MaybeSingleNoneTable:
    def table(self, name):
        self.name = name
        return self

    def select(self, *args, **kwargs):
        return self

    def eq(self, *args, **kwargs):
        return self

    def maybe_single(self):
        return self

    def execute(self):
        return None


def test_get_bot_action_treats_maybe_single_none_as_missing(monkeypatch):
    monkeypatch.setattr(queries, "sb_admin", lambda: _MaybeSingleNoneTable())

    assert queries.get_bot_action("message-1", "schedule_meeting") is None


def test_phone_variants_generates_china_country_code_for_bare_11_digits():
    out = _phone_variants("13800138000")

    assert "+8613800138000" in out
    assert "8613800138000" in out
    assert "13800138000" in out


def test_phone_variants_strips_china_country_code_when_present():
    out = _phone_variants("+8613800138000")

    assert "13800138000" in out
    assert "+13800138000" in out
    assert "+8613800138000" in out
    assert "8613800138000" in out


def test_phone_variants_handles_dashes_and_spaces():
    out = _phone_variants("+86 138-0013-8000")

    assert "+8613800138000" in out
    assert "8613800138000" in out
    assert "+13800138000" in out
    assert "13800138000" in out


def test_phone_variants_empty_returns_empty_list():
    assert _phone_variants("") == []
    assert _phone_variants(None) == []
