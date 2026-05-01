-- Store raw JSONL transcript snapshots for internal debugging and future
-- search. The raw bytes live in private Supabase Storage; Postgres keeps
-- only metadata and lookup keys.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'raw-transcripts',
    'raw-transcripts',
    false,
    52428800,
    array['application/gzip']::text[]
)
on conflict (id) do update
set public = false,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

create table public.transcript_files (
    id                  bigserial primary key,
    user_id             uuid not null references auth.users(id) on delete cascade,
    agent               text not null check (agent in ('claude_code', 'codex')),
    agent_session_id    text not null,
    device_label        text,
    project_path        text,
    project_root        text,
    local_path          text,
    storage_bucket      text not null default 'raw-transcripts',
    storage_path        text not null,
    byte_size           bigint not null check (byte_size >= 0),
    compressed_size     bigint not null check (compressed_size >= 0),
    line_count          int check (line_count is null or line_count >= 0),
    sha256              text not null,
    last_mtime          timestamptz,
    first_seen_at       timestamptz not null default now(),
    last_uploaded_at    timestamptz not null default now(),
    upload_generation   int not null default 1,
    unique (user_id, agent, agent_session_id)
);

comment on table public.transcript_files is
    'Private raw JSONL transcript snapshot metadata. Storage object contains gzip-compressed raw JSONL.';

create index transcript_files_user_uploaded
    on public.transcript_files (user_id, last_uploaded_at desc);

create index transcript_files_project_uploaded
    on public.transcript_files (user_id, project_root, last_uploaded_at desc);

alter table public.transcript_files enable row level security;

create policy "users read own transcript metadata"
    on public.transcript_files for select using (auth.uid() = user_id);
