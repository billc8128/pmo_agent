-- project_summaries: cached one-line LLM summaries of "what's been
-- happening in this user's <project_root> lately".
--
-- Computed on demand by the summarize_project Edge Function. The
-- caller passes (user_id, project_root, turn_count_signal). When
-- turn_count_signal differs from the row's stored value, the cache
-- is considered stale and the Edge Function regenerates and upserts.
--
-- Why a separate table instead of a column on profiles or turns:
--   - Per-(user, project_root) is the natural key.
--   - Lets us independently rate-limit + invalidate.
--   - Reads from the web app are RLS-public-select-friendly: any
--     visitor browsing /u/<handle> needs to read these summaries.

create table public.project_summaries (
    user_id      uuid not null references auth.users(id) on delete cascade,
    project_root text not null,
    summary      text,                                   -- nullable when generation failed/empty
    turn_count   int  not null default 0,                -- last-seen turn count for this group
    last_turn_at timestamptz,                            -- newest turn we summarized
    generated_at timestamptz not null default now(),
    primary key (user_id, project_root)
);
comment on table public.project_summaries is
    'Cached LLM aggregations per (user, project_root). Invalidate by comparing turn_count to live count(*).';

alter table public.project_summaries enable row level security;

-- Public read: anonymous visitors browsing /u/<handle> need to see
-- these. Mirrors the public-select policy on turns.
create policy "project_summaries are public read"
    on public.project_summaries for select using (true);

-- Writes happen via service_role inside the summarize_project Edge
-- Function, which bypasses RLS. We don't expose write paths to
-- authenticated users directly (no /me feature mints these).
