from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from feishu import drive


def test_create_folder_at_root_uses_real_root_folder_token(monkeypatch):
    requests = []
    fake_resp = MagicMock(success=lambda: True)
    fake_resp.data = SimpleNamespace(token="fld_123")

    fake_client = MagicMock()
    fake_client.drive.v1.file.create_folder = lambda req: requests.append(req) or fake_resp
    monkeypatch.setattr(drive, "_lark_client", lambda: fake_client)
    monkeypatch.setattr(drive, "_root_folder_token", lambda: asyncio.sleep(0, result="fld_root"), raising=False)

    token = asyncio.run(drive.create_folder("包工头的文档柜"))

    assert token == "fld_123"
    assert requests[0].body.name == "包工头的文档柜"
    assert requests[0].body.folder_token == "fld_root"
