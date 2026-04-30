"""Read-only Supabase client.

The bot reads three things from the database:
  - profiles (handle -> user_id)
  - turns    (filtered by user / project / time)
  - project_summaries (per (user, project_root) cached blurbs)

All reads use the public anon key. RLS allows anonymous SELECT on these
three tables, mirroring how the web app reads them. We do NOT use
service_role here — even though the bot is server-side, holding a
service_role key in a chat-bot service is a bigger blast radius than
necessary when public-anon-read covers everything we need.
"""
from __future__ import annotations

from supabase import Client, create_client

from config import settings


_client: Client | None = None


def sb() -> Client:
    global _client
    if _client is None:
        _client = create_client(settings.supabase_url, settings.supabase_anon_key)
    return _client
