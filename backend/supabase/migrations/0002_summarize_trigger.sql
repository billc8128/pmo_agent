-- After every public.turns INSERT, asynchronously call the summarize
-- Edge Function so agent_summary gets populated within ~5s.
--
-- We use pg_net (Supabase's HTTP client extension) for fire-and-forget
-- POST so the daemon's INSERT is never blocked by OpenRouter latency.
-- pg_net writes the request to a queue table; a background worker
-- delivers it.
--
-- Why a Postgres trigger instead of Supabase Database Webhooks?
--   - The trigger lives in this migration, so it's version-controlled
--     and reproducible across environments.
--   - Database Webhooks are configured in the dashboard and don't
--     appear in git diffs.
--
-- Failure handling: if the HTTP request fails (Edge Function down,
-- OpenRouter rate-limited), agent_summary stays NULL. The web UI shows
-- "Summary unavailable" with a manual retry path. This matches spec §5.3.

create extension if not exists pg_net;

create or replace function public.trigger_summarize()
returns trigger
language plpgsql
security definer
as $$
declare
    fn_url text := 'https://xecnsibhijdlwqulkxor.supabase.co/functions/v1/summarize';
begin
    -- pg_net is async: returns a request id immediately. We don't
    -- block the transaction or watch for the response here.
    perform net.http_post(
        url := fn_url,
        body := jsonb_build_object(
            'type',   'INSERT',
            'table',  'turns',
            'record', jsonb_build_object('id', new.id)
        ),
        headers := jsonb_build_object(
            'Content-Type', 'application/json'
        ),
        timeout_milliseconds := 30000
    );
    return new;
end;
$$;

drop trigger if exists turns_summarize on public.turns;
create trigger turns_summarize
    after insert on public.turns
    for each row execute function public.trigger_summarize();
