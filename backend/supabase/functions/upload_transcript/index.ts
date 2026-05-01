// upload_transcript — accepts gzip-compressed raw JSONL session snapshots.
//
// Daemon contract:
//   POST /functions/v1/upload_transcript
//   Authorization: Bearer pmo_<plaintext>
//   Content-Type: application/gzip
//   X-PMO-Transcript-Metadata: base64(JSON)
//   Body: gzip-compressed raw JSONL bytes

import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
const SERVICE_ROLE = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;
const BUCKET = "raw-transcripts";
const MAX_COMPRESSED_BYTES = 50 * 1024 * 1024;

const admin = createClient(SUPABASE_URL, SERVICE_ROLE, {
  auth: { persistSession: false, autoRefreshToken: false },
});

type UploadMetadata = {
  agent: string;
  agent_session_id: string;
  project_path?: string | null;
  project_root?: string | null;
  local_path?: string | null;
  byte_size: number;
  compressed_size: number;
  line_count?: number | null;
  sha256: string;
  last_mtime?: string | null;
};

function bad(status: number, message: string) {
  return new Response(JSON.stringify({ ok: false, error: message }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

async function sha256Hex(input: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(input));
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

Deno.serve(async (req) => {
  if (req.method !== "POST") return bad(405, "method not allowed");

  const authz = req.headers.get("authorization") ?? "";
  const m = authz.match(/^Bearer\s+(pmo_[A-Za-z0-9_\-]{16,})$/);
  if (!m) return bad(401, "missing or malformed bearer token");
  const tokenPlain = m[1];

  const tokenHash = await sha256Hex(tokenPlain);
  const { data: tokenRow, error: tokenErr } = await admin
    .from("tokens")
    .select("id, user_id, label, revoked_at")
    .eq("token_hash", tokenHash)
    .maybeSingle();

  if (tokenErr) return bad(500, `token lookup failed: ${tokenErr.message}`);
  if (!tokenRow) return bad(401, "invalid token");
  if (tokenRow.revoked_at) return bad(401, "token revoked");

  const meta = parseMetadata(req.headers.get("x-pmo-transcript-metadata"));
  if (!meta.ok) return bad(400, meta.error);
  const p = meta.value;
  const validation = validateMetadata(p);
  if (validation) return bad(400, validation);

  if ((req.headers.get("content-type") ?? "").split(";")[0] !== "application/gzip") {
    return bad(400, "content-type must be application/gzip");
  }

  const body = new Uint8Array(await req.arrayBuffer());
  if (body.byteLength === 0) return bad(400, "empty body");
  if (body.byteLength > MAX_COMPRESSED_BYTES) {
    return bad(413, "compressed transcript exceeds 50MB limit");
  }
  if (body.byteLength < 2 || body[0] !== 0x1f || body[1] !== 0x8b) {
    return bad(400, "body must be gzip-compressed JSONL");
  }
  if (body.byteLength !== p.compressed_size) {
    return bad(400, "compressed_size does not match request body");
  }

  const storagePath = [
    tokenRow.user_id,
    safeSegment(p.agent),
    `${safeSegment(p.agent_session_id)}.jsonl.gz`,
  ].join("/");

  const { error: storageErr } = await admin.storage
    .from(BUCKET)
    .upload(storagePath, new Blob([body], { type: "application/gzip" }), {
      contentType: "application/gzip",
      upsert: true,
    });
  if (storageErr) return bad(500, `storage upload failed: ${storageErr.message}`);

  const { data: existing, error: existingErr } = await admin
    .from("transcript_files")
    .select("upload_generation")
    .eq("user_id", tokenRow.user_id)
    .eq("agent", p.agent)
    .eq("agent_session_id", p.agent_session_id)
    .maybeSingle();
  if (existingErr) return bad(500, `lookup transcript metadata failed: ${existingErr.message}`);

  const generation = (existing?.upload_generation ?? 0) + 1;
  const { error: upsertErr } = await admin
    .from("transcript_files")
    .upsert(
      {
        user_id: tokenRow.user_id,
        agent: p.agent,
        agent_session_id: p.agent_session_id,
        device_label: tokenRow.label ?? null,
        project_path: emptyToNull(p.project_path),
        project_root: emptyToNull(p.project_root),
        local_path: emptyToNull(p.local_path),
        storage_bucket: BUCKET,
        storage_path: storagePath,
        byte_size: p.byte_size,
        compressed_size: p.compressed_size,
        line_count: p.line_count ?? null,
        sha256: p.sha256,
        last_mtime: emptyToNull(p.last_mtime),
        last_uploaded_at: new Date().toISOString(),
        upload_generation: generation,
      },
      { onConflict: "user_id,agent,agent_session_id" },
    );
  if (upsertErr) return bad(500, `upsert transcript metadata failed: ${upsertErr.message}`);

  await admin.from("tokens").update({ last_used_at: new Date().toISOString() }).eq("id", tokenRow.id);

  return new Response(
    JSON.stringify({
      ok: true,
      storage_bucket: BUCKET,
      storage_path: storagePath,
      upload_generation: generation,
    }),
    { headers: { "content-type": "application/json" } },
  );
});

function parseMetadata(raw: string | null):
  | { ok: true; value: UploadMetadata }
  | { ok: false; error: string } {
  if (!raw) return { ok: false, error: "missing x-pmo-transcript-metadata" };
  try {
    const decoded = atob(raw);
    const bytes = Uint8Array.from(decoded, (c) => c.charCodeAt(0));
    return { ok: true, value: JSON.parse(new TextDecoder().decode(bytes)) };
  } catch {
    return { ok: false, error: "invalid x-pmo-transcript-metadata" };
  }
}

function validateMetadata(p: UploadMetadata): string | null {
  if (!["claude_code", "codex"].includes(p.agent)) return "agent must be claude_code or codex";
  if (typeof p.agent_session_id !== "string" || !p.agent_session_id) {
    return "agent_session_id required";
  }
  if (!Number.isSafeInteger(p.byte_size) || p.byte_size < 0) return "byte_size invalid";
  if (!Number.isSafeInteger(p.compressed_size) || p.compressed_size < 0) {
    return "compressed_size invalid";
  }
  if (p.line_count != null && (!Number.isSafeInteger(p.line_count) || p.line_count < 0)) {
    return "line_count invalid";
  }
  if (typeof p.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(p.sha256)) {
    return "sha256 must be 64 lowercase hex chars";
  }
  return null;
}

function safeSegment(s: string): string {
  return s.replace(/[^A-Za-z0-9._-]/g, "_");
}

function emptyToNull(s: string | null | undefined): string | null {
  if (!s || !s.trim()) return null;
  return s;
}
