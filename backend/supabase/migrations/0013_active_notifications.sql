-- Proactive PMO notifications 1.0a.
-- Forward-only migration: events -> subscriptions -> decisions -> delivery leases.

create extension if not exists pgcrypto;

-- ────────────────────────────────────────────────────────────────────────
-- Events: append-ish signal stream derived from turns.
-- ────────────────────────────────────────────────────────────────────────
create table public.events (
    id                bigserial primary key,
    source            text not null,
    source_id         text not null,
    user_id           uuid references public.profiles(id) on delete set null,
    project_root      text,
    occurred_at       timestamptz not null,
    ingested_at       timestamptz not null default now(),
    processed_at      timestamptz,
    processed_version int not null default 0,
    payload_version   int not null default 1,
    payload           jsonb not null,
    unique (source, source_id)
);

create index events_unprocessed_idx
    on public.events (ingested_at)
    where processed_at is null or processed_version < payload_version;

create index events_user_time_idx
    on public.events (user_id, occurred_at desc);

create or replace view public.events_needing_decision as
    select *
      from public.events
     where processed_at is null
        or processed_version < payload_version;

revoke all on public.events_needing_decision from public;
revoke all on public.events_needing_decision from anon;
revoke all on public.events_needing_decision from authenticated;
grant select on public.events_needing_decision to service_role;

-- ────────────────────────────────────────────────────────────────────────
-- Natural-language subscriptions.
-- ────────────────────────────────────────────────────────────────────────
create table public.subscriptions (
    id          uuid primary key default gen_random_uuid(),
    scope_kind  text not null check (scope_kind in ('user', 'chat')),
    scope_id    text not null,
    description text not null,
    enabled     boolean not null default true,
    created_by  uuid references public.profiles(id) on delete set null,
    chat_id     text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),
    constraint subs_scope_ck check (
        (scope_kind = 'user' and scope_id ~ '^[0-9a-f-]{36}$') or
        (scope_kind = 'chat' and length(scope_id) > 0)
    )
);

create index subs_scope_enabled_idx
    on public.subscriptions (scope_kind, scope_id)
    where enabled = true;

create index subs_creator_recent_idx
    on public.subscriptions (created_by, created_at desc);

create trigger subscriptions_set_updated_at
    before update on public.subscriptions
    for each row execute function public.set_updated_at();

-- ────────────────────────────────────────────────────────────────────────
-- Notifications: one mutable decision row per (event, subscription).
-- ────────────────────────────────────────────────────────────────────────
create table public.notifications (
    id                      bigserial primary key,
    event_id                bigint not null references public.events(id) on delete cascade,
    subscription_id         uuid not null references public.subscriptions(id) on delete cascade,
    status                  text not null check (
                                status in (
                                  'pending',
                                  'claimed',
                                  'sent',
                                  'suppressed',
                                  'failed'
                                )
                            ),
    claimed_at              timestamptz,
    claim_id                uuid,
    suppressed_by           text,
    rendered_text           text,
    feishu_msg_id           text,
    delivery_kind           text check (
                                delivery_kind is null or
                                delivery_kind in ('feishu_user', 'feishu_chat')
                            ),
    delivery_target         text,
    decided_at              timestamptz not null default now(),
    decided_payload_version int not null default 1,
    sent_at                 timestamptz,
    error                   text,
    payload_snapshot        jsonb not null,
    constraint notif_event_sub_uniq unique (event_id, subscription_id)
);

create index notif_pending_idx
    on public.notifications (decided_at)
    where status = 'pending';

create index notif_recent_per_subscription_idx
    on public.notifications (subscription_id, decided_at desc);

create index notif_feishu_msg_idx
    on public.notifications (feishu_msg_id)
    where feishu_msg_id is not null;

-- ────────────────────────────────────────────────────────────────────────
-- Judge audit log: every LLM decision attempt.
-- ────────────────────────────────────────────────────────────────────────
create table public.decision_logs (
    id              bigserial primary key,
    event_id        bigint not null references public.events(id) on delete cascade,
    subscription_id uuid not null references public.subscriptions(id) on delete cascade,
    payload_version int not null,
    judge_input     jsonb not null,
    judge_output    jsonb not null,
    model           text not null,
    latency_ms      int,
    input_tokens    int,
    output_tokens   int,
    created_at      timestamptz not null default now()
);

create index decision_logs_event_sub_idx
    on public.decision_logs (event_id, subscription_id);

create index decision_logs_subscription_recent_idx
    on public.decision_logs (subscription_id, created_at desc);

-- ────────────────────────────────────────────────────────────────────────
-- turns -> events trigger. It references OLD only in UPDATE branches.
-- ────────────────────────────────────────────────────────────────────────
create or replace function public.on_turn_to_event()
returns trigger
language plpgsql
as $$
declare
    payload_significantly_changed boolean;
    new_fingerprint text;
    old_fingerprint text;
begin
    new_fingerprint := md5(concat_ws(
        '|',
        coalesce(new.user_message, ''),
        coalesce(new.agent_summary, ''),
        coalesce(new.agent_response_full, ''),
        coalesce(new.project_path, ''),
        coalesce(new.project_root, ''),
        coalesce(new.user_message_at::text, '')
    ));

    if tg_op = 'INSERT' then
        payload_significantly_changed := true;
    else
        old_fingerprint := md5(concat_ws(
            '|',
            coalesce(old.user_message, ''),
            coalesce(old.agent_summary, ''),
            coalesce(old.agent_response_full, ''),
            coalesce(old.project_path, ''),
            coalesce(old.project_root, ''),
            coalesce(old.user_message_at::text, '')
        ));
        payload_significantly_changed :=
            new_fingerprint is distinct from old_fingerprint;
    end if;

    insert into public.events (
        source,
        source_id,
        user_id,
        project_root,
        occurred_at,
        payload,
        payload_version
    )
    values (
        'turn',
        new.id::text,
        new.user_id,
        new.project_root,
        new.user_message_at,
        jsonb_build_object(
            'turn_id', new.id,
            'agent', new.agent,
            'project_path', new.project_path,
            'project_root', new.project_root,
            'user_message', new.user_message,
            'agent_summary', new.agent_summary,
            'agent_response_full', new.agent_response_full,
            'user_message_at', new.user_message_at
        ),
        1
    )
    on conflict (source, source_id) do update
        set payload = excluded.payload,
            user_id = excluded.user_id,
            project_root = excluded.project_root,
            occurred_at = excluded.occurred_at,
            payload_version = case
                when payload_significantly_changed
                    then public.events.payload_version + 1
                else public.events.payload_version
            end,
            ingested_at = case
                when payload_significantly_changed then now()
                else public.events.ingested_at
            end;

    return new;
end $$;

drop trigger if exists turns_to_events on public.turns;

create trigger turns_to_events
    after insert or update on public.turns
    for each row execute function public.on_turn_to_event();

-- ────────────────────────────────────────────────────────────────────────
-- RPC helpers for atomic PostgREST-safe operations.
-- ────────────────────────────────────────────────────────────────────────
create or replace function public.claim_pending_notifications(
    p_claim_id uuid,
    p_limit    int
) returns table (
    notification           jsonb,
    notif_payload_snapshot jsonb,
    notif_payload_version  int,
    subscription           jsonb
)
language plpgsql
security definer
as $$
begin
    return query
    with claimed as (
        update public.notifications n
           set status = 'claimed',
               claim_id = p_claim_id,
               claimed_at = now()
         where n.id in (
            select n2.id
              from public.notifications n2
              join public.events e on e.id = n2.event_id
             where n2.status = 'pending'
               and n2.decided_payload_version = e.payload_version
             order by n2.decided_at
             limit greatest(coalesce(p_limit, 20), 0)
             for update of n2 skip locked
         )
        returning n.*
    )
    select to_jsonb(c)                as notification,
           c.payload_snapshot         as notif_payload_snapshot,
           c.decided_payload_version  as notif_payload_version,
           to_jsonb(s)                as subscription
      from claimed c
      join public.subscriptions s on s.id = c.subscription_id;
end $$;

create or replace function public.mark_sent_if_claimed(
    p_id            bigint,
    p_claim_id      uuid,
    p_msg_id        text,
    p_rendered_text text
) returns bigint
language sql
security definer
as $$
    update public.notifications
       set status = 'sent',
           sent_at = now(),
           feishu_msg_id = p_msg_id,
           rendered_text = p_rendered_text,
           claim_id = null,
           claimed_at = null
     where id = p_id
       and claim_id = p_claim_id
       and status = 'claimed'
    returning id;
$$;

create or replace function public.mark_failed_if_claimed(
    p_id       bigint,
    p_claim_id uuid,
    p_error    text
) returns bigint
language sql
security definer
as $$
    update public.notifications
       set status = 'failed',
           error = p_error,
           claim_id = null,
           claimed_at = null
     where id = p_id
       and claim_id = p_claim_id
       and status = 'claimed'
    returning id;
$$;

create or replace function public.release_claim(
    p_id       bigint,
    p_claim_id uuid
) returns bigint
language sql
security definer
as $$
    update public.notifications
       set status = 'pending',
           claim_id = null,
           claimed_at = null
     where id = p_id
       and claim_id = p_claim_id
       and status = 'claimed'
    returning id;
$$;

create or replace function public.reap_stale_claims(
    p_stale_after_minutes int default 5
) returns int
language plpgsql
security definer
as $$
declare
    n int;
begin
    update public.notifications
       set status = 'pending',
           claim_id = null,
           claimed_at = null
     where status = 'claimed'
       and claimed_at < now() - make_interval(mins => p_stale_after_minutes);
    get diagnostics n = row_count;
    return n;
end $$;

create or replace function public.upsert_notification_row(
    p_event_id                bigint,
    p_subscription_id         uuid,
    p_status                  text,
    p_suppressed_by           text,
    p_delivery_kind           text,
    p_delivery_target         text,
    p_decided_payload_version int,
    p_payload_snapshot        jsonb
) returns text
language plpgsql
security definer
as $$
declare
    result_action text;
begin
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
            payload_snapshot
        ) values (
            p_event_id,
            p_subscription_id,
            p_status,
            p_suppressed_by,
            p_delivery_kind,
            p_delivery_target,
            p_decided_payload_version,
            now(),
            p_payload_snapshot
        )
        on conflict (event_id, subscription_id) do update
            set status = excluded.status,
                suppressed_by = excluded.suppressed_by,
                delivery_kind = excluded.delivery_kind,
                delivery_target = excluded.delivery_target,
                decided_payload_version = excluded.decided_payload_version,
                decided_at = excluded.decided_at,
                payload_snapshot = excluded.payload_snapshot,
                rendered_text = null,
                feishu_msg_id = null,
                sent_at = null,
                error = null,
                claim_id = null,
                claimed_at = null
            where public.notifications.status not in ('sent', 'claimed')
              and excluded.decided_payload_version
                  > public.notifications.decided_payload_version
        returning (xmax = 0) as inserted
    )
    select case
             when (select inserted from up) then 'inserted'
             when exists (select 1 from up) then 'updated'
             else 'noop'
           end
      into result_action;

    return result_action;
end $$;

-- SECURITY DEFINER RPCs are service-role-only write primitives.
do $$
declare
    fn text;
begin
    for fn in select unnest(array[
        'public.claim_pending_notifications(uuid,int)',
        'public.mark_sent_if_claimed(bigint,uuid,text,text)',
        'public.mark_failed_if_claimed(bigint,uuid,text)',
        'public.release_claim(bigint,uuid)',
        'public.reap_stale_claims(int)',
        'public.upsert_notification_row(bigint,uuid,text,text,text,text,int,jsonb)'
    ])
    loop
        execute format('revoke execute on function %s from public', fn);
        execute format('revoke execute on function %s from anon', fn);
        execute format('revoke execute on function %s from authenticated', fn);
        execute format('grant execute on function %s to service_role', fn);
    end loop;
end $$;

alter function public.claim_pending_notifications(uuid,int)
    set search_path = public, pg_temp;
alter function public.mark_sent_if_claimed(bigint,uuid,text,text)
    set search_path = public, pg_temp;
alter function public.mark_failed_if_claimed(bigint,uuid,text)
    set search_path = public, pg_temp;
alter function public.release_claim(bigint,uuid)
    set search_path = public, pg_temp;
alter function public.reap_stale_claims(int)
    set search_path = public, pg_temp;
alter function public.upsert_notification_row(bigint,uuid,text,text,text,text,int,jsonb)
    set search_path = public, pg_temp;

-- ────────────────────────────────────────────────────────────────────────
-- RLS. 1.0a bot access uses service role; direct browser access remains
-- closed except for user-scoped subscription reads.
-- ────────────────────────────────────────────────────────────────────────
alter table public.events enable row level security;
alter table public.subscriptions enable row level security;
alter table public.notifications enable row level security;
alter table public.decision_logs enable row level security;

create policy "users read own user subscriptions"
    on public.subscriptions for select
    using (scope_kind = 'user' and scope_id = auth.uid()::text);

create policy "users read own user notifications"
    on public.notifications for select
    using (
        exists (
            select 1
              from public.subscriptions s
             where s.id = notifications.subscription_id
               and s.scope_kind = 'user'
               and s.scope_id = auth.uid()::text
        )
    );
