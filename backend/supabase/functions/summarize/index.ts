// summarize — fills in turns.agent_summary asynchronously.
//
// Triggered by a Postgres trigger on public.turns INSERT (set up in
// migration 0002). Body shape (Supabase Database Webhooks payload):
//
//   { "type": "INSERT", "table": "turns", "record": { id, agent_response_full, ... } }
//
// On success: UPDATE public.turns SET agent_summary = <one-sentence> WHERE id = <record.id>.
// On failure: leaves agent_summary NULL — the web UI shows "Summary unavailable"
// with a manual retry path. We do NOT auto-retry server-side; an errored summary
// is a stale state, not a stuck job.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL  = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE  = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const OPENROUTER_KEY = Deno.env.get("OPENROUTER_API_KEY") ?? "";

// Per spec §5.3. Easy to swap to a different model via this string.
const MODEL = "anthropic/claude-haiku-4.5";

const admin = createClient(SUPABASE_URL, SERVICE_ROLE, {
  auth: { persistSession: false, autoRefreshToken: false },
});

Deno.serve(async (req) => {
  if (req.method !== "POST") return new Response("method not allowed", { status: 405 });

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

  // The webhook payload includes the inserted row's columns. If
  // agent_response_full is absent (e.g. invoked by hand for testing),
  // fetch it.
  let text = body.record?.agent_response_full ?? null;
  if (!text) {
    const { data, error } = await admin
      .from("turns")
      .select("agent_response_full")
      .eq("id", turnId)
      .maybeSingle();
    if (error) {
      return errResponse(500, `fetch turn: ${error.message}`);
    }
    text = data?.agent_response_full ?? null;
  }
  if (!text || text.trim() === "") {
    // Nothing to summarize. Leave agent_summary NULL so it doesn't
    // look "summarized but empty".
    return new Response(JSON.stringify({ ok: true, turn_id: turnId, skipped: "empty" }), {
      headers: { "content-type": "application/json" },
    });
  }

  if (!OPENROUTER_KEY) {
    return errResponse(500, "OPENROUTER_API_KEY not set in function secrets");
  }

  let summary: string;
  try {
    summary = await summarizeViaOpenRouter(text);
  } catch (e) {
    return errResponse(502, `openrouter: ${(e as Error).message}`);
  }

  const { error: upErr } = await admin
    .from("turns")
    .update({ agent_summary: summary })
    .eq("id", turnId);
  if (upErr) {
    return errResponse(500, `update turn: ${upErr.message}`);
  }

  return new Response(JSON.stringify({ ok: true, turn_id: turnId, summary }), {
    headers: { "content-type": "application/json" },
  });
});

async function summarizeViaOpenRouter(text: string): Promise<string> {
  // Truncate very long agent responses before sending. Haiku handles
  // long input fine, but we don't need it — a one-sentence summary
  // doesn't get better past ~4 KB of context.
  const MAX_CHARS = 8000;
  const trimmed = text.length > MAX_CHARS
    ? text.slice(0, MAX_CHARS) + "\n[…truncated]"
    : text;

  const res = await fetch("https://openrouter.ai/api/v1/chat/completions", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${OPENROUTER_KEY}`,
      "Content-Type": "application/json",
      // OpenRouter encourages identifying your app for analytics/quota.
      "HTTP-Referer": "https://pmo-agent.vercel.app",
      "X-Title": "pmo_agent",
    },
    body: JSON.stringify({
      model: MODEL,
      messages: [
        {
          role: "system",
          content:
            // The summary is a public timeline entry — readers scan many
            // of them in a row, so we want the dense, changelog-style.
            "You write timeline entries summarizing what an AI coding " +
            "assistant did in one turn. The input includes prose AND " +
            "bracketed tool calls like [Bash], [Edit], [Read], [Write], " +
            "[Grep], [Task]. Tool calls show what was actually executed " +
            "(commands, file paths, search patterns) — treat them as " +
            "first-class evidence of what was done, not noise.\n\n" +
            "Write a dense, concrete summary (1–3 short clauses, ≤40 " +
            "words total) that captures: (a) the substantive result or " +
            "decision, and (b) at least one specific action taken — a " +
            "command run, a file edited, a bug found, a decision made. " +
            "Use semicolons or em-dashes to separate clauses. Mention " +
            "specific file names, commands, or numbers when present. " +
            "Avoid generic verbs like 'helped', 'discussed', 'addressed'.\n\n" +
            "Hard rules:\n" +
            "1. ALWAYS produce a summary, even if the response is " +
            "short or has no tool calls. Brief responses get brief " +
            "summaries (e.g. '确认进入下一步' or 'Acknowledged; " +
            "starting next step'). NEVER refuse, NEVER ask for more " +
            "context, NEVER reply 'I need more information' — just " +
            "summarize whatever is there in one short clause.\n" +
            "2. PARAPHRASE — do not copy or quote sentences from the " +
            "input verbatim, even when the input is itself a list or " +
            "bullet structure. Compress to your own words.\n" +
            "3. Never address the user ('you', '你'). Never continue " +
            "the conversation. Output ONLY the summary — no quotes, " +
            "no preface, no markdown headers, no labels.\n" +
            "4. Match the dominant language of the response (Chinese " +
            "if mostly Chinese, English if mostly English).",
        },
        {
          role: "user",
          content: `<response>\n${trimmed}\n</response>`,
        },
      ],
      max_tokens: 200,
      // Keep summaries deterministic-ish so re-runs don't drift.
      temperature: 0.2,
    }),
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${detail.slice(0, 200)}`);
  }
  const json = await res.json();
  const out: string | undefined = json?.choices?.[0]?.message?.content;
  if (!out) {
    throw new Error("no content in response");
  }
  return out.trim();
}

function errResponse(status: number, msg: string): Response {
  return new Response(JSON.stringify({ ok: false, error: msg }), {
    status,
    headers: { "content-type": "application/json" },
  });
}
