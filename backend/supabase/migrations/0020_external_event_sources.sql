-- Proactive PMO notifications 2.0a.
-- External event sources: GitHub/Gitea webhooks, identity mapping,
-- repo mapping, raw delivery archive, and PR resource cache.

create extension if not exists pgcrypto;

alter table public.events
    add column if not exists payload_fingerprint text;

create table if not exists public.external_identities (
    id             uuid primary key default gen_random_uuid(),
    profile_id     uuid not null references public.profiles(id) on delete cascade,
    provider       text not null check (provider in ('github', 'gitea')),
    external_login text not null,
    external_id    text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    constraint extid_login_unique unique (provider, external_login)
);

create unique index if not exists extid_id_unique
    on public.external_identities (provider, external_id)
    where external_id is not null;

create index if not exists extid_profile_idx
    on public.external_identities (profile_id);

drop trigger if exists external_identities_set_updated_at
    on public.external_identities;
create trigger external_identities_set_updated_at
    before update on public.external_identities
    for each row execute function public.set_updated_at();

create table if not exists public.external_repos (
    id             uuid primary key default gen_random_uuid(),
    provider       text not null check (provider in ('github', 'gitea')),
    repo_full_name text not null,
    project_root   text not null,
    created_by     uuid references public.profiles(id) on delete set null,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    constraint repo_unique unique (provider, repo_full_name)
);

create index if not exists repos_project_root_idx
    on public.external_repos (project_root);

drop trigger if exists external_repos_set_updated_at
    on public.external_repos;
create trigger external_repos_set_updated_at
    before update on public.external_repos
    for each row execute function public.set_updated_at();

create table if not exists public.external_webhook_deliveries (
    id          bigserial primary key,
    provider    text not null check (provider in ('github', 'gitea')),
    delivery_id text not null,
    event_type  text not null,
    received_at timestamptz not null default now(),
    raw_body    jsonb not null,
    raw_headers jsonb,
    event_id    bigint references public.events(id) on delete set null,
    constraint webhook_delivery_unique unique (provider, delivery_id)
);

create index if not exists webhook_deliveries_event_idx
    on public.external_webhook_deliveries (event_id)
    where event_id is not null;

create index if not exists webhook_deliveries_received_at_idx
    on public.external_webhook_deliveries (received_at desc);

create table if not exists public.external_resource_cache (
    id            uuid primary key default gen_random_uuid(),
    provider      text not null check (provider in ('github', 'gitea')),
    resource_kind text not null check (
                      resource_kind in ('pr_files', 'pr_diff', 'commit', 'release_notes')
                  ),
    resource_key  text not null,
    content       jsonb not null,
    fetched_at    timestamptz not null default now(),
    expires_at    timestamptz not null,
    constraint resource_unique unique (provider, resource_kind, resource_key)
);

create index if not exists resource_cache_expires_idx
    on public.external_resource_cache (expires_at);

alter table public.external_identities enable row level security;
alter table public.external_repos enable row level security;
alter table public.external_webhook_deliveries enable row level security;
alter table public.external_resource_cache enable row level security;

drop policy if exists "users read own external identities"
    on public.external_identities;
create policy "users read own external identities"
    on public.external_identities
    for select
    to authenticated
    using (auth.uid() = profile_id);

revoke all on public.external_identities from public, anon, authenticated;
revoke all on public.external_repos from public, anon, authenticated;
revoke all on public.external_webhook_deliveries from public, anon, authenticated;
revoke all on public.external_resource_cache from public, anon, authenticated;

grant select on public.external_identities to authenticated;
grant all on public.external_identities to service_role;
grant all on public.external_repos to service_role;
grant all on public.external_webhook_deliveries to service_role;
grant all on public.external_resource_cache to service_role;
grant usage, select on sequence public.external_webhook_deliveries_id_seq to service_role;

create or replace function public.upsert_external_event(
    p_source text,
    p_source_id text,
    p_user_id uuid,
    p_project_root text,
    p_occurred_at timestamptz,
    p_payload jsonb,
    p_payload_fingerprint text
) returns bigint
language plpgsql
security definer
as $$
declare
    v_event_id bigint;
begin
    insert into public.events (
        source,
        source_id,
        user_id,
        project_root,
        occurred_at,
        payload,
        payload_version,
        payload_fingerprint
    ) values (
        p_source,
        p_source_id,
        p_user_id,
        p_project_root,
        coalesce(p_occurred_at, now()),
        p_payload,
        1,
        p_payload_fingerprint
    )
    on conflict (source, source_id) do update
        set user_id = case
                when excluded.payload_fingerprint is not null
                     and excluded.payload_fingerprint
                     is distinct from public.events.payload_fingerprint
                then excluded.user_id
                else public.events.user_id
            end,
            project_root = case
                when excluded.payload_fingerprint is not null
                     and excluded.payload_fingerprint
                     is distinct from public.events.payload_fingerprint
                then excluded.project_root
                else public.events.project_root
            end,
            occurred_at = case
                when excluded.payload_fingerprint is not null
                     and excluded.payload_fingerprint
                     is distinct from public.events.payload_fingerprint
                then excluded.occurred_at
                else public.events.occurred_at
            end,
            payload = case
                when excluded.payload_fingerprint is not null
                     and excluded.payload_fingerprint
                     is distinct from public.events.payload_fingerprint
                then excluded.payload
                else public.events.payload
            end,
            payload_fingerprint = case
                when excluded.payload_fingerprint is not null
                     and excluded.payload_fingerprint
                     is distinct from public.events.payload_fingerprint
                then excluded.payload_fingerprint
                else public.events.payload_fingerprint
            end,
            payload_version = case
                when excluded.payload_fingerprint is not null
                     and excluded.payload_fingerprint
                     is distinct from public.events.payload_fingerprint
                then public.events.payload_version + 1
                else public.events.payload_version
            end,
            ingested_at = case
                when excluded.payload_fingerprint is not null
                     and excluded.payload_fingerprint
                     is distinct from public.events.payload_fingerprint
                then now()
                else public.events.ingested_at
            end
    returning id into v_event_id;

    return v_event_id;
end $$;

revoke execute on function public.upsert_external_event(text,text,uuid,text,timestamptz,jsonb,text) from public;
revoke execute on function public.upsert_external_event(text,text,uuid,text,timestamptz,jsonb,text) from anon;
revoke execute on function public.upsert_external_event(text,text,uuid,text,timestamptz,jsonb,text) from authenticated;
grant execute on function public.upsert_external_event(text,text,uuid,text,timestamptz,jsonb,text) to service_role;

alter function public.upsert_external_event(text,text,uuid,text,timestamptz,jsonb,text)
    set search_path = public, pg_temp;
