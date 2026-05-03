from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from feishu import docx


def test_delete_blocks_maps_block_ids_to_current_child_indexes(monkeypatch):
    fake_children = [
        MagicMock(block_id="keep_a"),
        MagicMock(block_id="del_1"),
        MagicMock(block_id="del_2"),
        MagicMock(block_id="keep_b"),
        MagicMock(block_id="del_3"),
    ]
    fake_get_resp = MagicMock(success=lambda: True)
    fake_get_resp.data.items = fake_children
    fake_get_resp.data.has_more = False

    delete_requests = []
    fake_delete_resp = MagicMock(success=lambda: True)

    fake_client = MagicMock()
    fake_client.docx.v1.document_block_children.get = lambda req: fake_get_resp
    fake_client.docx.v1.document_block_children.batch_delete = (
        lambda req: delete_requests.append(req) or fake_delete_resp
    )
    monkeypatch.setattr(docx, "_lark_client", lambda: fake_client)

    out = asyncio.run(
        docx.delete_blocks(
            "doc_token_123", "root", ["del_1", "del_2", "already_missing", "del_3"]
        )
    )

    assert out == {"deleted": 3, "missing": 1}
    assert [(r.body.start_index, r.body.end_index) for r in delete_requests] == [
        (4, 5),
        (1, 3),
    ]
