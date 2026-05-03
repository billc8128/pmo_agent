"""Supabase client for the bot.

The bot reads:
  - profiles            (handle -> user_id)         — anon-readable
  - turns               (filtered by user/project)  — anon-readable
  - project_summaries   (cached blurbs)             — anon-readable
  - feishu_links        (open_id -> user_id)        — RLS-protected
  - bot_workspace       (bot-owned Feishu resources) — service-role only
  - bot_actions         (write idempotency/audit)   — service-role only

The first three are public via RLS, mirroring how the web reads them.
feishu_links is owner-only by design (we don't want a logged-in user
to be able to scrape the open_id ↔ user_id mapping from the browser).

So the bot uses two clients:
  - sb()       — anon key, for the public tables.
  - sb_admin() — service-role key, for server-only identity and write-tool state.

Service role bypasses RLS — never expose it to browser code.
"""
from __future__ import annotations

from config import settings


_client = None
_admin = None


def _create_client(url: str, key: str):
    from supabase import create_client

    return create_client(url, key)


def sb():
    global _client
    if _client is None:
        _client = _create_client(settings.supabase_url, settings.supabase_anon_key)
    return _client


def sb_admin():
    """Service-role client. Bypasses RLS — only use for feishu_links."""
    global _admin
    if _admin is None:
        if not settings.supabase_service_role_key:
            raise RuntimeError(
                "supabase_service_role_key is not set; "
                "Feishu identity lookups, bot_workspace bootstrap, "
                "bot_actions idempotency, and write tools will fail."
            )
        _admin = _create_client(
            settings.supabase_url, settings.supabase_service_role_key,
        )
    return _admin
