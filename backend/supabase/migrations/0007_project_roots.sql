-- Store the canonical project grouping key separately from the raw cwd.
--
-- project_path remains the agent's original working directory for
-- debugging/audit. project_root is resolved by the daemon, usually via
-- `git rev-parse --show-toplevel`, and is what readers should use for
-- grouping/filtering/summaries.

alter table public.turns
    add column if not exists project_root text;

comment on column public.turns.project_root is
    'Canonical project grouping key. Usually the nearest git root for project_path; falls back to project_path outside git.';

create index if not exists turns_user_project_root_time
    on public.turns (user_id, project_root, user_message_at desc);
