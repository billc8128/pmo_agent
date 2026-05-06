begin;

do $$
declare
    sub_id uuid;
    e1 bigint;
    e2 bigint;
    e3 bigint;
    j1 bigint;
    j1_again bigint;
    j2 bigint;
    claim1 uuid := gen_random_uuid();
    claim2 uuid := gen_random_uuid();
    n int;
    row record;
    ids bigint[];
    job_status text;
    notif_id bigint;
    retry_notif_id bigint;
    version int;
    fn text;
begin
    insert into public.events (source, source_id, user_id, project_root, occurred_at, payload)
    values ('smoke', '1c-' || gen_random_uuid()::text || '-1', null, '/Users/example/vibelive',
            now(), '{"agent_summary": "first"}'::jsonb)
    returning id into e1;

    insert into public.events (source, source_id, user_id, project_root, occurred_at, payload)
    values ('smoke', '1c-' || gen_random_uuid()::text || '-2', null, '/Users/example/vibelive',
            now(), '{"agent_summary": "second"}'::jsonb)
    returning id into e2;

    insert into public.events (source, source_id, user_id, project_root, occurred_at, payload)
    values ('smoke', '1c-' || gen_random_uuid()::text || '-3', null, '/Users/example/vibelive',
            now(), '{"agent_summary": "third"}'::jsonb)
    returning id into e3;

    insert into public.subscriptions (scope_kind, scope_id, description)
    values ('user', '22222222-2222-2222-2222-222222222222', 'vibelive 进展告诉我')
    returning id into sub_id;

    j1 := public.append_to_or_open_investigation_job(sub_id, e1, 'first focus', 'first reason', 30);
    select seed_event_ids, status into ids, job_status
      from public.investigation_jobs
     where id = j1;
    if ids <> array[e1] or job_status <> 'open' then
        raise exception 'step 1 failed: ids %, status %', ids, job_status;
    end if;

    j1_again := public.append_to_or_open_investigation_job(sub_id, e2, 'second focus', 'second reason', 30);
    if j1_again <> j1 then
        raise exception 'step 2 failed: expected same job %, got %', j1, j1_again;
    end if;
    select seed_event_ids into ids from public.investigation_jobs where id = j1;
    if ids <> array[e1, e2] then
        raise exception 'step 2 failed: ids %', ids;
    end if;

    j1_again := public.append_to_or_open_investigation_job(sub_id, e1, 'dupe focus', 'dupe reason', 30);
    select seed_event_ids into ids from public.investigation_jobs where id = j1;
    if ids <> array[e1, e2] then
        raise exception 'step 3 failed: duplicate append changed ids %', ids;
    end if;

    update public.investigation_jobs
       set opened_at = now() - interval '31 min'
     where id = j1;

    j2 := public.append_to_or_open_investigation_job(sub_id, e3, 'third focus', 'third reason', 30);
    if j2 = j1 then
        raise exception 'step 5 failed: expected new job after window';
    end if;
    select seed_event_ids into ids from public.investigation_jobs where id = j2;
    if ids <> array[e3] then
        raise exception 'step 5 failed: j2 ids %', ids;
    end if;

    drop table if exists pg_temp.claim1_rows;
    create temp table claim1_rows on commit drop as
        select * from public.claim_investigatable_jobs(claim1, 5, 30);
    select count(*) into n from claim1_rows;
    if n <> 1 then
        raise exception 'step 6 failed: expected one claim row, got %', n;
    end if;
    select * into row from claim1_rows limit 1;
    if (row.investigation_job->>'id')::bigint <> j1 then
        raise exception 'step 6 failed: claimed wrong job %', row.investigation_job;
    end if;
    if row.investigation_job->>'status' <> 'investigating' then
        raise exception 'step 6 failed: job json status %', row.investigation_job->>'status';
    end if;
    if row.subscription->>'id' <> sub_id::text then
        raise exception 'step 6 failed: subscription json %', row.subscription;
    end if;
    if jsonb_array_length(row.event_payloads) <> 2 then
        raise exception 'step 6 failed: event_payloads %', row.event_payloads;
    end if;
    if (row.event_payloads->0->>'id')::bigint <> e1
       or (row.event_payloads->1->>'id')::bigint <> e2 then
        raise exception 'step 6 failed: event order %', row.event_payloads;
    end if;
    if (select status from public.investigation_jobs where id = j2) <> 'open' then
        raise exception 'step 6 failed: j2 should remain open';
    end if;

    if public.mark_job_suppressed_if_claimed(
        j1, claim1, '{"notify": false, "reason": "test"}'::jsonb, null, null
    ) is null then
        raise exception 'step 7 failed: suppress right claim returned null';
    end if;
    if (select status from public.investigation_jobs where id = j1) <> 'suppressed' then
        raise exception 'step 7 failed: j1 not suppressed';
    end if;

    if public.mark_job_suppressed_if_claimed(
        j1, gen_random_uuid(), '{"notify": false, "reason": "wrong"}'::jsonb, null, null
    ) is not null then
        raise exception 'step 8 failed: wrong claim updated row';
    end if;

    update public.investigation_jobs
       set opened_at = now() - interval '31 min'
     where id = j2;

    drop table if exists pg_temp.claim2_rows;
    create temp table claim2_rows on commit drop as
        select * from public.claim_investigatable_jobs(claim2, 5, 30);
    select count(*) into n from claim2_rows;
    if n <> 1 then
        raise exception 'step 9 failed: expected one j2 claim row, got %', n;
    end if;
    select * into row from claim2_rows limit 1;
    if (row.investigation_job->>'id')::bigint <> j2
       or row.investigation_job->>'status' <> 'investigating' then
        raise exception 'step 9 failed: j2 claim row %', row.investigation_job;
    end if;

    select payload_version into version from public.events where id = e3;
    notif_id := public.create_notification_for_investigation_job(
        j2,
        claim2,
        e3,
        sub_id,
        version,
        '{"notify": true, "headline": "vibelive progress", "key_facts": ["third"], "evidence_event_ids": []}'::jsonb,
        'feishu_user',
        'ou_test',
        null,
        null
    );
    if notif_id is null then
        raise exception 'step 10 failed: notification id null';
    end if;
    if not exists (
        select 1
          from public.notifications
         where id = notif_id
           and investigation_job_id = j2
           and payload_snapshot->>'headline' = 'vibelive progress'
           and decided_payload_version = version
    ) then
        raise exception 'step 10 failed: notification row not correct';
    end if;
    if (select status from public.investigation_jobs where id = j2) <> 'notified' then
        raise exception 'step 10 failed: j2 not notified';
    end if;

    retry_notif_id := public.create_notification_for_investigation_job(
        j2,
        claim2,
        e3,
        sub_id,
        version,
        '{"notify": true}'::jsonb,
        'feishu_user',
        'ou_test',
        null,
        null
    );
    if retry_notif_id is not null then
        raise exception 'step 11 failed: stale retry returned %', retry_notif_id;
    end if;

    for fn in
        select unnest(array[
            'public.append_to_or_open_investigation_job(uuid,bigint,text,text,int)',
            'public.claim_investigatable_jobs(uuid,int,int)',
            'public.create_notification_for_investigation_job(bigint,uuid,bigint,uuid,int,jsonb,text,text,int,int)',
            'public.mark_job_suppressed_if_claimed(bigint,uuid,jsonb,int,int)',
            'public.mark_job_failed_if_claimed(bigint,uuid,text)',
            'public.release_job_claim(bigint,uuid)',
            'public.reap_stale_job_claims(int)',
            'public.bump_investigation_parse_failure(bigint,uuid,text)',
            'public.index_subscription_metadata(uuid)'
        ])
    loop
        if has_function_privilege('anon', fn, 'EXECUTE') then
            raise exception 'step 13 failed: anon can execute %', fn;
        end if;
        if has_function_privilege('authenticated', fn, 'EXECUTE') then
            raise exception 'step 13 failed: authenticated can execute %', fn;
        end if;
        if not has_function_privilege('service_role', fn, 'EXECUTE') then
            raise exception 'step 13 failed: service_role cannot execute %', fn;
        end if;
    end loop;
end $$;

rollback;
