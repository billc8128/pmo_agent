-- Proactive PMO notifications 1.0c.
-- Gatekeeper decisions open investigation jobs; investigator jobs
-- write final notification rows atomically after reading broader context.

create extension if not exists pgcrypto;

alter table public.subscriptions
    add column if not exists metadata jsonb not null default '{}'::jsonb;

create table if not exists public.investigation_jobs (
    id                     bigserial primary key,
    subscription_id        uuid not null references public.subscriptions(id) on delete cascade,
    status                 text not null default 'open' check (
                               status in ('open', 'investigating', 'notified', 'suppressed', 'failed')
                           ),
    seed_event_ids         bigint[] not null default '{}'::bigint[],
    initial_focus          text,
    decider_reason         text,
    investigator_decision  jsonb,
    notification_id        bigint references public.notifications(id) on delete set null,
    claim_id               uuid,
    claimed_at             timestamptz,
    attempt_count          int not null default 0,
    last_error             text,
    last_error_at          timestamptz,
    input_tokens           int,
    output_tokens          int,
    opened_at              timestamptz not null default now(),
    updated_at             timestamptz not null default now(),
    closed_at              timestamptz,
    error                  text
);

create index if not exists investigation_jobs_open_idx
    on public.investigation_jobs (subscription_id, opened_at desc)
    where status = 'open';

create index if not exists investigation_jobs_status_idx
    on public.investigation_jobs (status, opened_at)
    where status in ('open', 'investigating');

alter table public.notifications
    add column if not exists investigation_job_id bigint
        references public.investigation_jobs(id) on delete set null;

create index if not exists notif_investigation_job_idx
    on public.notifications (investigation_job_id)
    where investigation_job_id is not null;

alter table public.decision_logs
    add column if not exists investigation_job_id bigint
        references public.investigation_jobs(id) on delete set null;

create index if not exists decision_logs_investigation_job_idx
    on public.decision_logs (investigation_job_id)
    where investigation_job_id is not null;

create or replace function public.append_to_or_open_investigation_job(
    p_subscription_id uuid,
    p_event_id bigint,
    p_initial_focus text,
    p_decider_reason text,
    p_window_minutes int default 30
) returns bigint
language plpgsql
security definer
as $$
declare
    v_job_id bigint;
    v_window interval := make_interval(mins => greatest(coalesce(p_window_minutes, 30), 1));
begin
    perform pg_advisory_xact_lock(hashtext('inv_job:' || p_subscription_id::text));

    select id
      into v_job_id
      from public.investigation_jobs
     where subscription_id = p_subscription_id
       and status = 'open'
       and opened_at >= now() - v_window
     order by opened_at desc
     limit 1
     for update;

    if v_job_id is null then
        insert into public.investigation_jobs (
            subscription_id,
            seed_event_ids,
            initial_focus,
            decider_reason
        ) values (
            p_subscription_id,
            array[p_event_id],
            p_initial_focus,
            p_decider_reason
        )
        returning id into v_job_id;
    else
        update public.investigation_jobs
           set seed_event_ids = case
                   when p_event_id = any(seed_event_ids) then seed_event_ids
                   else seed_event_ids || p_event_id
               end,
               initial_focus = coalesce(initial_focus, p_initial_focus),
               decider_reason = coalesce(decider_reason, p_decider_reason),
               updated_at = now()
         where id = v_job_id;
    end if;

    return v_job_id;
end $$;

create or replace function public.claim_investigatable_jobs(
    p_claim_id uuid,
    p_limit int,
    p_window_minutes int default 30
) returns table (
    investigation_job jsonb,
    subscription jsonb,
    event_payloads jsonb
)
language plpgsql
security definer
as $$
begin
    return query
    with claimed as (
        update public.investigation_jobs j
           set status = 'investigating',
               claim_id = p_claim_id,
               claimed_at = now(),
               updated_at = now()
         where j.id in (
            select j2.id
              from public.investigation_jobs j2
             where j2.status = 'open'
               and (
                    coalesce(array_length(j2.seed_event_ids, 1), 0) >= 5
                    or j2.opened_at <= now() - make_interval(mins => greatest(coalesce(p_window_minutes, 30), 1))
               )
             order by j2.opened_at
             limit greatest(coalesce(p_limit, 5), 0)
             for update skip locked
         )
        returning j.*
    )
    select to_jsonb(c) as investigation_job,
           to_jsonb(s) as subscription,
           coalesce(ev.events, '[]'::jsonb) as event_payloads
      from claimed c
      join public.subscriptions s on s.id = c.subscription_id
      left join lateral (
        select jsonb_agg(
                   jsonb_build_object(
                       'id', e.id,
                       'user_id', e.user_id,
                       'payload', e.payload,
                       'payload_version', e.payload_version,
                       'occurred_at', e.occurred_at,
                       'project_root', e.project_root
                   )
                   order by sid.ord
               ) as events
          from unnest(c.seed_event_ids) with ordinality as sid(event_id, ord)
          join public.events e on e.id = sid.event_id
      ) ev on true;
end $$;

create or replace function public.create_notification_for_investigation_job(
    p_job_id bigint,
    p_claim_id uuid,
    p_event_id bigint,
    p_subscription_id uuid,
    p_decided_payload_version int,
    p_payload_snapshot jsonb,
    p_delivery_kind text,
    p_delivery_target text,
    p_input_tokens int default null,
    p_output_tokens int default null
) returns bigint
language plpgsql
security definer
as $$
declare
    v_notif_id bigint;
    v_status text := case when p_delivery_target is null then 'suppressed' else 'pending' end;
    v_suppressed_by text := case when p_delivery_target is null then 'no_delivery_target' else null end;
begin
    perform 1
      from public.investigation_jobs
     where id = p_job_id
       and claim_id = p_claim_id
       and status = 'investigating'
     for update;

    if not found then
        return null;
    end if;

    with up as (
        insert into public.notifications (
            event_id,
            subscription_id,
            status,
            suppressed_by,
            delivery_kind,
            delivery_target,
            decided_payload_version,
            decided_at,
            payload_snapshot,
            investigation_job_id
        ) values (
            p_event_id,
            p_subscription_id,
            v_status,
            v_suppressed_by,
            p_delivery_kind,
            p_delivery_target,
            p_decided_payload_version,
            now(),
            p_payload_snapshot,
            p_job_id
        )
        on conflict (event_id, subscription_id) do update
            set status = excluded.status,
                suppressed_by = excluded.suppressed_by,
                delivery_kind = excluded.delivery_kind,
                delivery_target = excluded.delivery_target,
                decided_payload_version = excluded.decided_payload_version,
                decided_at = excluded.decided_at,
                payload_snapshot = excluded.payload_snapshot,
                investigation_job_id = excluded.investigation_job_id,
                rendered_text = null,
                feishu_msg_id = null,
                sent_at = null,
                error = null,
                claim_id = null,
                claimed_at = null
            where public.notifications.status not in ('sent', 'claimed')
              and excluded.decided_payload_version
                  > public.notifications.decided_payload_version
        returning id
    )
    select id into v_notif_id from up;

    if v_notif_id is null then
        update public.investigation_jobs
           set status = 'suppressed',
               investigator_decision = jsonb_build_object(
                   'notify', false,
                   'suppressed_by', 'delivery_dedup',
                   'reason', 'notification row was already sent or claimed'
               ),
               claim_id = null,
               claimed_at = null,
               closed_at = now(),
               updated_at = now(),
               input_tokens = p_input_tokens,
               output_tokens = p_output_tokens
         where id = p_job_id
           and claim_id = p_claim_id
           and status = 'investigating';
        return null;
    end if;

    update public.investigation_jobs
       set status = case when v_status = 'pending' then 'notified' else 'suppressed' end,
           investigator_decision = p_payload_snapshot,
           notification_id = v_notif_id,
           claim_id = null,
           claimed_at = null,
           closed_at = now(),
           updated_at = now(),
           input_tokens = p_input_tokens,
           output_tokens = p_output_tokens
     where id = p_job_id
       and claim_id = p_claim_id
       and status = 'investigating';

    return v_notif_id;
end $$;

create or replace function public.mark_job_suppressed_if_claimed(
    p_id bigint,
    p_claim_id uuid,
    p_brief jsonb,
    p_input_tokens int default null,
    p_output_tokens int default null
) returns bigint
language sql
security definer
as $$
    update public.investigation_jobs
       set status = 'suppressed',
           investigator_decision = p_brief,
           input_tokens = p_input_tokens,
           output_tokens = p_output_tokens,
           claim_id = null,
           claimed_at = null,
           closed_at = now(),
           updated_at = now()
     where id = p_id
       and claim_id = p_claim_id
       and status = 'investigating'
    returning id;
$$;

create or replace function public.release_job_claim(
    p_id bigint,
    p_claim_id uuid
) returns bigint
language sql
security definer
as $$
    update public.investigation_jobs
       set status = 'open',
           claim_id = null,
           claimed_at = null,
           updated_at = now()
     where id = p_id
       and claim_id = p_claim_id
       and status = 'investigating'
    returning id;
$$;

create or replace function public.mark_job_failed_if_claimed(
    p_id bigint,
    p_claim_id uuid,
    p_error text
) returns bigint
language sql
security definer
as $$
    update public.investigation_jobs
       set status = 'failed',
           error = p_error,
           last_error = p_error,
           last_error_at = now(),
           claim_id = null,
           claimed_at = null,
           closed_at = now(),
           updated_at = now()
     where id = p_id
       and claim_id = p_claim_id
       and status = 'investigating'
    returning id;
$$;

create or replace function public.reap_stale_job_claims(
    p_stale_after_minutes int default 10
) returns int
language plpgsql
security definer
as $$
declare
    n int;
begin
    update public.investigation_jobs
       set status = 'open',
           claim_id = null,
           claimed_at = null,
           updated_at = now()
     where status = 'investigating'
       and claimed_at < now() - make_interval(mins => p_stale_after_minutes);
    get diagnostics n = row_count;
    return n;
end $$;

create or replace function public.bump_investigation_parse_failure(
    p_id bigint,
    p_claim_id uuid,
    p_error text
) returns int
language plpgsql
security definer
as $$
declare
    n int;
begin
    update public.investigation_jobs
       set attempt_count = attempt_count + 1,
           last_error = p_error,
           last_error_at = now(),
           updated_at = now()
     where id = p_id
       and claim_id = p_claim_id
       and status = 'investigating'
    returning attempt_count into n;

    return coalesce(n, 0);
end $$;

create or replace function public.index_subscription_metadata(
    p_subscription_id uuid
) returns void
language plpgsql
security definer
as $$
declare
    desc_lower text;
    matched jsonb;
    k_hash text;
begin
    select lower(description)
      into desc_lower
      from public.subscriptions
     where id = p_subscription_id
     for update;

    if desc_lower is null then
        return;
    end if;

    with tokens as (
        select distinct lower(regexp_replace(project_root, '^.*/', '')) as token
          from public.events
         where project_root is not null
           and project_root <> ''
    ), clean as (
        select token
          from tokens
         where token <> ''
    ), hashed as (
        select substr(
                   encode(extensions.digest(coalesce(string_agg(token, '|' order by token), ''), 'sha256'), 'hex'),
                   1,
                   16
               ) as h
          from clean
    ), escaped as (
        select token,
               regexp_replace(token, '([\\^$.|?*+(){}\[\]])', E'\\\\\\1', 'g') as token_re
          from clean
    ), matches as (
        select token
          from escaped
         where (
             length(token) >= 4
             and desc_lower ~ ('\m' || token_re || '\M')
         ) or (
             length(token) < 4
             and (
                 desc_lower ~ ('\mproject[\s\-_:]*' || token_re || '\M')
                 or desc_lower ~ ('项目[\s\-_:''`"]*' || token_re || '($|[\s''`"])')
                 or desc_lower ~ ('`' || token_re || '`')
                 or desc_lower ~ ('/' || token_re || '(/|$|[^a-z0-9])')
                 or desc_lower ~ ('"' || token_re || '"')
             )
         )
    )
    select coalesce((select jsonb_agg(token order by token) from matches), '[]'::jsonb),
           (select h from hashed)
      into matched, k_hash;

    update public.subscriptions
       set metadata = jsonb_build_object(
               'matched_projects', matched,
               'project_tokens_hash', k_hash,
               'indexed_at', now()
           ),
           updated_at = now()
     where id = p_subscription_id;
end $$;

alter table public.investigation_jobs enable row level security;

revoke execute on function public.append_to_or_open_investigation_job(uuid,bigint,text,text,int) from public;
revoke execute on function public.append_to_or_open_investigation_job(uuid,bigint,text,text,int) from anon;
revoke execute on function public.append_to_or_open_investigation_job(uuid,bigint,text,text,int) from authenticated;
grant execute on function public.append_to_or_open_investigation_job(uuid,bigint,text,text,int) to service_role;

revoke execute on function public.claim_investigatable_jobs(uuid,int,int) from public;
revoke execute on function public.claim_investigatable_jobs(uuid,int,int) from anon;
revoke execute on function public.claim_investigatable_jobs(uuid,int,int) from authenticated;
grant execute on function public.claim_investigatable_jobs(uuid,int,int) to service_role;

revoke execute on function public.create_notification_for_investigation_job(bigint,uuid,bigint,uuid,int,jsonb,text,text,int,int) from public;
revoke execute on function public.create_notification_for_investigation_job(bigint,uuid,bigint,uuid,int,jsonb,text,text,int,int) from anon;
revoke execute on function public.create_notification_for_investigation_job(bigint,uuid,bigint,uuid,int,jsonb,text,text,int,int) from authenticated;
grant execute on function public.create_notification_for_investigation_job(bigint,uuid,bigint,uuid,int,jsonb,text,text,int,int) to service_role;

revoke execute on function public.mark_job_suppressed_if_claimed(bigint,uuid,jsonb,int,int) from public;
revoke execute on function public.mark_job_suppressed_if_claimed(bigint,uuid,jsonb,int,int) from anon;
revoke execute on function public.mark_job_suppressed_if_claimed(bigint,uuid,jsonb,int,int) from authenticated;
grant execute on function public.mark_job_suppressed_if_claimed(bigint,uuid,jsonb,int,int) to service_role;

revoke execute on function public.release_job_claim(bigint,uuid) from public;
revoke execute on function public.release_job_claim(bigint,uuid) from anon;
revoke execute on function public.release_job_claim(bigint,uuid) from authenticated;
grant execute on function public.release_job_claim(bigint,uuid) to service_role;

revoke execute on function public.mark_job_failed_if_claimed(bigint,uuid,text) from public;
revoke execute on function public.mark_job_failed_if_claimed(bigint,uuid,text) from anon;
revoke execute on function public.mark_job_failed_if_claimed(bigint,uuid,text) from authenticated;
grant execute on function public.mark_job_failed_if_claimed(bigint,uuid,text) to service_role;

revoke execute on function public.reap_stale_job_claims(int) from public;
revoke execute on function public.reap_stale_job_claims(int) from anon;
revoke execute on function public.reap_stale_job_claims(int) from authenticated;
grant execute on function public.reap_stale_job_claims(int) to service_role;

revoke execute on function public.bump_investigation_parse_failure(bigint,uuid,text) from public;
revoke execute on function public.bump_investigation_parse_failure(bigint,uuid,text) from anon;
revoke execute on function public.bump_investigation_parse_failure(bigint,uuid,text) from authenticated;
grant execute on function public.bump_investigation_parse_failure(bigint,uuid,text) to service_role;

revoke execute on function public.index_subscription_metadata(uuid) from public;
revoke execute on function public.index_subscription_metadata(uuid) from anon;
revoke execute on function public.index_subscription_metadata(uuid) from authenticated;
grant execute on function public.index_subscription_metadata(uuid) to service_role;

alter function public.append_to_or_open_investigation_job(uuid,bigint,text,text,int)
    set search_path = public, pg_temp;
alter function public.claim_investigatable_jobs(uuid,int,int)
    set search_path = public, pg_temp;
alter function public.create_notification_for_investigation_job(bigint,uuid,bigint,uuid,int,jsonb,text,text,int,int)
    set search_path = public, pg_temp;
alter function public.mark_job_suppressed_if_claimed(bigint,uuid,jsonb,int,int)
    set search_path = public, pg_temp;
alter function public.release_job_claim(bigint,uuid)
    set search_path = public, pg_temp;
alter function public.mark_job_failed_if_claimed(bigint,uuid,text)
    set search_path = public, pg_temp;
alter function public.reap_stale_job_claims(int)
    set search_path = public, pg_temp;
alter function public.bump_investigation_parse_failure(bigint,uuid,text)
    set search_path = public, pg_temp;
alter function public.index_subscription_metadata(uuid)
    set search_path = public, pg_temp;
