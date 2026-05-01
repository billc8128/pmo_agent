-- Map a pmo_agent user to their Feishu identity, so the bot can answer
-- questions like "我昨天做了啥" without asking who you are. Populated
-- via OAuth — see web/app/api/feishu/oauth/callback/route.ts.
--
-- Strict 1:1: a Feishu account binds to at most one pmo_agent account
-- (open_id PRIMARY KEY) and a pmo_agent account binds to at most one
-- Feishu account (UNIQUE on user_id). Re-binding either side requires
-- explicit unbind first — keeps the mental model simple.

create table public.feishu_links (
    feishu_open_id text  primary key,
    user_id        uuid  not null unique references auth.users(id) on delete cascade,
    feishu_name    text,
    feishu_email   text,
    linked_at      timestamptz not null default now()
);

comment on table public.feishu_links is
    'Maps a Feishu user (open_id) to a pmo_agent profile. Populated by web OAuth.';

-- RLS: users can read/delete their own row. The bot reads via service-
-- role key so it bypasses RLS for arbitrary lookups.
alter table public.feishu_links enable row level security;

create policy "owners can read their link"
    on public.feishu_links for select
    using (auth.uid() = user_id);

create policy "owners can delete their link"
    on public.feishu_links for delete
    using (auth.uid() = user_id);

-- Inserts and updates only happen via service role (the OAuth callback
-- runs server-side with elevated privileges). No anon insert policy by
-- design — we don't want a logged-in user to be able to claim someone
-- else's open_id from the browser.
