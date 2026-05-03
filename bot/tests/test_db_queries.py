from __future__ import annotations

from db.queries import _phone_variants


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
