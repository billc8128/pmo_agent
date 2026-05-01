// ingest — daemon's write path. Validates a PAT and inserts a turn.
//
// Why a custom Edge Function instead of direct PostgREST + Supabase Auth JWT?
// See docs/specs/2026-04-29-mvp-design.md §5.2 + the brainstorming decision
// to use option (c): custom token table, ingest via Edge Function. This makes
// PAT revocation immediate (set tokens.revoked_at = now()) instead of waiting
// for a long-lived JWT to expire.
//
// Daemon contract:
//   POST /functions/v1/ingest
//   Authorization: Bearer pmo_<plaintext>
//   Content-Type: application/json
//   {
//     "agent": "claude_code" | "codex",
//     "agent_session_id": "uuid-from-jsonl-filename",
//     "project_path": "/Users/.../some/repo" | null,
//     "project_root": "/Users/.../some/repo" | null,
//     "turn_index": 0,
//     "user_message": "redacted prompt text",
//     "agent_response_full": "redacted response text" | null,
//     "user_message_at": "2026-04-30T12:34:56.789Z",
//     "agent_response_at": "2026-04-30T12:35:01.123Z" | null
//   }
//
// Idempotency: relies on the turns_dedup unique index
//   (user_id, agent, agent_session_id, turn_index).
// Re-uploads of the same logical turn are no-ops.

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

const admin = createClient(SUPABASE_URL, SERVICE_ROLE, {
  auth: { persistSession: false, autoRefreshToken: false },
});

type TurnPayload = {
  agent: string;
  agent_session_id: string;
  project_path: string | null;
  project_root?: string | null;
  turn_index: number;
  user_message: string;
  agent_response_full: string | null;
  user_message_at: string;
  agent_response_at: string | null;
};

async function sha256Hex(input: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(input));
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function bad(status: number, message: string) {
  return new Response(JSON.stringify({ ok: false, error: message }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return bad(405, "method not allowed");

  // 1. Extract PAT.
  const authz = req.headers.get("authorization") ?? "";
  const m = authz.match(/^Bearer\s+(pmo_[A-Za-z0-9_\-]{16,})$/);
  if (!m) return bad(401, "missing or malformed bearer token");
  const tokenPlain = m[1];

  // 2. Hash and look up. RLS-enabled table, but service_role bypasses RLS.
  //    We pull the token's `label` too so we can stamp the device on
  //    each turn — visible on the public timeline so multi-machine
  //    users can tell which laptop / desktop a turn came from.
  const tokenHash = await sha256Hex(tokenPlain);
  const { data: tokenRow, error: tokenErr } = await admin
    .from("tokens")
    .select("id, user_id, label, revoked_at")
    .eq("token_hash", tokenHash)
    .maybeSingle();

  if (tokenErr) return bad(500, `token lookup failed: ${tokenErr.message}`);
  if (!tokenRow) return bad(401, "invalid token");
  if (tokenRow.revoked_at) return bad(401, "token revoked");

  // 3. Parse + minimally validate payload. Trust the daemon for content; this
  //    layer only enforces shape. (Redaction is the daemon's job, before upload.)
  let p: TurnPayload;
  try {
    p = await req.json();
  } catch {
    return bad(400, "invalid json");
  }
  if (typeof p.agent !== "string" || !["claude_code", "codex"].includes(p.agent)) {
    return bad(400, "agent must be 'claude_code' or 'codex'");
  }
  if (typeof p.agent_session_id !== "string" || !p.agent_session_id) {
    return bad(400, "agent_session_id required");
  }
  if (!Number.isInteger(p.turn_index) || p.turn_index < 0) {
    return bad(400, "turn_index must be a non-negative integer");
  }
  if (typeof p.user_message !== "string") {
    return bad(400, "user_message required");
  }
  if (typeof p.user_message_at !== "string") {
    return bad(400, "user_message_at required (ISO-8601 string)");
  }
  if (
    p.project_root !== undefined &&
    p.project_root !== null &&
    typeof p.project_root !== "string"
  ) {
    return bad(400, "project_root must be a string or null");
  }

  // 4. Upsert. ON CONFLICT (turns_dedup) DO NOTHING — re-uploads are no-ops.
  const { data: inserted, error: insertErr } = await admin
    .from("turns")
    .upsert(
      {
        user_id: tokenRow.user_id,
        agent: p.agent,
        agent_session_id: p.agent_session_id,
        project_path: p.project_path,
        project_root: normalizeProjectRoot(p.project_root, p.project_path),
        turn_index: p.turn_index,
        user_message: p.user_message,
        agent_response_full: p.agent_response_full,
        user_message_at: p.user_message_at,
        agent_response_at: p.agent_response_at,
        device_label: tokenRow.label ?? null,
      },
      { onConflict: "user_id,agent,agent_session_id,turn_index", ignoreDuplicates: true },
    )
    .select("id")
    .maybeSingle();

  if (insertErr) return bad(500, `insert failed: ${insertErr.message}`);

  // 5. Best-effort: bump last_used_at. Don't fail the request if this fails.
  await admin.from("tokens").update({ last_used_at: new Date().toISOString() }).eq("id", tokenRow.id);

  return new Response(
    JSON.stringify({ ok: true, turn_id: inserted?.id ?? null, deduped: inserted === null }),
    { headers: { "content-type": "application/json" } },
  );
});

function normalizeProjectRoot(
  projectRoot: string | null | undefined,
  projectPath: string | null,
): string | null {
  if (projectRoot && projectRoot.trim()) return projectRoot;
  if (!projectPath) return null;
  return legacyProjectRootFromPath(projectPath);
}

// Legacy fallback for old daemons that have not yet been upgraded to
// send project_root. New daemons resolve git roots locally.
function legacyProjectRootFromPath(p: string): string {
  const trimmed = p.startsWith("/") ? p.slice(1) : p;
  const parts = trimmed.split("/");
  if (parts.length <= 4) return p;
  return "/" + parts.slice(0, 4).join("/");
}
