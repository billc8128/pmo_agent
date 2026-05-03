"""Create and persist the bot-owned Feishu PMO workspace.

Usage from the bot directory:
    python -m scripts.bootstrap_bot_workspace
"""
from __future__ import annotations

import asyncio
import logging

from db import queries
from feishu import bitable, calendar, drive


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> int:
    existing = queries.get_bot_workspace()
    if existing:
        logger.info("bot_workspace already bootstrapped: %s", existing)
        return 0

    lock = queries.acquire_bootstrap_lock()
    if not lock:
        logger.error("another process holds the bootstrap lock; refusing to proceed")
        return 1

    try:
        calendar_id = await calendar.create_calendar(summary="包工头的日历")
        base_app_token, action_items_table_id, meetings_table_id = await bitable.bootstrap_base()
        docs_folder_token = await drive.create_folder("包工头的文档柜")
        queries.upsert_bot_workspace(
            calendar_id=calendar_id,
            base_app_token=base_app_token,
            action_items_table_id=action_items_table_id,
            meetings_table_id=meetings_table_id,
            docs_folder_token=docs_folder_token,
        )
        logger.info("bot_workspace persisted")
        return 0
    finally:
        queries.release_bootstrap_lock(lock["id"])


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
