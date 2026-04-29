-- pmo_agent initial schema. Source of truth: docs/specs/2026-04-29-mvp-design.md §3, §5.2.
-- Forward-only: never edit this file after merge; add a new migration instead.

-- ────────────────────────────────────────────────────────────────────────
-- profiles: public-facing user identity. handle is the URL slug at /u/:handle.
-- ────────────────────────────────────────────────────────────────────────
create table public.profiles (
    id           uuid primary key references auth.users(id) on delete cascade,
    handle       text not null unique,
    display_name text,
    created_at   timestamptz not null default now()
);
comment on table public.profiles is 'Public-facing user identity. handle is the URL slug.';

-- ────────────────────────────────────────────────────────────────────────
-- turns: one row = one (user_message, agent_response) atomic unit.
-- Sessions are derived by grouping on agent_session_id.
-- ────────────────────────────────────────────────────────────────────────
create table public.turns (
    id                  bigserial primary key,
    user_id             uuid not null references auth.users(id) on delete cascade,
    agent               text not null,
    agent_session_id    text not null,
    project_path        text,
    turn_index          int  not null,
    user_message        text not null,
    agent_response_full text,
    agent_summary       text,
    user_message_at     timestamptz not null,
    agent_response_at   timestamptz,
    created_at          timestamptz not null default now(),
    updated_at          timestamptz not null default now()
);
comment on column public.turns.agent_session_id is
    'Native session id from the agent (e.g. CC jsonl filename UUID). UI grouping only; no active/closed semantics.';
comment on column public.turns.agent_summary is
    'One-sentence LLM summary of agent_response_full, generated asynchronously by the summarize Edge Function. NULL = pending.';

-- Idempotent re-uploads from the daemon: same (user, agent, session, turn_index) collapses into one row.
create unique index turns_dedup
    on public.turns (user_id, agent, agent_session_id, turn_index);

-- Profile timeline query: list a user's turns newest-first.
create index turns_user_time on public.turns (user_id, user_message_at desc);

-- /discover feed: list everyone's turns newest-first.
create index turns_global_time on public.turns (user_message_at desc);

-- ────────────────────────────────────────────────────────────────────────
-- tokens: long-lived PATs the daemon presents. We store only SHA-256(token),
-- never plaintext. Revocation = set revoked_at.
-- ────────────────────────────────────────────────────────────────────────
create table public.tokens (
    id           uuid primary key default gen_random_uuid(),
    user_id      uuid not null references auth.users(id) on delete cascade,
    token_hash   text not null unique,
    label        text,
    created_at   timestamptz not null default now(),
    last_used_at timestamptz,
    revoked_at   timestamptz
);
comment on table public.tokens is
    'Daemon PATs. token_hash = sha256(plaintext); plaintext is shown to the user once at creation and never stored.';

create index tokens_user on public.tokens (user_id);

-- ────────────────────────────────────────────────────────────────────────
-- RLS: public-read everywhere; writes scoped to authenticated owner.
-- The ingest Edge Function uses the service_role key and bypasses RLS,
-- but these policies remain as defence-in-depth for any future direct
-- writes from authenticated web users.
-- ────────────────────────────────────────────────────────────────────────
alter table public.profiles enable row level security;
alter table public.turns    enable row level security;
alter table public.tokens   enable row level security;

create policy "profiles are public read"
    on public.profiles for select using (true);
create policy "users insert own profile"
    on public.profiles for insert with check (auth.uid() = id);
create policy "users update own profile"
    on public.profiles for update using (auth.uid() = id);

create policy "turns are public read"
    on public.turns for select using (true);
create policy "users insert own turns"
    on public.turns for insert with check (auth.uid() = user_id);
create policy "users update own turns"
    on public.turns for update using (auth.uid() = user_id);
create policy "users delete own turns"
    on public.turns for delete using (auth.uid() = user_id);

-- tokens: never publicly readable. Owner can read/manage their own tokens.
-- (token_hash is one-way, but we still don't leak which hashes exist.)
create policy "users read own tokens"
    on public.tokens for select using (auth.uid() = user_id);
create policy "users insert own tokens"
    on public.tokens for insert with check (auth.uid() = user_id);
create policy "users update own tokens"
    on public.tokens for update using (auth.uid() = user_id);
create policy "users delete own tokens"
    on public.tokens for delete using (auth.uid() = user_id);

-- ────────────────────────────────────────────────────────────────────────
-- updated_at trigger for turns.
-- ────────────────────────────────────────────────────────────────────────
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at := now();
    return new;
end;
$$;

create trigger turns_set_updated_at
    before update on public.turns
    for each row execute function public.set_updated_at();
