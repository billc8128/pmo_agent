-- Audit why an archived webhook delivery did not become an events row.

alter table public.external_webhook_deliveries
    add column if not exists ignored_reason text,
    add column if not exists ignored_at timestamptz;

comment on column public.external_webhook_deliveries.ignored_reason is
    'Safe operational reason for archived-only deliveries, e.g. unsupported_event_type, bot_actor, or missing_source_identity.';

comment on column public.external_webhook_deliveries.raw_body is
    'redacted parsed payload, not the original byte-for-byte webhook body.';

create index if not exists webhook_deliveries_ignored_idx
    on public.external_webhook_deliveries (ignored_at desc)
    where ignored_reason is not null;

-- Webhook normalisation lowercases repository full names before
-- producing project identifiers such as github:owner/repo. Keep the
-- registry in the same canonical form so mappings do not silently miss.
with ranked_external_repos as (
    select
        id,
        row_number() over (
            partition by provider, lower(repo_full_name)
            order by created_at asc, id asc
        ) as rn
    from public.external_repos
)
delete from public.external_repos r
using ranked_external_repos ranked
where r.id = ranked.id
  and ranked.rn > 1;

update public.external_repos
   set repo_full_name = lower(repo_full_name)
 where repo_full_name <> lower(repo_full_name);

alter table public.external_repos
    drop constraint if exists external_repos_repo_full_name_lower;

alter table public.external_repos
    add constraint external_repos_repo_full_name_lower
    check (repo_full_name = lower(repo_full_name));
