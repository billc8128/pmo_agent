-- Proactive PMO notifications 2.0c Phase 1.
-- Opt-in Feishu chat memory and lightweight PMO people memory.

create table if not exists public.chat_memory_settings (
    chat_id              text primary key,
    enabled              boolean not null default false,
    enabled_at           timestamptz,
    enabled_by_user_id   uuid references public.profiles(id) on delete set null,
    enabled_by_open_id   text,
    disabled_at          timestamptz,
    disabled_by_user_id  uuid references public.profiles(id) on delete set null,
    disabled_by_open_id  text,
    retention_days       int not null default 90 check (retention_days between 1 and 730),
    observer_enabled     boolean not null default false,
    people_loop_cursor   timestamptz,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now()
);

create table if not exists public.chat_memory_settings_history (
    id                  bigserial primary key,
    chat_id             text not null
                        references public.chat_memory_settings(chat_id) on delete cascade,
    action              text not null check (
                          action in ('enable', 'disable', 'retention_change',
                                     'observer_enable', 'observer_disable',
                                     'clear_history')
                        ),
    actor_user_id       uuid references public.profiles(id) on delete set null,
    actor_open_id       text,
    old_value           jsonb,
    new_value           jsonb,
    created_at          timestamptz not null default now()
);

create table if not exists public.chat_messages (
    id                  bigserial primary key,
    feishu_message_id   text not null unique,
    chat_id             text not null
                        references public.chat_memory_settings(chat_id) on delete cascade,
    chat_type           text not null check (chat_type in ('group', 'p2p')),
    sender_open_id      text not null,
    sender_user_id      uuid references public.profiles(id) on delete set null,
    sender_display_name text,
    message_type        text not null default 'text' check (
                          message_type in ('text', 'post', 'share_chat',
                                           'file', 'link', 'unknown')
                        ),
    text_redacted       text not null default '' check (length(text_redacted) > 0),
    is_at_bot           boolean not null default false,
    sender_is_bot       boolean not null default false,
    parent_message_id   text,
    root_message_id     text,
    mentions            jsonb not null default '[]'::jsonb,
    redacted_payload    jsonb not null default '{}'::jsonb,
    content_metadata    jsonb not null default '{}'::jsonb,
    occurred_at         timestamptz not null,
    ingested_at         timestamptz not null default now(),
    edited_at           timestamptz,
    deleted_at          timestamptz
);

create index if not exists chat_messages_chat_time_idx
    on public.chat_messages (chat_id, occurred_at desc);

create index if not exists chat_messages_sender_time_idx
    on public.chat_messages (sender_open_id, occurred_at desc);

create index if not exists chat_messages_parent_idx
    on public.chat_messages (parent_message_id)
    where parent_message_id is not null;

create index if not exists chat_messages_text_fts_idx
    on public.chat_messages
    using gin (to_tsvector('simple', text_redacted));

create index if not exists chat_messages_deleted_idx
    on public.chat_messages (chat_id, deleted_at)
    where deleted_at is not null;

create table if not exists public.people_memory (
    person_key        text primary key,
    profile_id        uuid references public.profiles(id) on delete set null,
    feishu_open_id    text,
    display_name      text,
    handle            text,
    pmo_notes         text not null default '',
    notes_updated_at  timestamptz,
    last_observed_at  timestamptz,
    metadata          jsonb not null default '{}'::jsonb,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    constraint people_memory_identity_check check (
        person_key like 'profile:%' or person_key like 'feishu:%'
    )
);

create unique index if not exists people_memory_profile_idx
    on public.people_memory (profile_id)
    where profile_id is not null;

create unique index if not exists people_memory_feishu_idx
    on public.people_memory (feishu_open_id)
    where feishu_open_id is not null;

create index if not exists people_memory_last_observed_idx
    on public.people_memory (last_observed_at desc);

create table if not exists public.people_memory_updates (
    id             bigserial primary key,
    person_key     text not null references public.people_memory(person_key) on delete cascade,
    update_source  text not null check (
                     update_source in ('background_loop', 'identity_merge', 'manual_repair')
                   ),
    model          text,
    input_tokens   int,
    output_tokens  int,
    old_note_hash  text,
    new_note_hash  text,
    created_at     timestamptz not null default now()
);

create index if not exists people_memory_updates_source_time_idx
    on public.people_memory_updates (update_source, created_at desc);

create index if not exists people_memory_updates_person_time_idx
    on public.people_memory_updates (person_key, created_at desc);

drop trigger if exists chat_memory_settings_set_updated_at on public.chat_memory_settings;
create trigger chat_memory_settings_set_updated_at
    before update on public.chat_memory_settings
    for each row execute function public.set_updated_at();

drop trigger if exists people_memory_set_updated_at on public.people_memory;
create trigger people_memory_set_updated_at
    before update on public.people_memory
    for each row execute function public.set_updated_at();

alter table public.chat_memory_settings enable row level security;
alter table public.chat_memory_settings_history enable row level security;
alter table public.chat_messages enable row level security;
alter table public.people_memory enable row level security;
alter table public.people_memory_updates enable row level security;

comment on table public.chat_memory_settings is
    'Current opt-in state for Feishu chat memory. User-facing access goes through bot tools.';
comment on table public.chat_memory_settings_history is
    'Audit trail for chat memory enable/disable/retention/observer changes.';
comment on table public.chat_messages is
    'Redacted Feishu chat memory rows for opted-in chats. Raw encrypted webhook bodies and binary content are not stored.';
comment on column public.chat_messages.text_redacted is
    'Redacted visible text or [REDACTED] when all visible text was removed.';
comment on column public.chat_messages.redacted_payload is
    'Parsed redacted payload needed for diagnostics and future extraction; not the raw Feishu body.';
comment on column public.chat_messages.content_metadata is
    'Safe structured facts such as shared-link title/URL or file name; no binary content.';
comment on table public.people_memory is
    'PMO work-context notes keyed by profile or Feishu open_id. Notes are not directly exposed to conversational agents.';
comment on table public.people_memory_updates is
    'Audit and cost accounting rows for people-memory rewrites and identity merges.';
