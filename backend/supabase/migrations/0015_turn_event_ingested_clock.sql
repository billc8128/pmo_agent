-- Use wall-clock time for event ingestion updates.
--
-- The trigger smoke runs insert/update assertions inside one transaction.
-- PostgreSQL now() is transaction-stable, so use clock_timestamp() for the
-- mutable ingestion timestamp that tracks when a payload version changed.

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
        ingested_at,
        payload,
        payload_version
    )
    values (
        'turn',
        new.id::text,
        new.user_id,
        new.project_root,
        new.user_message_at,
        clock_timestamp(),
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
                when payload_significantly_changed then clock_timestamp()
                else public.events.ingested_at
            end;

    return new;
end $$;
