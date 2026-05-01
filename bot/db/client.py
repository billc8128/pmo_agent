"""Supabase client for the bot.

The bot reads:
  - profiles            (handle -> user_id)         — anon-readable
  - turns               (filtered by user/project)  — anon-readable
  - project_summaries   (cached blurbs)             — anon-readable
  - feishu_links        (open_id -> user_id)        — RLS-protected

The first three are public via RLS, mirroring how the web reads them.
feishu_links is owner-only by design (we don't want a logged-in user
to be able to scrape the open_id ↔ user_id mapping from the browser).

So the bot uses two clients:
  - sb()       — anon key, for the public tables.
  - sb_admin() — service-role key, ONLY for feishu_links lookups.

Service role bypasses RLS — keep its usage narrow.
"""
from __future__ import annotations

from supabase import Client, create_client

from config import settings


_client: Client | None = None
_admin: Client | None = None


def sb() -> Client:
    global _client
    if _client is None:
        _client = create_client(settings.supabase_url, settings.supabase_anon_key)
    return _client


def sb_admin() -> Client:
    """Service-role client. Bypasses RLS — only use for feishu_links."""
    global _admin
    if _admin is None:
        if not settings.supabase_service_role_key:
            raise RuntimeError(
                "supabase_service_role_key is not set; "
                "feishu identity lookups will fail."
            )
        _admin = create_client(
            settings.supabase_url, settings.supabase_service_role_key,
        )
    return _admin
