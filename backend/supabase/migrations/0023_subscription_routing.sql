-- Proactive PMO notifications 2.0b.
-- Split rule ownership (scope_*) from delivery target, and add
-- explicit/cross-chat consent primitives.

create extension if not exists pgcrypto;

alter table public.subscriptions
    add column if not exists target_kind text,
    add column if not exists target_id text,
    add column if not exists target_user_open_id text,
    add column if not exists consent_anchor text;

update public.subscriptions
   set target_kind = case
           when scope_kind = 'user' then 'user_dm'
           when scope_kind = 'chat' then 'chat'
       end,
       target_id = scope_id
 where target_kind is null;

create or replace function public.set_subscription_default_target()
returns trigger
language plpgsql
as $$
begin
    if new.target_kind is null then
        new.target_kind := case
            when new.scope_kind = 'user' then 'user_dm'
            when new.scope_kind = 'chat' then 'chat'
        end;
    end if;
    if new.target_id is null then
        new.target_id := new.scope_id;
    end if;
    return new;
end $$;

drop trigger if exists subscriptions_default_target on public.subscriptions;
create trigger subscriptions_default_target
    before insert on public.subscriptions
    for each row execute function public.set_subscription_default_target();

alter table public.subscriptions
    alter column target_kind set not null,
    alter column target_id set not null;

do $$
begin
    if not exists (
        select 1 from pg_constraint where conname = 'subs_target_kind_ck'
    ) then
        alter table public.subscriptions
            add constraint subs_target_kind_ck
            check (target_kind in ('user_dm', 'chat', 'mention_in_chat'));
    end if;

    if not exists (
        select 1 from pg_constraint where conname = 'subs_target_check'
    ) then
        alter table public.subscriptions
            add constraint subs_target_check
            check (
                (
                    target_kind = 'user_dm'
                    and target_id ~ '^[0-9a-f-]{36}$'
                    and target_user_open_id is null
                ) or (
                    target_kind = 'chat'
                    and length(target_id) > 0
                    and target_user_open_id is null
                ) or (
                    target_kind = 'mention_in_chat'
                    and length(target_id) > 0
                    and target_user_open_id is not null
                )
            );
    end if;
end $$;

create index if not exists subs_target_idx
    on public.subscriptions (target_kind, target_id)
    where enabled = true and archived_at is null;

alter table public.notifications
    add column if not exists mention_open_id text;

create table if not exists public.target_consents (
    id              uuid primary key default gen_random_uuid(),
    target_user_id  uuid not null references public.profiles(id) on delete cascade,
    source_user_id  uuid not null references public.profiles(id) on delete cascade,
    granted_at      timestamptz not null default now(),
    revoked_at      timestamptz,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),
    constraint target_consents_pair_uniq unique (target_user_id, source_user_id)
);

create index if not exists target_consents_active_idx
    on public.target_consents (target_user_id, source_user_id)
    where revoked_at is null;

drop trigger if exists target_consents_set_updated_at on public.target_consents;
create trigger target_consents_set_updated_at
    before update on public.target_consents
    for each row execute function public.set_updated_at();

create table if not exists public.pending_target_consents (
    id                 uuid primary key default gen_random_uuid(),
    source_user_id     uuid not null references public.profiles(id) on delete cascade,
    target_user_id     uuid not null references public.profiles(id) on delete cascade,
    request_message_id text,
    rule_description   text not null,
    status             text not null default 'pending'
                       check (status in ('pending', 'granted', 'declined', 'expired')),
    created_at         timestamptz not null default now(),
    expires_at         timestamptz not null default now() + interval '7 days',
    resolved_at        timestamptz
);

create unique index if not exists pending_target_consents_one_open_idx
    on public.pending_target_consents (source_user_id, target_user_id)
    where status = 'pending';

create index if not exists pending_target_consents_msg_idx
    on public.pending_target_consents (request_message_id)
    where status = 'pending' and request_message_id is not null;

alter table public.target_consents enable row level security;
alter table public.pending_target_consents enable row level security;

drop policy if exists "users read target consents touching them" on public.target_consents;
create policy "users read target consents touching them"
    on public.target_consents for select
    using (
        auth.uid() = target_user_id
        or auth.uid() = source_user_id
    );

drop policy if exists "users read pending target consents touching them" on public.pending_target_consents;
create policy "users read pending target consents touching them"
    on public.pending_target_consents for select
    using (
        auth.uid() = target_user_id
        or auth.uid() = source_user_id
    );

create or replace function public.add_target_consent(
    p_target_user_id uuid,
    p_source_user_id uuid
) returns public.target_consents
language sql
security definer
as $$
    insert into public.target_consents (
        target_user_id,
        source_user_id,
        granted_at,
        revoked_at
    ) values (
        p_target_user_id,
        p_source_user_id,
        now(),
        null
    )
    on conflict (target_user_id, source_user_id) do update
        set granted_at = now(),
            revoked_at = null,
            updated_at = now()
    returning *;
$$;

create or replace function public.revoke_target_consent(
    p_target_user_id uuid,
    p_source_user_id uuid
) returns public.target_consents
language sql
security definer
as $$
    update public.target_consents
       set revoked_at = now(),
           updated_at = now()
     where target_user_id = p_target_user_id
       and source_user_id = p_source_user_id
       and revoked_at is null
    returning *;
$$;

create or replace function public.mark_suppressed_if_claimed(
    p_id            bigint,
    p_claim_id      uuid,
    p_suppressed_by text,
    p_error         text default null
) returns bigint
language sql
security definer
as $$
    update public.notifications
       set status = 'suppressed',
           suppressed_by = p_suppressed_by,
           error = p_error,
           claim_id = null,
           claimed_at = null
     where id = p_id
       and claim_id = p_claim_id
       and status = 'claimed'
    returning id;
$$;

drop function if exists public.create_notification_for_investigation_job(
    bigint, uuid, bigint, uuid, int, jsonb, text, text, int, int
);

create or replace function public.create_notification_for_investigation_job(
    p_job_id bigint,
    p_claim_id uuid,
    p_event_id bigint,
    p_subscription_id uuid,
    p_decided_payload_version int,
    p_payload_snapshot jsonb,
    p_delivery_kind text,
    p_delivery_target text,
    p_mention_open_id text default null,
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
            mention_open_id,
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
            p_mention_open_id,
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
                mention_open_id = excluded.mention_open_id,
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

-- SECURITY DEFINER RPCs are service-role-only write primitives.
revoke execute on function public.add_target_consent(uuid,uuid) from public;
revoke execute on function public.add_target_consent(uuid,uuid) from anon;
revoke execute on function public.add_target_consent(uuid,uuid) from authenticated;
grant execute on function public.add_target_consent(uuid,uuid) to service_role;

revoke execute on function public.revoke_target_consent(uuid,uuid) from public;
revoke execute on function public.revoke_target_consent(uuid,uuid) from anon;
revoke execute on function public.revoke_target_consent(uuid,uuid) from authenticated;
grant execute on function public.revoke_target_consent(uuid,uuid) to service_role;

revoke execute on function public.mark_suppressed_if_claimed(bigint,uuid,text,text) from public;
revoke execute on function public.mark_suppressed_if_claimed(bigint,uuid,text,text) from anon;
revoke execute on function public.mark_suppressed_if_claimed(bigint,uuid,text,text) from authenticated;
grant execute on function public.mark_suppressed_if_claimed(bigint,uuid,text,text) to service_role;

revoke execute on function public.create_notification_for_investigation_job(bigint,uuid,bigint,uuid,int,jsonb,text,text,text,int,int) from public;
revoke execute on function public.create_notification_for_investigation_job(bigint,uuid,bigint,uuid,int,jsonb,text,text,text,int,int) from anon;
revoke execute on function public.create_notification_for_investigation_job(bigint,uuid,bigint,uuid,int,jsonb,text,text,text,int,int) from authenticated;
grant execute on function public.create_notification_for_investigation_job(bigint,uuid,bigint,uuid,int,jsonb,text,text,text,int,int) to service_role;

alter function public.add_target_consent(uuid,uuid)
    set search_path = public, pg_temp;
alter function public.revoke_target_consent(uuid,uuid)
    set search_path = public, pg_temp;
alter function public.mark_suppressed_if_claimed(bigint,uuid,text,text)
    set search_path = public, pg_temp;
alter function public.create_notification_for_investigation_job(bigint,uuid,bigint,uuid,int,jsonb,text,text,text,int,int)
    set search_path = public, pg_temp;
alter function public.set_subscription_default_target()
    set search_path = public, pg_temp;
