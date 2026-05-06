-- Proactive PMO notifications 2.0a review hardening.
-- Webhook events use repo identifiers in events.project_root
-- (e.g. github:owner/repo), not developer-local filesystem paths.

update public.events
   set user_id = null
 where source in ('github', 'gitea')
   and user_id is not null;

update public.events
   set project_root = lower(source || ':' || (payload #>> '{repo,full_name}')),
       payload = jsonb_set(
           jsonb_set(
               payload,
               '{project_root}',
               to_jsonb(lower(source || ':' || (payload #>> '{repo,full_name}'))),
               true
           ),
           '{repo,project_root}',
           to_jsonb(lower(source || ':' || (payload #>> '{repo,full_name}'))),
           true
       )
 where source in ('github', 'gitea')
   and payload #>> '{repo,full_name}' is not null;

create or replace function public.index_subscription_metadata(
    p_subscription_id uuid
) returns void
language plpgsql
security definer
as $$
declare
    desc_lower text;
    matched jsonb;
    k_hash text;
begin
    select lower(description)
      into desc_lower
      from public.subscriptions
     where id = p_subscription_id
     for update;

    if desc_lower is null then
        return;
    end if;

    with tokens as (
        select distinct
               case
                   when lower(project_root) ~ '^[a-z][a-z0-9_+.-]*:[^/\s]+/[^/\s]+$'
                   then lower(project_root)
                   else lower(regexp_replace(project_root, '^.*/', ''))
               end as token
          from public.events
         where project_root is not null
           and project_root <> ''
    ), clean as (
        select token
          from tokens
         where token <> ''
    ), hashed as (
        select substr(
                   encode(extensions.digest(coalesce(string_agg(token, '|' order by token), ''), 'sha256'), 'hex'),
                   1,
                   16
               ) as h
          from clean
    ), escaped as (
        select token,
               regexp_replace(token, '([\\^$.|?*+(){}\[\]])', E'\\\\\\1', 'g') as token_re
          from clean
    ), matches as (
        select token
          from escaped
         where (
             token ~ '^[a-z][a-z0-9_+.-]*:[^/\s]+/[^/\s]+$'
             and position(token in desc_lower) > 0
         ) or (
             length(token) >= 4
             and token !~ '^[a-z][a-z0-9_+.-]*:[^/\s]+/[^/\s]+$'
             and desc_lower ~ ('\m' || token_re || '\M')
         ) or (
             length(token) < 4
             and (
                 desc_lower ~ ('\mproject[\s\-_:]*' || token_re || '\M')
                 or desc_lower ~ ('项目[\s\-_:''`"]*' || token_re || '($|[\s''`"])')
                 or desc_lower ~ ('`' || token_re || '`')
                 or desc_lower ~ ('/' || token_re || '(/|$|[^a-z0-9])')
                 or desc_lower ~ ('"' || token_re || '"')
             )
         )
    )
    select coalesce((select jsonb_agg(token order by token) from matches), '[]'::jsonb),
           (select h from hashed)
      into matched, k_hash;

    update public.subscriptions
       set metadata = jsonb_build_object(
               'matched_projects', matched,
               'project_tokens_hash', k_hash,
               'indexed_at', now()
           ),
           updated_at = now()
     where id = p_subscription_id;
end $$;

revoke execute on function public.index_subscription_metadata(uuid) from public;
revoke execute on function public.index_subscription_metadata(uuid) from anon;
revoke execute on function public.index_subscription_metadata(uuid) from authenticated;
grant execute on function public.index_subscription_metadata(uuid) to service_role;

alter function public.index_subscription_metadata(uuid)
    set search_path = public, pg_temp;
