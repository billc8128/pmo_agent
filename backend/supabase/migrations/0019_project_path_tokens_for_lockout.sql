-- Include event payload project_path leafs in project-name lockout metadata.
--
-- events.project_root is the canonical grouping root. In repos that contain
-- multiple user-visible projects, it can be a parent directory such as
-- /Users/castheart/Documents/vibe while the subscription says "vibelive".
-- The turn payload still carries project_path=/.../vibe/vibelive, so both
-- SQL metadata and Python lockout must treat that leaf as a real project token.

create or replace function public.index_subscription_metadata(
    p_subscription_id uuid
) returns void
language plpgsql
security definer
as $$
declare
    desc_lower text;
    match_desc_lower text;
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

    match_desc_lower := regexp_replace(
        desc_lower,
        '((不要|别|禁止|排除|不通知|不关注|不看|除了)[^，。；;,.]*(（[^）]*）|\([^)]*\))?|\m(exclude|except|not)\M[^，。；;,.]*(\([^)]*\))?)',
        ' ',
        'g'
    );

    with raw_tokens as (
        select project_root as path
          from public.events
         where project_root is not null
           and project_root <> ''
        union all
        select payload->>'project_path' as path
          from public.events
         where payload ? 'project_path'
           and coalesce(payload->>'project_path', '') <> ''
        union all
        select payload->>'project_root' as path
          from public.events
         where payload ? 'project_root'
           and coalesce(payload->>'project_root', '') <> ''
    ), clean as (
        select distinct lower(regexp_replace(path, '^.*/', '')) as token
          from raw_tokens
         where path is not null
           and path <> ''
    ), filtered as (
        select token
          from clean
         where token <> ''
    ), hashed as (
        select substr(
                   encode(extensions.digest(coalesce(string_agg(token, '|' order by token), ''), 'sha256'), 'hex'),
                   1,
                   16
               ) as h
          from filtered
    ), escaped as (
        select token,
               regexp_replace(token, '([\\^$.|?*+(){}\[\]])', E'\\\\\\1', 'g') as token_re
          from filtered
    ), matches as (
        select token
          from escaped
         where (
             length(token) >= 4
             and match_desc_lower ~ ('\m' || token_re || '\M')
         ) or (
             length(token) < 4
             and (
                 match_desc_lower ~ ('\mproject[\s\-_:]*' || token_re || '\M')
                 or match_desc_lower ~ ('项目[\s\-_:''`"]*' || token_re || '($|[\s''`"])')
                 or match_desc_lower ~ ('`' || token_re || '`')
                 or match_desc_lower ~ ('/' || token_re || '(/|$|[^a-z0-9])')
                 or match_desc_lower ~ ('"' || token_re || '"')
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

do $$
declare
    sub record;
begin
    for sub in
        select id
          from public.subscriptions
    loop
        perform public.index_subscription_metadata(sub.id);
    end loop;
end $$;
