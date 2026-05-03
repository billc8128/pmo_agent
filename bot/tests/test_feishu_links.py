from __future__ import annotations

from feishu import links


def test_parse_docx_url():
    assert links.parse_url("https://example.feishu.cn/docx/doxcnAAAA") == {
        "kind": "docx",
        "token": "doxcnAAAA",
    }


def test_parse_base_url_with_table_and_view():
    assert links.parse_url(
        "https://example.feishu.cn/base/bascnCCCC?table=tblD&view=vewE"
    ) == {
        "kind": "bitable",
        "app_token": "bascnCCCC",
        "table_id": "tblD",
        "view_id": "vewE",
    }


def test_parse_wiki_url():
    assert links.parse_url("https://example.feishu.cn/wiki/wikcnFFFF") == {
        "kind": "wiki",
        "token": "wikcnFFFF",
    }


def test_parse_unknown_url_returns_unknown():
    assert links.parse_url("https://example.com/random") == {
        "kind": "unknown",
        "url": "https://example.com/random",
    }
