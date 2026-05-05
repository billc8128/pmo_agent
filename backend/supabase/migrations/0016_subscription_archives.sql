-- Distinguish paused rules from archived/deleted rules.
--
-- Before 1.0b, enabled=false only meant "removed" because there was
-- no pause/resume UI. Migrate those rows to archived so the new public
-- rules panel can use enabled=false for pause without resurrecting old
-- removals.

alter table public.subscriptions
    add column if not exists archived_at timestamptz;

update public.subscriptions
   set archived_at = coalesce(updated_at, created_at, now()),
       enabled = false
 where enabled = false
   and archived_at is null;

drop index if exists subs_scope_enabled_idx;

create index subs_scope_enabled_idx
    on public.subscriptions (scope_kind, scope_id)
    where enabled = true and archived_at is null;

create index if not exists subs_public_user_active_idx
    on public.subscriptions (created_at desc)
    where scope_kind = 'user'
      and enabled = true
      and archived_at is null;
