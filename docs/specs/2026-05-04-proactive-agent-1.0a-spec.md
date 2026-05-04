# Proactive PMO Agent 1.0a — Spec

- **Status**: Draft for implementation
- **Date**: 2026-05-04
- **Branch**: `proactive-agent`
- **Roadmap**: [proactive-agent-roadmap.md](2026-05-04-proactive-agent-roadmap.md)
- **Plan**: [proactive-agent-1.0a-plan.md](2026-05-04-proactive-agent-1.0a-plan.md)

This spec describes the first stage of the proactive PMO bot. It is
the **source of truth** for 1.0a's data model, decision rules, and
tool contracts. When implementation diverges, update this file.

Conventions established in earlier specs (forward-only migrations,
public-by-default, daemon → Supabase REST → bot, etc.) carry forward
unchanged.

---

## 1. Scope

What 1.0a delivers, restated for precision:

- A **subscriptions** layer that records each user's (or chat's)
  natural-language preferences for what they want to be told about.
- An **events** ingest layer that turns every new `turns` row into
  an event consumable by the proactive pipeline. (No GitHub yet.)
- A **decider** background process that, for each new event and
  each enabled subscription, asks an LLM: should this go out?
- A **renderer** that runs an existing-agent-style loop on approved
  decisions to produce the user-facing notification text.
- A **delivery** layer that pushes the rendered text to the right
  Feishu chat, using a new send-message client method (separate from
  the reply path the bot already has).
- Four new agent tools so users can manage subscriptions in chat:
  `add_subscription`, `list_subscriptions`, `update_subscription`,
  `remove_subscription`.
- A **why_no_notification** tool the agent can use to answer
  "why didn't you tell me about X" by reading decision logs.
- A small extension to the agent's existing `[asker]` framing: when
  a user replies to a previous notification, the parent
  notification's payload is appended to the prompt so follow-ups
  are coherent.

What 1.0a does **not** deliver — see the roadmap §2.

---

## 2. Data model

Two existing tables are touched; four new tables are added. All new
tables use `service_role` for bot writes; reads use `service_role`
for cross-user lookups (matching the existing `feishu_links`
pattern).

### 2.1 `feishu_links` — add timezone

```sql
alter table feishu_links
    add column timezone text not null default 'Asia/Shanghai';
```

The default is `Asia/Shanghai` (per user decision #4 — the most
common case for this team). The OAuth callback today does NOT
extract `timezone` from the Feishu user_info response (see current
`web/app/api/feishu/oauth/callback/route.ts`); part of this slice
is to extend that callback to also read `userJson.data.timezone`
and include it in the `feishu_links` upsert. Plan §1.6 covers
that change.

If Feishu's user_info doesn't return a timezone for a given user
(some accounts don't set one), the column stays at its default and
the user can later override it via the web UI in 1.0b.

The callback change is plan §1.6.

### 2.2 `events` — append-only signal stream

```sql
create table events (
    id            bigserial primary key,
    source        text not null,                -- 'turn' for now
    source_id     text not null,                -- (source, source_id) is unique
    user_id       uuid references profiles(id), -- subject of the event, if known
    project_root  text,                         -- canonical project, if applicable
    occurred_at      timestamptz not null,
    ingested_at      timestamptz not null default now(),
    processed_at     timestamptz,                       -- null = not yet decided on
    processed_version int default 0,                    -- which payload_version was decided on
    payload_version  int not null default 1,            -- bumped each time payload mutates
    payload          jsonb not null,
    unique (source, source_id)
);

-- Pick up: never-processed events AND events whose payload was
-- updated after the last decision (e.g. agent_summary arrived late).
create index events_unprocessed_idx
    on events (ingested_at)
    where processed_at is null or processed_version < payload_version;
```

The decider's watermark is **(processed_at IS NULL) OR
(processed_version < payload_version)**. The `payload_version`
counter exists because turn events get an empty `agent_summary`
on insert and the real summary arrives via UPDATE 5-30s later from
the summarise edge function. Without versioning, we'd either:
- decide on the empty summary → notification missing the punch line
- block forever waiting → notifications never go out for turns the
  summariser failed on

Versioning lets us decide once on what we have, then **re-decide
when the payload becomes meaningfully better**. The trigger §2.6
bumps `payload_version` whenever the **fingerprint** of the
decider-relevant fields changes — that fingerprint covers
`user_message`, `agent_summary`, `agent_response_full`,
`project_path`, `project_root`, and `user_message_at`. Pure
metadata updates (e.g. `device_label` corrections, `created_at`)
don't shift the fingerprint and don't trigger reprocessing.

**Notification rewrite rules** (enforced in `upsert_notification_row`):

| Existing status | New decided version | Action |
|-----------------|---------------------|--------|
| (no row)        | any                 | INSERT |
| `pending`       | > old               | UPDATE in place (decision changed before delivery picked it up) |
| `pending`       | ≤ old               | no-op |
| `claimed`       | any                 | no-op — delivery loop owns it; if delivery succeeds the row becomes `sent` and freezes; if delivery fails the row drops back to `pending` and the next decider iteration can rewrite it. (See §3.2 lease release.) |
| `suppressed`    | > old               | UPDATE in place |
| `suppressed`    | ≤ old               | no-op |
| `failed`        | > old               | UPDATE in place |
| `failed`        | ≤ old               | no-op |
| `sent`          | any                 | no-op — can't unsend, freeze the record |

The `claimed` row's no-op rule is what closes the staleness gap:
once delivery has begun, the decider stops mutating that row until
delivery either commits (`sent`, frozen) or fails (releases lease
back to `pending`). This means a v1 decision that's already mid-
render won't be silently rewritten to v2 underneath the renderer;
instead, when v1 finishes (sent or failed), the next decider pass
sees the higher payload_version and either freezes the v1 send (if
already delivered) or re-decides on v2.

This is what makes the late-summary regression test (validation
step 12) pass: the first decision writes `suppressed/mismatch`
with `decided_payload_version=1`; when the summary arrives and
`payload_version` becomes 2, the decider re-judges, gets a `send`
verdict, and the upsert rewrites the row to `pending` with
`decided_payload_version=2`.

For 1.0a only one source exists: `source = 'turn'`, `source_id =
turns.id::text`. The trigger lives in §2.5.

### 2.3 `subscriptions` — natural-language preferences

```sql
create table subscriptions (
    id           uuid primary key default gen_random_uuid(),
    scope_kind   text not null check (scope_kind in ('user', 'chat')),
    scope_id     text not null,
    description  text not null,
    enabled      boolean not null default true,
    created_by   uuid references profiles(id),  -- profile that created it
    chat_id      text,                          -- where it was created (for audit)
    created_at   timestamptz not null default now(),
    updated_at   timestamptz not null default now(),
    -- Soft index: helps the decider's "fetch all enabled subs for owner"
    constraint subs_scope_ck check (
        (scope_kind = 'user'  and scope_id ~ '^[0-9a-f-]{36}$') or
        (scope_kind = 'chat'  and length(scope_id) > 0)
    )
);

create index subs_scope_enabled_idx
    on subscriptions (scope_kind, scope_id)
    where enabled = true;
```

A "subscription" is the user's verbatim phrase. We do not parse it
into rules. The decider re-reads it on every event.

`scope_kind = 'user'` means "deliver to this profile's DM with the
bot". `scope_kind = 'chat'` means "deliver to this Feishu chat".
The corresponding `scope_id` is the profile's UUID or the Feishu
`chat_id`.

`created_by` and `chat_id` exist purely for audit / display and are
NOT used in routing logic.

### 2.4 `notifications` — one row per decision

```sql
create table notifications (
    id              bigserial primary key,
    event_id        bigint not null references events(id) on delete cascade,
    subscription_id uuid   not null references subscriptions(id) on delete cascade,
    status          text   not null check (status in (
                       'pending',          -- decided send, awaiting render+push
                       'claimed',          -- delivery loop owns this row, rendering/sending
                       'sent',             -- delivered to Feishu
                       'suppressed',       -- decider said no, kept for audit
                       'failed'            -- render or push errored permanently
                     )),
    -- Concurrency control between decider rewrites and delivery loop.
    -- delivery sets claimed_at + claim_id when transitioning
    -- pending → claimed; both must match on mark_sent / mark_failed
    -- so a stale claim can't overwrite a row the decider has since
    -- rewritten or another worker has re-claimed.
    claimed_at      timestamptz,
    claim_id        uuid,
    suppressed_by   text,                  -- 'duplicate_in_window' / 'quiet_hours' /
                                           -- 'daily_cap' / 'explicit_exclude' / null
    rendered_text   text,                  -- final user-facing markdown
    feishu_msg_id   text,                  -- set after successful send
    delivery_kind   text,                  -- 'feishu_user' | 'feishu_chat'
    delivery_target text,                  -- open_id or chat_id
    decided_at      timestamptz not null default now(),
    -- Which payload_version of the underlying event this decision was
    -- made on. Lets us replace stale decisions when the event payload
    -- gets a meaningful update (e.g. agent_summary arrives late).
    decided_payload_version int not null default 1,
    sent_at         timestamptz,
    error           text,
    -- Frozen snapshot of events.payload AS-OF decision time. The
    -- renderer reads this, NOT the current events.payload, so a
    -- v2 mutation between decision and delivery cannot inject new
    -- content into a notification that was approved on v1. The
    -- snapshot is rewritten alongside decided_payload_version
    -- whenever the rewrite table (§2.4) updates the row in place.
    -- Always populated for pending/sent rows; may be null for
    -- suppressed rows (we don't need to keep the body of a message
    -- we never sent).
    payload_snapshot jsonb,
    -- Idempotency guard: at most one notification row per
    -- (event, subscription); re-decisions overwrite in place when
    -- allowed by the rewrite rules below.
    constraint notif_event_sub_uniq unique (event_id, subscription_id)
);

create index notif_pending_idx
    on notifications (decided_at)
    where status = 'pending';

create index notif_recent_per_subscription_idx
    on notifications (subscription_id, decided_at desc);
```

`suppressed_by` exists so the "why didn't you tell me" path can
surface a real reason without parsing free text.

`delivery_kind` and `delivery_target` are denormalised from
subscription scope at decision time so the row is self-contained
even if the subscription is later edited or deleted.

### 2.5 `decision_logs` — every judge call

```sql
create table decision_logs (
    id              bigserial primary key,
    event_id        bigint not null references events(id) on delete cascade,
    subscription_id uuid   not null references subscriptions(id) on delete cascade,
    -- Which payload_version this decision was made on. The same
    -- (event, subscription) pair will accumulate one row per judged
    -- payload_version (e.g. v1 mismatch then v2 send when summary
    -- arrives), and why_no_notification needs to surface them all.
    payload_version int    not null,
    judge_input     jsonb  not null,
    judge_output    jsonb  not null,
    model           text   not null,
    latency_ms      int,
    -- Token usage so we can size the budget honestly (§7).
    -- Some endpoints don't return usage; columns are nullable.
    input_tokens    int,
    output_tokens   int,
    created_at      timestamptz not null default now()
);

create index decision_logs_event_sub_idx
    on decision_logs (event_id, subscription_id);

create index decision_logs_subscription_recent_idx
    on decision_logs (subscription_id, created_at desc);
```

Always written, even when the decider decides not to send. This is
how prompt iteration becomes data-driven and how the
`why_no_notification` tool works.

### 2.6 Turn → events trigger

```sql
create function on_turn_to_event() returns trigger as $$
declare
    payload_significantly_changed boolean;
    new_fingerprint text;
    old_fingerprint text;
begin
    -- Build a fingerprint of every field that ends up in the event
    -- payload AND that the decider or renderer can read. Anything
    -- not on this list is metadata that doesn't justify a re-judge.
    new_fingerprint := md5(concat_ws(
        '|',
        coalesce(new.user_message, ''),
        coalesce(new.agent_summary, ''),
        coalesce(new.agent_response_full, ''),
        coalesce(new.project_path, ''),
        coalesce(new.project_root, ''),
        coalesce(new.user_message_at::text, '')
    ));

    -- Compute "did decider-relevant fields actually change?" Branch
    -- explicitly on TG_OP so we never reference OLD on INSERT.
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

    insert into events (source, source_id, user_id, project_root,
                        occurred_at, payload, payload_version)
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
            -- Renderer needs the full agent response when writing
            -- a 200-400 char brief; storing it on the event row
            -- means the renderer doesn't have to re-fetch from the
            -- turns table. The decider does NOT receive this verbatim
            -- — see judge prompt in §4.1: it gets a capped excerpt.
            'agent_response_full', new.agent_response_full,
            'user_message_at', new.user_message_at
        ),
        1
    )
    on conflict (source, source_id) do update
        set payload = excluded.payload,
            -- Top-level columns must follow the source-of-truth
            -- update too, otherwise a turn row whose project_root
            -- gets corrected later would have the new value in
            -- payload but the stale value in the indexed top-level
            -- column. Same for user_id (rare but possible during
            -- handle migrations) and occurred_at (if user_message_at
            -- gets adjusted).
            user_id = excluded.user_id,
            project_root = excluded.project_root,
            occurred_at = excluded.occurred_at,
            payload_version = case
                when payload_significantly_changed
                    then events.payload_version + 1
                else events.payload_version
            end,
            ingested_at = case
                when payload_significantly_changed then now()
                else events.ingested_at
            end;
    return new;
end $$ language plpgsql;

create trigger turns_to_events
    after insert or update on turns
    for each row execute function on_turn_to_event();
```

We trigger on UPDATE too because `agent_summary` is filled in
asynchronously by the summarise edge function. The
`payload_significantly_changed` guard ensures the decider only
re-considers an event when the new content is materially different
— writing the same summary twice does not cause two notifications.

The trigger is **idempotent in the trivial sense** (same input →
same row), and the version field plus the
`notifications(event_id, subscription_id)` unique constraint give
the decider safe re-processing semantics.

### 2.7 RLS

All four new tables get RLS enabled. Policies:

- `events` — no anon read; service role only (the bot is the only
  reader)
- `subscriptions` — owner-readable: a `user`-scoped subscription is
  visible to the owning profile (RLS on `auth.uid() = scope_id`); a
  `chat`-scoped subscription is visible to anyone who can read the
  chat (deferred until we have web UI; for 1.0a the bot writes via
  service role and reads via service role)
- `notifications` — owner-readable (same pattern as subscriptions)
- `decision_logs` — service role only (debug data, not for users)

For 1.0a all access is via the bot's service-role client. RLS
policies for direct user access are added in 1.0b alongside the web
UI.

### 2.8 RPC functions for atomic operations

The bot's DB layer uses Supabase's Python SDK, which talks to
PostgREST and does NOT support raw SQL constructs like
`FOR UPDATE SKIP LOCKED` or transactional updates with multiple
conditions. To express the lease-based delivery (§3.2) and
versioned upsert (§2.4) atomically, we ship a small set of
PL/pgSQL **RPC functions** in the migration. Python helpers in
`bot/db/queries.py` then just call `sb_admin().rpc(name, args)`.

Functions:

```sql
-- Atomic pending → claimed transition for up to N rows. Returns
-- each claimed row PLUS the FROZEN payload snapshot taken at
-- decision time PLUS the subscription. Renderer reads
-- notif_payload_snapshot, NOT events.payload — that decoupling is
-- what guarantees a v2 mutation in events between decision and
-- delivery cannot inject new content into a v1 notification.
--
-- We return jsonb instead of composite row types because Postgres
-- doesn't support `alias.*::row_type` casts cleanly, and the bot's
-- Python deserialiser handles jsonb → dict natively via supabase-py.
create function claim_pending_notifications(
    p_claim_id uuid,
    p_limit    int
) returns table (
    notification          jsonb,
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
        update notifications n
           set status = 'claimed',
               claim_id = p_claim_id,
               claimed_at = now()
         where n.id in (
            select id from notifications
             where status = 'pending'
             order by decided_at
             limit p_limit
             for update skip locked
         )
        returning n.*
    )
    select to_jsonb(c)              as notification,
           c.payload_snapshot       as notif_payload_snapshot,
           c.decided_payload_version as notif_payload_version,
           to_jsonb(s)              as subscription
      from claimed c
      join subscriptions s on s.id = c.subscription_id;
end $$;

-- Conditional commit: only succeeds if the lease is still ours.
-- Returns the row id on success, NULL on lost lease.
create function mark_sent_if_claimed(
    p_id            bigint,
    p_claim_id      uuid,
    p_msg_id        text,
    p_rendered_text text
) returns bigint
language sql
security definer
as $$
    update notifications
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

create function mark_failed_if_claimed(
    p_id        bigint,
    p_claim_id  uuid,
    p_error     text
) returns bigint
language sql
security definer
as $$
    update notifications
       set status = 'failed',
           error = p_error,
           claim_id = null,
           claimed_at = null
     where id = p_id
       and claim_id = p_claim_id
       and status = 'claimed'
    returning id;
$$;

-- Release the lease back to pending (used on transient errors).
create function release_claim(
    p_id        bigint,
    p_claim_id  uuid
) returns bigint
language sql
security definer
as $$
    update notifications
       set status = 'pending',
           claim_id = null,
           claimed_at = null
     where id = p_id
       and claim_id = p_claim_id
       and status = 'claimed'
    returning id;
$$;

-- Reap stale claims (delivery worker crashed mid-render).
-- Returns the number of rows reaped.
create function reap_stale_claims(
    p_stale_after_minutes int default 5
) returns int
language plpgsql
security definer
as $$
declare
    n int;
begin
    update notifications
       set status = 'pending',
           claim_id = null,
           claimed_at = null
     where status = 'claimed'
       and claimed_at < now() - make_interval(mins => p_stale_after_minutes);
    get diagnostics n = row_count;
    return n;
end $$;

-- Atomic upsert implementing the §2.4 rewrite table.
-- Returns 'inserted' / 'updated' / 'noop' so the caller can log.
--
-- Uses INSERT ... ON CONFLICT (event_id, subscription_id) so
-- concurrent decider workers can't collide: at most one INSERT
-- wins, the rest fall into the DO UPDATE branch and re-evaluate
-- the rewrite table against whatever the winner wrote.
create function upsert_notification_row(
    p_event_id        bigint,
    p_subscription_id uuid,
    p_status          text,
    p_suppressed_by   text,
    p_delivery_kind   text,
    p_delivery_target text,
    p_decided_payload_version int,
    p_payload_snapshot jsonb
) returns text
language plpgsql
security definer
as $$
declare
    result_action text;
begin
    -- Single-statement upsert using a WHERE guard on DO UPDATE.
    --   - Brand-new row → INSERT, returned with xmax = 0.
    --   - Existing row that's REWRITEABLE under §2.4 (status not in
    --     sent/claimed AND incoming version > existing version)
    --     → UPDATE all fields, returned with xmax != 0.
    --   - Existing row that's NOT rewriteable → DO UPDATE's WHERE
    --     prunes the conflict update; the INSERT failed; nothing is
    --     returned. We see 0 rows and report 'noop'.
    -- We can't reference `excluded.*` in RETURNING (Postgres
    -- restriction), so the CASE inside RETURNING uses only the
    -- post-update tuple (`notifications.*`) and `xmax`.
    with up as (
        insert into notifications (
            event_id, subscription_id, status, suppressed_by,
            delivery_kind, delivery_target, decided_payload_version,
            decided_at, payload_snapshot
        ) values (
            p_event_id, p_subscription_id, p_status, p_suppressed_by,
            p_delivery_kind, p_delivery_target, p_decided_payload_version,
            now(), p_payload_snapshot
        )
        on conflict (event_id, subscription_id) do update
            set status                  = excluded.status,
                suppressed_by           = excluded.suppressed_by,
                delivery_kind           = excluded.delivery_kind,
                delivery_target         = excluded.delivery_target,
                decided_payload_version = excluded.decided_payload_version,
                decided_at              = excluded.decided_at,
                payload_snapshot        = excluded.payload_snapshot,
                -- Wipe the rendered/sent state on a real rewrite —
                -- old text was for an older payload version.
                rendered_text = null,
                feishu_msg_id = null,
                sent_at = null,
                error = null
            where notifications.status not in ('sent', 'claimed')
              and excluded.decided_payload_version
                  > notifications.decided_payload_version
        returning (xmax = 0) as inserted
    )
    select case when (select inserted from up) then 'inserted'
                when exists (select 1 from up)  then 'updated'
                else                                'noop'
           end
      into result_action;

    return result_action;
end $$;
```

Why the WHERE guard works: when the conflict path's predicate is
false, Postgres treats the row as **filtered**, the DO UPDATE
becomes a no-op, the INSERT also rolls back its conflict, and the
statement returns zero rows. This is documented behaviour of
`ON CONFLICT ... DO UPDATE ... WHERE` and is what lets us collapse
the §2.4 rewrite table into a single statement without the
forbidden `excluded.*` reference in RETURNING.

The `'noop'` return covers all the cases where the rewrite table
says "leave the row alone": existing was sent/claimed, OR incoming
version was ≤ existing version.

All six functions are `security definer` so they can do their
work without being bound by the caller's RLS policies. **But** that
makes ACL hygiene critical — if anon or authenticated could call
them they'd be a write-side RLS bypass: any browser-side code
could mark arbitrary notifications as `sent`, claim rows it doesn't
own, or rewrite decisions. The migration therefore explicitly
revokes execute from the public roles and grants to service_role
only:

```sql
-- Lock down all RPC functions defined in this section.
do $$
declare fn text;
begin
  for fn in select unnest(array[
        'claim_pending_notifications(uuid,int)',
        'mark_sent_if_claimed(bigint,uuid,text,text)',
        'mark_failed_if_claimed(bigint,uuid,text)',
        'release_claim(bigint,uuid)',
        'reap_stale_claims(int)',
        'upsert_notification_row(bigint,uuid,text,text,text,text,int)'
      ])
  loop
    execute format('revoke execute on function %s from public', fn);
    execute format('revoke execute on function %s from anon', fn);
    execute format('revoke execute on function %s from authenticated', fn);
    execute format('grant  execute on function %s to service_role', fn);
  end loop;
end $$;

-- Pin search_path so a malicious schema in the caller's path
-- can't shadow public.notifications and trick a security-definer
-- function into reading the wrong table.
alter function claim_pending_notifications(uuid,int)
    set search_path = public, pg_temp;
alter function mark_sent_if_claimed(bigint,uuid,text,text)
    set search_path = public, pg_temp;
alter function mark_failed_if_claimed(bigint,uuid,text)
    set search_path = public, pg_temp;
alter function release_claim(bigint,uuid)
    set search_path = public, pg_temp;
alter function reap_stale_claims(int)
    set search_path = public, pg_temp;
alter function upsert_notification_row(bigint,uuid,text,text,text,text,int)
    set search_path = public, pg_temp;
```

In 1.0b when the web UI needs user-context operations (e.g. "delete
my own subscription"), we add **separate** RPC functions guarded
by `auth.uid()` checks against `subscriptions.scope_id` /
`feishu_links.user_id`, granted to `authenticated`. The ones above
remain service-role-only.

The Python helpers in `bot/db/queries.py` (plan §2) are one-line
wrappers:

```python
def claim_pending_notifications(claim_id: str, limit: int) -> list[dict]:
    res = sb_admin().rpc("claim_pending_notifications", {
        "p_claim_id": claim_id, "p_limit": limit,
    }).execute()
    return res.data or []
```

---

## 3. Pipelines

### 3.1 Decider loop

Runs in the bot process as an `asyncio.create_task(...)` started in
`lifespan`. Polls `events` every **30 seconds**.

```
async def decider_loop():
    while True:
        await asyncio.sleep(30)
        # "unprocessed" = never decided OR decided on a stale payload_version
        events = fetch_events_needing_decision(limit=100)
        if not events:
            continue

        # Pull ALL enabled subscriptions once per loop iteration —
        # not per event, not by event scope. An event about albert's
        # vibelive turn must reach every user / chat with a relevant
        # subscription, regardless of where the event originated.
        all_subs = fetch_all_enabled_subscriptions()
        subs_by_scope = group_by_scope(all_subs)

        for ev in events:
            decided_version = ev.payload_version
            had_unhandled_error = False    # partial decider failures
            had_blocking_claim  = False    # claim on stale version blocks finalisation

            for scope_key, scope_subs in subs_by_scope.items():
                for candidate in scope_subs:
                    siblings = [s for s in scope_subs if s.id != candidate.id]
                    existing = get_notification(ev.id, candidate.id)
                    if existing and existing.status == 'sent':
                        continue  # frozen, can't change
                    if existing and existing.status == 'claimed':
                        # Delivery owns the row; we cannot rewrite it
                        # (spec §2.4 frozen-while-claimed rule).
                        # BUT: if the claimed row is at an older
                        # payload_version than the current event, we
                        # must NOT mark the event processed at the
                        # current version — otherwise a transient
                        # delivery failure would release the claim
                        # back to `pending` (still at old version)
                        # and the event would never be re-fetched
                        # to rewrite that pending to a v2 decision.
                        if existing.decided_payload_version < decided_version:
                            had_blocking_claim = True
                        continue
                    if existing and existing.decided_payload_version >= decided_version:
                        continue  # already judged this version
                    try:
                        decision = await judge(ev, candidate, siblings,
                                               context_for(scope_key))
                        write_decision_log(ev, candidate, decision,
                                           decided_version, model,
                                           latency, tokens)
                        # Pass ev.payload as the snapshot so the
                        # renderer reads the SAME bytes the judge
                        # decided on, even if events.payload mutates
                        # to v3 between now and delivery.
                        upsert_notification_row(ev, candidate, decision,
                                                decided_version,
                                                payload_snapshot=ev.payload)
                    except Exception as e:
                        log.exception(
                            "decider error event=%s sub=%s",
                            ev.id, candidate.id,
                        )
                        had_unhandled_error = True
                        continue

            # Flip processed_at only when EVERY (candidate, event)
            # pair was either:
            #   - successfully judged at decided_version, or
            #   - a no-op skip (sent, or claimed AT decided_version,
            #     or already at decided_payload_version >= decided_version).
            # Block finalisation on:
            #   - any unhandled exception (had_unhandled_error), or
            #   - any claimed row at an older version
            #     (had_blocking_claim) — we need another loop pass
            #     after delivery resolves so we can rewrite to v2.
            if not had_unhandled_error and not had_blocking_claim:
                mark_event_processed(ev.id, decided_version)
```

`mark_event_processed(event_id, version)` does:
```sql
update events
   set processed_at = now(), processed_version = $version
 where id = $event_id;
```

A later UPDATE to that turn row that bumps `payload_version` will
pull the event back into `fetch_events_needing_decision`. The
per-(event, candidate) `decided_payload_version` guard ensures
each candidate is re-judged exactly once per real payload change,
even if a previous loop iteration had partial failures and didn't
mark the event processed.

`context_for(sub)` is the bundle the judge needs. Critically, the
judge sees **all of the owner's preferences**, not just the
candidate subscription, because exclusions and quiet-hours are
written as separate `subscriptions` rows but must be able to
suppress matches from a *different* row. Concretely:

- **Candidate subscription**: the row currently being decided on
  (positive description, e.g. "vibelive 进展告诉我"). The judge
  considers this the potential match source.
- **All sibling subscriptions for the same scope**: every other
  enabled row owned by the same `(scope_kind, scope_id)`, ordered
  newest-first. These contain exclusions ("项目 C 不要"), quiet-hours
  ("今晚别打扰我"), and other modifiers that must veto the candidate
  if applicable.
- **Recent notifications for this scope** (last 30min). Each
  row carries:
    - `decided_at` (so the 5-min dedup rule has actual timestamps)
    - `event_id` (so the judge can ignore prior decisions about
      *the same* event when re-judging on a new payload version —
      otherwise a `suppressed/mismatch` row from version 1 would
      block version 2's send)
    - `status` — one of `sent`, `claimed`, `pending`, `suppressed`,
      `failed`. **`claimed` is included** because a notification
      that's mid-render-or-send hasn't reached the user yet, but
      will any second now; another similar event arriving in that
      window must dedup against it.
    - `subject_summary` (one line of the rendered or candidate text)
    - `project_root`
    - `suppressed_by` (when applicable)
  The judge MUST ignore rows where `event_id == current_event.id`
  when applying duplicate-window logic, and MUST NOT count
  `suppressed/mismatch` rows as occupying the dedup slot at all
  (they didn't actually disturb the user). `sent` / `claimed` /
  `pending` all DO occupy the slot. This is enforced by the
  prompt; see §4.1.
- **Daily count** for the owner (notifications with status='sent'
  since local-midnight in the owner's timezone).
- **Owner wall clock** in their timezone.
- **is_subject_the_owner**: whether `event.user_id` matches the
  subscription scope. Default is to send (per user decision #2);
  individual subscription descriptions can flip this if they
  explicitly say so.

The judge's verdict is therefore a function of (event, candidate
sub, all sibling subs, recent notifs, time, subject-relation), and
"项目 C 不要" or "今晚别打扰" written as a sibling row will reliably
veto a candidate match.

**Judge input construction (cost guard)**: The full payload stored
on `events` includes `agent_response_full` for the renderer's
benefit. The decider does NOT pass the full body to the judge. It
calls `build_judge_event(payload)` which returns a smaller dict:

```python
def build_judge_event(payload: dict) -> dict:
    return {
        **{k: payload[k] for k in (
            "turn_id", "agent", "project_path", "project_root",
            "user_message_at",
        )},
        "user_message": (payload.get("user_message") or "")[:800],
        "agent_summary": payload.get("agent_summary"),
        "agent_response_excerpt":
            (payload.get("agent_response_full") or "")[:600] or None,
    }
```

This caps each judge call at roughly 1.5k input tokens regardless of
how chatty the underlying turn was, keeping the §7 budget honest.
The full body is only loaded by the renderer, which runs at most
once per `pending` notification (rather than once per
`(event, subscription)` pair).

Errors during decision → log, mark this (event, sub) skipped (do
not write a notification row), do not block the rest of the batch.
Next loop iteration retries because `processed_at` only flips when
all subs for that event finished without error.

Concurrency: the loop runs strictly serially within one bot
process. For 1.0a we don't run multiple bot replicas, so no
distributed-lock concern. (When we do, the unique constraint on
`notifications(event_id, subscription_id)` is the cheap dedup; lock
acquisition can be added then.)

### 3.2 Renderer / delivery loop

Separate loop, claims-then-renders `pending` notifications every
**15 seconds**. Uses an explicit lease so it can't race with the
decider rewriting the same row.

```
async def delivery_loop():
    while True:
        await asyncio.sleep(15)
        # Reap stale claims first (worker crashed >5min ago).
        reap_stale_claims()
        # Claim up to 20 rows atomically: pending → claimed.
        claim_id = uuid4()
        # Each ClaimedBundle bundles the notification + joined event
        # snapshot + subscription, so the renderer has all the
        # context it needs without a second DB roundtrip AND its
        # input is frozen at decision time.
        bundles = claim_pending_notifications(claim_id, limit=20)
        for b in bundles:
            notif = b.notification
            try:
                text = await render_notification(
                    notif_row=notif,
                    # Frozen snapshot — NOT events.payload, which
                    # may have moved on. See spec §2.4 for why.
                    event_payload=b.notif_payload_snapshot,
                    subscription=b.subscription,
                )

                # Idempotency: stable uuid from (id, version) so a
                # crash-after-send doesn't double-send AND a v2
                # rewrite isn't silently dedupe-d into the v1
                # message Feishu still has cached.
                feishu_idempotency_uuid = stable_uuid_from_notif(
                    notif.id, notif.decided_payload_version,
                )

                msg_id = await deliver(
                    notif, text,
                    idempotency_uuid=feishu_idempotency_uuid,
                )
                # Conditional commit — only if our lease is still
                # the one on the row.
                ok = await mark_sent_if_claimed(
                    notif.id, claim_id, msg_id=msg_id, rendered_text=text,
                )
                if not ok:
                    log.warning("notif %s claim lost; skipping", notif.id)
            except TransientError:
                release_claim(notif.id, claim_id)
            except PermanentError as e:
                mark_failed_if_claimed(notif.id, claim_id, error=str(e))
```

**Crash-safe send (at-most-once, with caveat)**:

`stable_uuid_from_notif(notification_id, decided_payload_version)`
produces a deterministic UUIDv5 derived from the pair (namespace =
a fixed project UUID hardcoded in `feishu/client.py`). The same
(row, version) tuple always produces the same uuid; bumping the
version produces a different uuid, so a rewritten v2 notification
is sent fresh rather than being silently dedupe-d into the v1
message Feishu still has cached.

This uuid goes through to Feishu via the `uuid` query parameter
on `/open-apis/im/v1/messages`. Feishu's documented behaviour is
that within 1 hour, a request with the same `(app, receive_id,
uuid)` returns the *original* message_id rather than creating a
duplicate. So the failure scenario:

1. Delivery loop renders + calls Feishu — message lands in chat,
   gets msg_id `om_xxx`
2. Process crashes before `mark_sent_if_claimed`
3. After 5min reaper, row goes back to `pending`
4. Next iteration claims it again, renders again, calls Feishu
   with the same idempotency uuid
5. Feishu returns `om_xxx` instead of creating a new message
6. We `mark_sent_if_claimed` with `om_xxx`

Net effect: the user sees one message, the DB row reflects truth.

**Caveat (acknowledged)**: Feishu's idempotency window is ~1h. If
the process is down longer than that, the second send WILL produce
a duplicate. For 1.0a we accept this — multi-hour bot downtime
is rare and worth a dup over silent data loss. If this becomes a
problem in production we add a Feishu-msg-id read-back ("did we
already send this notification?") before the second send.

`claim_pending_notifications`, `mark_sent_if_claimed`,
`mark_failed_if_claimed`, `release_claim`, and `reap_stale_claims`
are all defined in §2.8 (Postgres SECURITY DEFINER RPCs). The
shape of the claim function's return — notification +
notif_payload_snapshot + notif_payload_version + subscription per
row — is what the delivery loop above destructures into
`b.notification`, `b.notif_payload_snapshot`, `b.subscription` for
the renderer. The snapshot is the FROZEN payload from decision
time, decoupling rendering from any subsequent mutations to
events.payload.

The conditional UPDATE pattern used by mark_sent_if_claimed /
mark_failed_if_claimed looks like:

```sql
update notifications
   set status = 'sent',
       sent_at = now(),
       feishu_msg_id = $msg_id,
       rendered_text = $text,
       claim_id = null,
       claimed_at = null
 where id = $id
   and claim_id = $claim_id
   and status = 'claimed'
returning id;  -- empty result = lost the lease
```

Lease expiry: rows stuck in `claimed` for > 5 minutes (e.g. delivery
worker crashed mid-render) are reaped at the top of each loop
iteration:

```sql
update notifications
   set status = 'pending', claim_id = null, claimed_at = null
 where status = 'claimed' and claimed_at < now() - interval '5 minutes';
```

`render_notification` is an agent invocation:

- System prompt: see §4
- User message: structured payload — event payload + subscription
  description + scope hint
- Tools available: read-only subset — `list_users`, `lookup_user`,
  `get_recent_turns`, `get_project_overview`, `get_activity_stats`,
  `today_iso`, plus a new **`resolve_subject_mention(user_id)`**
  tool that returns the linked Feishu open_id (and display name) so
  the renderer can emit a real `<at user_id="ou_xxx"></at>` for
  group mentions. Image generation, write tools (calendar / bitable
  / doc), external link readers, and `resolve_people` (which is
  ambiguity-aware and prompts for follow-ups) are all explicitly
  disallowed during rendering — the renderer must produce a final
  string in one shot with no side effects.

  As a fallback when `resolve_subject_mention` returns nothing
  (subject hasn't bound their Feishu account), the renderer uses
  `@<handle>` plain text so the message is still readable.
- Output: markdown text, post-processed via the existing
  `markdown_to_post` and sent as a `post` message (to keep parity
  with the existing answer style).

`deliver` resolves `delivery_kind`:

- `feishu_user` → call `client.send_to_user(open_id, post_content)`
  (a new method on the existing `FeishuClient`)
- `feishu_chat` → call `client.send_to_chat(chat_id, post_content)`

Failures: classify as transient (5xx, network) → retry up to 3
times with backoff inside the same loop iteration; permanent (4xx
non-rate-limit) → mark `failed` with `error`. Rate-limit (429) →
back off for 60s then continue the loop.

### 3.3 Why these are two loops

Splitting decider and delivery makes each easier to reason about:

- Decider is CPU-cheap, LLM-bound at constant low cost (Haiku-sized).
- Delivery may be I/O-heavy (rendering with full tool calls, then
  Feishu round-trips).
- A slow render doesn't delay other decisions.

They communicate only through the `notifications` table. Either
loop can be restarted, killed, or replaced without the other
noticing.

---

## 4. LLM prompts

### 4.1 Judge prompt

A single prompt, ~600 tokens. Filled with a small Python format
function. Pseudocode:

```
你是 pmo_agent 的通知决策器。给你一条新事件 + 订阅人的全部偏好 +
最近通知历史。判断要不要给候选订阅发通知。

## 候选订阅（Candidate）

  id: {uuid}
  scope: {user | chat}
  description: "{用户原话}"

## 订阅人的其他生效偏好（Sibling rules）

按时间倒序，**任何一条都可能 veto 候选订阅**——比如某条说"项目 C
不要"、"凌晨别打扰"、"我自己干的事不用提醒"——只要它和事件相关，
就压过候选订阅。

  - id={uuid}, "{原话}"
  - id={uuid}, "{原话}"
  ...

## 订阅人状态

  owner_local_time: {wall clock in their timezone, e.g. "2026-05-04 23:42 Asia/Shanghai"}
  owner_today_sent_count: {今天已**实际发出**（status='sent'）的通知数}
  owner_recent_notifications: [
    { decided_at: "2026-05-04T23:30:00+08:00",
      event_id: 12345,
      status: "sent" | "claimed" | "pending" | "suppressed" | "failed",
      subject_summary: "albert 在 vibelive 调播放器 buffer",
      project_root: "/Users/.../vibelive",
      suppressed_by: null | "duplicate_in_window" | "quiet_hours" | ... },
    ...
  ]   # last 30 minutes; 用 decided_at 判去重时间窗

## 事件

  source: {turn}
  occurred_at: {ISO}
  subject_user: {handle, 或 "未绑定"}
  project_root: {...}
  is_subject_the_owner: {true | false}
  payload:
    user_message: "{用户输入的 prompt, 截断到前 800 字符}"
    agent_summary: "{一句话总结，可能为空 — 还没异步生成出来}"
    agent_response_excerpt: "{agent 回复前 600 字符摘录，可能为空。
                              full body 没传给你 — 那是给渲染阶段用的。
                              当 summary 缺失但 excerpt 有内容时仍可
                              判断主题}"

## 决策原则

1. **排除/静音类 sibling 优先**：先扫一遍 sibling rules，看有没有任何
   一条会因当前事件或当前时间触发否定（"项目 X 不要"、"凌晨别打扰"、
   "周末不发"等）。命中就 send=false，suppressed_by 取最贴切的那个
   分类（"explicit_exclude" 或 "quiet_hours"）。
2. **去重**：扫 owner_recent_notifications，看 decided_at 在 5 分钟
   内且 subject 同主题的条目。占用 dedup slot 的判定：
   - **status 为 `sent` / `claimed` / `pending`**：占用——已经发出、
     正在发送、或即将发送，用户都会被打扰。
   - **status 为 `suppressed` 且 suppressed_by == 'mismatch'**：不
     占用——那次没真正打扰用户。
   - **status 为 `suppressed` 且 suppressed_by 为其他值**：不占用
     ——也是没打扰用户。
   - **status 为 `failed`**：不占用——发送失败，用户没收到。
   - **event_id == 当前事件**：永远忽略，不论 status——同一事件被
     payload_version 更新后重判，不该自己挡自己。
   余下条目里若有 5 分钟内同主题的 → send=false,
   suppressed_by="duplicate_in_window"，reason 必须引用被命中的那条
   通知的 decided_at 和 status（"5min 内已 sent 同主题 ..." 或
   "正在 claimed 中的同主题 ..."）。
3. **每日上限**：owner_today_sent_count >= 20 → send=false,
   suppressed_by="daily_cap"。除非 sibling rules 里写了"重要事件
   break through"。
4. **是否匹配候选订阅**：到此都没否决，看候选 description 是否覆盖
   当前事件。命中 → send=true，写 matched_aspect 和 preview_hint。
5. **agent_summary + agent_response_excerpt 都缺失或不足判断**：如果
   summary 为空，且 user_message + agent_response_excerpt 拼起来仍然
   不足以判断主题 → send=false, suppressed_by="mismatch"，reason
   注明 "summary not available yet"。等下一次 payload_version bump
   重审。如果其中任何一个有足够信息，就照常判。
6. 拿不准 → send=false。

## 输出 JSON

{
  "send": bool,
  "matched_aspect": "候选 description 里哪一块匹配的（一句话），未发出可空",
  "preview_hint": "若 send=true，1 句话告诉渲染阶段重点写什么",
  "suppressed_by": "duplicate_in_window | quiet_hours | daily_cap |
                    explicit_exclude | mismatch | null",
  "reason": "一句话说明判断依据，必须能让用户日后追问'为什么没通知'时复盘"
}
```

Model: ARK Coding Plan endpoint (same backend as the conversational
agent, per user decision #3). We use the lightest model available
on that endpoint and fall back to the default if not configured.

### 4.2 Renderer prompt

The renderer reuses the existing agent runner machinery
(`ClaudeSDKClient` + tool MCPs) but with a different system prompt.
Key differences from the question-answering prompt:

```
你是 pmo_agent 的 PMO 小助理。这次不是用户提问 — 是有一条事件
触发了用户的某个订阅，host 让你写一条主动通知。

## 当前任务

事件:
  {event payload, source, occurred_at, project_root, subject}

订阅:
  scope: {user | chat}
  description: "{原话}"
  preview_hint: "{judge 阶段的提示}"

## 输出格式

写一段 200-400 字的通知正文，markdown 可用。要求：
- 直接说事，不要 "我来告诉你" / "让我看看" 这种空话
- 重点写 [1] 改了什么 [2] 思考 / 技术方案。后者从 turn 上下文
  里挖（用 get_recent_turns / get_project_overview）
- 群通知里提到事件主体时调用 resolve_subject_mention(user_id) 拿
  open_id，然后用 `<at user_id="ou_xxx"></at>` 飞书 mention 语法。
  resolve_subject_mention 返回空说明那个人还没绑定飞书 → 直接写
  `@<handle>` 文字版本。**不要假设格式 — 一定要先调工具**。
- 末尾加一行小字写"—— 来自订阅 {description 的一句话摘要}"
  让用户知道为什么收到这条
- 不要加 [IMAGE:] 标记 — 主动通知里不生图（避免突然的视觉打扰）

## 不能做

- 不能调写工具 (schedule_meeting / append_action_items 等)
- 不能编内容 — 只用工具返回的事实
- 不能透露 user_id (UUID)
```

The renderer can call any read-only tool; image generation is
disabled for this path (the renderer's allowed_tools list omits it).

---

## 5. Subscription tools

### 5.0 Prerequisite: extend `RequestContext`

Today `RequestContext` carries `(message_id, chat_id,
sender_open_id, conversation_key)`. The subscription tools need
two more fields:

- `chat_type: str` — `'p2p' | 'group'`. Determines whether
  `add_subscription` writes a user-scoped or chat-scoped row.
- `asker_user_id: str | None` — the resolved profile UUID of the
  asker (None if Feishu account isn't bound). Used as
  `subscriptions.scope_id` for user scope, and as `created_by`
  always.
- `asker_handle: str | None` — convenience for tool error
  messages.

These are populated in `app.py::_handle_message` from
`feishu_events.ParsedMessageEvent.chat_type` (already parsed) and
`db_queries.lookup_by_feishu_open_id(sender_open_id)` (already
called for the `[asker]` framing). Then passed into the runner's
context the same way `message_id` / `chat_id` are today.

This is implemented as **§2.5 of the build plan, before the four
tools land**, because every tool depends on it.

### 5.1 The four tools

Four new MCP tools are added to the existing `tools.py`. They
follow the same `_ok` / `_err` wrapper convention.

### 5.1 `add_subscription`

```python
@tool(
    "add_subscription",
    "Save a new natural-language subscription preference for the "
    "current asker. Use when the user says things like 'X 项目有进展告诉我' "
    "or '项目 C 不要发了' or '凌晨别打扰'. The description is stored "
    "verbatim — do NOT paraphrase or extract structured rules. \n\n"
    "Scope is inferred from the conversation: in a private chat the "
    "subscription belongs to the asker; in a group it belongs to the "
    "group (delivery target = group chat). The host injects the right "
    "scope based on chat_type.",
    {"description": str},
)
async def add_subscription(args: dict) -> dict:
    ...
```

The tool uses `RequestContext` (already populated in 1.0a's
`_handle_message`) to read `chat_type`, `chat_id`, and the asker's
`user_id`. The asker must have a profile + `feishu_links` row to
create ANY subscription, including chat-scoped ones — even when
creating a group subscription on behalf of a chat, the creator's
identity is recorded in `subscriptions.created_by` and we don't
allow anonymous proxy creation. If the asker is unbound, return an
error pointing them to `/me` to bind first.

### 5.2 `list_subscriptions`

Returns the current scope's subscriptions. In a private chat, that's
the asker's user-scoped subs. In a group, it's the chat's
chat-scoped subs.

### 5.3 `update_subscription` and `remove_subscription`

Both take `id: str`. Both check that the subscription belongs to
the current scope before acting (so you can't `remove` someone
else's subscription by guessing UUIDs).

`update_subscription` only allows changing `description` and
`enabled` — scope is immutable.

### 5.4 `why_no_notification`

```python
@tool(
    "why_no_notification",
    "Look up why a particular event didn't trigger a notification. "
    "Use when the user asks 'why didn't you tell me about X' or "
    "'我没收到 vibelive 那条 push 的通知'. Searches recent decision "
    "logs for the asker's subscriptions, returns matched events with "
    "the suppressed_by reason and the judge's explanation.",
    {"query": str, "since_iso": str},
)
async def why_no_notification(args: dict) -> dict:
    ...
```

Implementation: fuzzy-match `query` against recent events' payloads
+ `decision_logs.judge_output.reason` for the asker's subscriptions
in the given time window (default 24h). When the same (event,
subscription) pair has multiple decision_logs rows (one per
`payload_version` re-judged), surface them all in chronological
order so the user can see the evolution: "v1 suppressed (mismatch:
summary not available), v2 suppressed (quiet_hours)".

---

## 6. Reply-as-followup behaviour

Today the existing `_handle_message` doesn't look at
`parent_message_id`. We add:

```
if ev.parent_message_id:
    parent_notif = lookup_notification_by_feishu_msg_id(
        ev.parent_message_id
    )
    if parent_notif:
        framed_question += render_notif_context_block(parent_notif)
```

`render_notif_context_block` produces:

```
[parent_notification] (the user is replying to this notification)
  event: turn id=42, project=vibelive, subject=albert
  payload_summary: "albert 调了播放器 buffer ..."
  notif_text: "{the actual notification text we sent}"
```

This injects the prior notification into the conversation so when
the user says "这次改动大不大" the agent already has "this" pinned
to that turn.

---

## 7. Cost / latency budget

Numbers based on user decisions and a small team (5 people):

- Daily turn volume: ~200
- Active subscriptions per person: 3-5 (assumed)
- Active group subscriptions: ~2-3
- Total subscriptions: ~25
- Decisions per day: 200 × 25 = **5000**
- Average decision tokens: ~1.5k input + 100 output (capped via
  `build_judge_event` — full agent_response_full goes to the renderer
  only, not multiplied by N subscriptions per event)

At ARK Coding Plan rates (approx Anthropic Haiku class, but pricing
is bundled), this fits comfortably within whatever monthly cap the
plan provides. We log per-decision token usage in `decision_logs`
and revisit if it exceeds budget.

Latency target:
- Decider: 30s loop + 0.5-1s/decision sequential ≤ 1 minute from
  turn write to decision write. Acceptable.
- Renderer: 5-15s per notification. Acceptable.
- End-to-end: turn → notification ≤ 2 minutes.

---

## 8. Open questions deferred to 1.0b/c

These are intentionally NOT decided in 1.0a. The architecture
permits any of these answers; we wait for real usage to choose.

1. **Pre-filter before LLM judge?** A coarse rule (e.g. project
   match by string) would cut decision volume 80%+. We don't add
   one in 1.0a so we can measure baseline judge accuracy
   uncontaminated.
2. **Multi-replica bot?** Today single-process. When we scale, the
   `(event_id, subscription_id)` unique constraint suffices for
   dedup, but lock-aware loop ownership is a future concern.
3. **Notification editing?** If a turn UPDATEs after we sent a
   notification (because `agent_summary` arrives late), do we patch
   the sent message? 1.0a: no — fire and forget. Revisit later.
4. **Cross-team subscriptions?** "Frontend group" as a subscription
   subject. Out of scope until we have group metadata.
