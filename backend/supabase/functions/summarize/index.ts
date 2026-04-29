// summarize — fills in turns.agent_summary asynchronously.
//
// Milestone 0 (this file): hardcoded "TODO summary" string. No OpenRouter.
// Milestone 2: replace summarize() with a real OpenRouter call per spec §5.3.
//
// Invocation contract (matches Supabase Database Webhook payload shape):
//   POST /functions/v1/summarize
//   { "type": "INSERT", "table": "turns", "record": { id, agent_response_full, ... } }
//
// For manual testing in Milestone 0 you can also send:
//   { "record": { "id": 123 } }

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE  = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const admin = createClient(SUPABASE_URL, SERVICE_ROLE, {
  auth: { persistSession: false, autoRefreshToken: false },
});

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return new Response("method not allowed", { status: 405 });
  }

  let body: { record?: { id?: number; agent_response_full?: string | null } };
  try {
    body = await req.json();
  } catch {
    return new Response("invalid json", { status: 400 });
  }

  const turnId = body.record?.id;
  if (typeof turnId !== "number") {
    return new Response("missing record.id", { status: 400 });
  }

  // Milestone 0 stub: don't call any LLM; just write a placeholder.
  // Milestone 2 will replace this block with an OpenRouter call.
  const summary = "TODO summary (milestone 0 stub)";

  const { error } = await admin
    .from("turns")
    .update({ agent_summary: summary })
    .eq("id", turnId);

  if (error) {
    return new Response(JSON.stringify({ ok: false, error: error.message }), {
      status: 500,
      headers: { "content-type": "application/json" },
    });
  }

  return new Response(JSON.stringify({ ok: true, turn_id: turnId, summary }), {
    headers: { "content-type": "application/json" },
  });
});
