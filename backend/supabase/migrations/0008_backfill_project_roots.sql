-- Backfill project_root for turns that predate daemon-side git-root
-- resolution. This is intentionally conservative: it only folds a
-- child cwd into a previously observed parent cwd when the first child
-- segment is a common monorepo/app subdirectory. Otherwise the raw cwd
-- remains its own project root.

with cleaned as (
    select
        id,
        user_id,
        regexp_replace(project_path, '/+$', '') as clean_path
    from public.turns
    where project_root is null
      and project_path is not null
),
observed as (
    select distinct
        user_id,
        regexp_replace(project_path, '/+$', '') as clean_path
    from public.turns
    where project_path is not null
),
resolved as (
    select
        c.id,
        case
            when position('/.claude/worktrees/' in c.clean_path) > 0 then
                substring(c.clean_path from 1 for position('/.claude/worktrees/' in c.clean_path) - 1)
            else coalesce(
                (
                    select o.clean_path
                    from observed o
                    where o.user_id = c.user_id
                      and left(c.clean_path, length(o.clean_path) + 1) = o.clean_path || '/'
                      and split_part(substring(c.clean_path from length(o.clean_path) + 2), '/', 1) = any (
                          array[
                              'android',
                              'api',
                              'app',
                              'apps',
                              'backend',
                              'bot',
                              'client',
                              'cmd',
                              'daemon',
                              'frontend',
                              'functions',
                              'ios',
                              'mobile',
                              'packages',
                              'server',
                              'src',
                              'supabase',
                              'web'
                          ]
                      )
                    order by length(o.clean_path) desc
                    limit 1
                ),
                c.clean_path
            )
        end as project_root
    from cleaned c
)
update public.turns t
set project_root = r.project_root
from resolved r
where t.id = r.id
  and t.project_root is null
  and r.project_root is not null;
