// summarize_project — generate or refresh a (user_id, project_root)
// summary in public.project_summaries.
//
// Body:
//   { "user_id": "<uuid>", "project_root": "/Users/a/Desktop/pmo_agent" }
//
// Behavior:
//   1. Fetch the most recent N turns for this (user, project_root)
//      where the live count differs from the cached turn_count, OR
//      no cache row exists yet.
//   2. If everything is up to date, return ok=true and skipped=true.
//   3. Otherwise: build a compact transcript of recent turns, ask
//      OpenRouter (claude-haiku-4.5) for one paragraph summarizing
//      "what's been happening in this project recently", and upsert
//      the row.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL  = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE  = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const OPENROUTER_KEY = Deno.env.get("OPENROUTER_API_KEY") ?? "";
const MODEL = "anthropic/claude-haiku-4.5";

// Read-side: fetch turns for the project. We only need the recent
// slice — the latest 30 covers the "what's lately happening" ask
// without ballooning input tokens.
const RECENT_TURN_LIMIT = 30;

const admin = createClient(SUPABASE_URL, SERVICE_ROLE, {
  auth: { persistSession: false, autoRefreshToken: false },
});

Deno.serve(async (req) => {
  if (req.method !== "POST") {
    return jsonRes(405, { ok: false, error: "method not allowed" });
  }

  let body: { user_id?: string; project_root?: string };
  try {
    body = await req.json();
  } catch {
    return jsonRes(400, { ok: false, error: "invalid json" });
  }
  if (!body.user_id || !body.project_root) {
    return jsonRes(400, { ok: false, error: "user_id and project_root required" });
  }

  // Compute the live turn count for this (user, root) by listing the
  // matching turns. We need both the count (for the cache key) and
  // the recent N (for the LLM input) — one query handles both.
  const fetched = await fetchProjectTurns(body.user_id, body.project_root);
  if (fetched.error) {
    return jsonRes(500, { ok: false, error: `fetch turns: ${fetched.error}` });
  }
  const matchingTurns = fetched.turns;
  const matchingLiveCount = fetched.liveCount;

  if (matchingTurns.length === 0) {
    // Empty set — caller has invalid project_root or no turns yet.
    return jsonRes(200, { ok: true, skipped: "no turns" });
  }

  // Cache check: read existing row.
  const { data: existing } = await admin
    .from("project_summaries")
    .select("turn_count, summary")
    .eq("user_id", body.user_id)
    .eq("project_root", body.project_root)
    .maybeSingle();

  if (existing && existing.turn_count === matchingLiveCount && existing.summary) {
    return jsonRes(200, {
      ok: true,
      skipped: "fresh",
      summary: existing.summary,
    });
  }

  // Build the LLM input: a compact list of recent (user_message,
  // agent_summary) pairs, oldest first so the model reads the
  // narrative arc.
  if (!OPENROUTER_KEY) {
    return jsonRes(500, { ok: false, error: "OPENROUTER_API_KEY not set" });
  }
  const transcript = matchingTurns
    .slice() // copy so we don't mutate
    .reverse() // oldest first
    .map((t: TurnRow, i: number) => {
      const u = oneLine(t.user_message, 200);
      const a = t.agent_summary ? oneLine(t.agent_summary, 200) : "(summary pending)";
      return `[${i + 1}] USER: ${u}\n    AGENT: ${a}`;
    })
    .join("\n");

  let summary: string;
  try {
    summary = await callOpenRouter(body.project_root, transcript);
  } catch (e) {
    return jsonRes(502, { ok: false, error: `openrouter: ${(e as Error).message}` });
  }

  const lastTurnAt = matchingTurns[0].user_message_at; // newest is first

  const { error: upsertErr } = await admin
    .from("project_summaries")
    .upsert({
      user_id: body.user_id,
      project_root: body.project_root,
      summary,
      turn_count: matchingLiveCount,
      last_turn_at: lastTurnAt,
      generated_at: new Date().toISOString(),
    });
  if (upsertErr) {
    return jsonRes(500, { ok: false, error: `upsert: ${upsertErr.message}` });
  }

  return jsonRes(200, { ok: true, summary, turn_count: matchingLiveCount });
});

type TurnRow = {
  id: number;
  turn_index: number;
  agent: string;
  project_path: string | null;
  project_root: string | null;
  user_message: string;
  agent_summary: string | null;
  user_message_at: string;
};

async function fetchProjectTurns(
  userID: string,
  projectRoot: string,
): Promise<{ turns: TurnRow[]; liveCount: number; error?: string }> {
  const select =
    "id, turn_index, agent, project_path, project_root, user_message, agent_summary, user_message_at";
  const [canonical, legacy] = await Promise.all([
    admin
      .from("turns")
      .select(select, { count: "exact" })
      .eq("user_id", userID)
      .eq("project_root", projectRoot)
      .order("user_message_at", { ascending: false })
      .limit(RECENT_TURN_LIMIT),
    admin
      .from("turns")
      .select(select, { count: "exact" })
      .eq("user_id", userID)
      .is("project_root", null)
      .filter("project_path", "ilike", legacyProjectRootPattern(projectRoot))
      .order("user_message_at", { ascending: false })
      .limit(RECENT_TURN_LIMIT),
  ]);

  if (canonical.error) return { turns: [], liveCount: 0, error: canonical.error.message };
  if (legacy.error) return { turns: [], liveCount: 0, error: legacy.error.message };

  const canonicalRows = (canonical.data ?? []) as TurnRow[];
  const legacyRows = ((legacy.data ?? []) as TurnRow[]).filter(
    (t) => projectRootForTurn(t) === projectRoot,
  );
  const turns = [...canonicalRows, ...legacyRows]
    .sort((a, b) => b.user_message_at.localeCompare(a.user_message_at))
    .slice(0, RECENT_TURN_LIMIT);

  // canonical.count is exact. legacy.count is a coarse SQL pre-filter;
  // keep the previous approximate behavior when the legacy set is large.
  const legacyLiveCount = legacyRows.length === RECENT_TURN_LIMIT
    ? (legacy.count ?? legacyRows.length)
    : legacyRows.length;
  return {
    turns,
    liveCount: (canonical.count ?? canonicalRows.length) + legacyLiveCount,
  };
}

// legacyProjectRootPattern returns an ilike pattern that catches every
// path whose first 4 components may match the given legacy root. It is
// only used for rows created before turns.project_root existed.
function legacyProjectRootPattern(root: string): string {
  // Escape SQL LIKE wildcards in the root itself.
  const safe = root.replace(/[%_]/g, "\\$&");
  return `${safe}%`;
}

function projectRootForTurn(t: Pick<TurnRow, "project_root" | "project_path">): string {
  return t.project_root || legacyProjectRootFromPath(t.project_path);
}

// legacyProjectRootFromPath mirrors the web fallback for old rows.
function legacyProjectRootFromPath(p: string | null | undefined): string {
  if (!p) return "(unknown)";
  const trimmed = p.startsWith("/") ? p.slice(1) : p;
  const parts = trimmed.split("/");
  if (parts.length <= 4) return p;
  return "/" + parts.slice(0, 4).join("/");
}

function oneLine(s: string, n: number): string {
  let out = s.replace(/\s+/g, " ").trim();
  if (out.length > n) out = out.slice(0, n) + "…";
  return out;
}

async function callOpenRouter(
  projectRoot: string,
  transcript: string,
): Promise<string> {
  const projectName = projectRoot.split("/").pop() ?? projectRoot;
  const res = await fetch("https://openrouter.ai/api/v1/chat/completions", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${OPENROUTER_KEY}`,
      "Content-Type": "application/json",
      "HTTP-Referer": "https://pmo-agent-sigma.vercel.app",
      "X-Title": "pmo_agent project summary",
    },
    body: JSON.stringify({
      model: MODEL,
      messages: [
        {
          role: "system",
          content:
            "You write a short paragraph summarizing what's been " +
            "happening in an AI coding project lately. The user pastes " +
            "a transcript of recent (user, agent) turn pairs from one " +
            `project ("${projectName}"). Write 2–3 sentences capturing ` +
            "the main thread of work — what the user is building, what " +
            "the agent is doing for them, what's been resolved or is " +
            "in flight. Concrete > generic: name files, decisions, " +
            "milestones, problems hit, when present. Match the dominant " +
            "language of the transcript (Chinese if mostly Chinese). " +
            "Output ONLY the paragraph — no quotes, headers, lists.",
        },
        {
          role: "user",
          content: `<transcript project="${projectName}">\n${transcript}\n</transcript>`,
        },
      ],
      // Chinese characters often eat 2–3 tokens each; 600 leaves room
      // for a full 2–3 sentence paragraph in either language without
      // truncation.
      max_tokens: 600,
      temperature: 0.3,
    }),
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    throw new Error(`HTTP ${res.status}: ${text.slice(0, 200)}`);
  }
  const json = await res.json();
  const out: string | undefined = json?.choices?.[0]?.message?.content;
  if (!out) throw new Error("no content");
  return out.trim();
}

function jsonRes(status: number, body: object): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}
