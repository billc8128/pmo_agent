from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse


_DOCX_RE = re.compile(r"^/(?:docx|doc)/([A-Za-z0-9]+)$")
_SHEET_RE = re.compile(r"^/sheets/([A-Za-z0-9]+)$")
_BASE_RE = re.compile(r"^/base/([A-Za-z0-9]+)$")
_WIKI_RE = re.compile(r"^/wiki/([A-Za-z0-9]+)$")


def parse_url(url: str) -> dict[str, str]:
    parsed = urlparse(url.strip())
    qs = parse_qs(parsed.query or "")

    if m := _DOCX_RE.match(parsed.path):
        return {"kind": "docx", "token": m.group(1)}
    if m := _SHEET_RE.match(parsed.path):
        out = {"kind": "sheet", "token": m.group(1)}
        if sheet_id := qs.get("sheet", [None])[0]:
            out["sheet_id"] = sheet_id
        return out
    if m := _BASE_RE.match(parsed.path):
        out = {"kind": "bitable", "app_token": m.group(1)}
        if table_id := qs.get("table", [None])[0]:
            out["table_id"] = table_id
        if view_id := qs.get("view", [None])[0]:
            out["view_id"] = view_id
        return out
    if m := _WIKI_RE.match(parsed.path):
        return {"kind": "wiki", "token": m.group(1)}

    return {"kind": "unknown", "url": url}
