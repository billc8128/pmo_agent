#!/usr/bin/env node

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const env = {
  ...parseEnv(path.join(root, 'web/.env.local')),
  ...parseEnv(path.join(root, 'backend/.env.local')),
  ...parseEnv(path.join(root, 'bot/.env')),
  ...process.env,
};

const supabaseURL = env.SUPABASE_URL || env.SUPABASE_PROJECT_URL || env.NEXT_PUBLIC_SUPABASE_URL;
const serviceRole = env.SUPABASE_SERVICE_ROLE_KEY;
const anonKey = env.SUPABASE_ANON_KEY || env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
const apply = process.argv.includes('--apply');
const apiKey = apply ? serviceRole : (serviceRole || anonKey);
const COMMON_PROJECT_SUBDIRS = new Set([
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
  'web',
]);

if (!supabaseURL || !apiKey) {
  throw new Error('SUPABASE_URL/NEXT_PUBLIC_SUPABASE_URL and an API key are required');
}
if (apply && !serviceRole) {
  throw new Error('SUPABASE_SERVICE_ROLE_KEY is required when using --apply');
}

const rows = await fetchTurns();
const rawPathsByUser = new Map();
for (const row of rows) {
  if (!row.project_path) continue;
  let paths = rawPathsByUser.get(row.user_id);
  if (!paths) {
    paths = new Set();
    rawPathsByUser.set(row.user_id, paths);
  }
  paths.add(cleanPath(row.project_path));
}

const changes = [];
for (const row of rows) {
  if (row.project_root) continue;
  const next = inferHistoricalRoot(row.project_path, rawPathsByUser.get(row.user_id) ?? new Set());
  if (!next || row.project_root === next) continue;
  changes.push({ id: row.id, user_id: row.user_id, project_path: row.project_path, from: row.project_root, to: next });
}

const byUserRoot = new Map();
for (const c of changes) {
  const key = `${c.user_id}\t${c.to}`;
  const prev = byUserRoot.get(key) ?? 0;
  byUserRoot.set(key, prev + 1);
}

console.log(JSON.stringify({
  mode: apply ? 'apply' : 'dry-run',
  scanned: rows.length,
  changes: changes.length,
  grouped_changes: [...byUserRoot.entries()].map(([key, count]) => {
    const [user_id, project_root] = key.split('\t');
    return { user_id, project_root, count };
  }),
}, null, 2));

if (apply) {
  for (const c of changes) {
    await sb('PATCH', `turns?id=eq.${c.id}`, { project_root: c.to });
  }
  console.log(`updated ${changes.length} turns`);
} else {
  console.log('dry-run only; re-run with --apply to write project_root');
}

function inferHistoricalRoot(projectPath, observedPaths) {
  if (!projectPath) return null;
  const cleaned = cleanPath(projectPath);
  const withoutClaudeWorktree = stripClaudeWorktree(cleaned);
  if (withoutClaudeWorktree !== cleaned) return withoutClaudeWorktree;

  const candidates = [...observedPaths]
    .filter((p) => cleaned.startsWith(`${p}/`))
    .sort((a, b) => b.length - a.length);
  for (const candidate of candidates) {
    const rel = cleaned.slice(candidate.length + 1);
    const first = rel.split('/')[0];
    if (COMMON_PROJECT_SUBDIRS.has(first)) return candidate;
  }
  return cleaned;
}

function stripClaudeWorktree(p) {
  const marker = '/.claude/worktrees/';
  const i = p.indexOf(marker);
  return i >= 0 ? p.slice(0, i) : p;
}

function cleanPath(p) {
  return p.replace(/\/+$/, '') || p;
}

async function sb(method, resource, body) {
  const res = await fetch(`${supabaseURL}/rest/v1/${resource}`, {
    method,
    headers: {
      apikey: apiKey,
      authorization: `Bearer ${apiKey}`,
      'content-type': 'application/json',
      prefer: 'return=minimal',
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`${method} ${resource} failed: ${res.status} ${text.slice(0, 500)}`);
  }
  return text ? JSON.parse(text) : null;
}

async function fetchTurns() {
  try {
    return await sb('GET', 'turns?select=id,user_id,project_path,project_root&order=id.asc');
  } catch (e) {
    const message = e instanceof Error ? e.message : String(e);
    if (!message.includes('column turns.project_root does not exist')) throw e;
    const oldRows = await sb('GET', 'turns?select=id,user_id,project_path&order=id.asc');
    return oldRows.map((row) => ({ ...row, project_root: null }));
  }
}

function parseEnv(file) {
  if (!fs.existsSync(file)) return {};
  const out = {};
  for (const line of fs.readFileSync(file, 'utf8').split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eq = trimmed.indexOf('=');
    if (eq < 0) continue;
    const key = trimmed.slice(0, eq).trim();
    let value = trimmed.slice(eq + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    out[key] = value;
  }
  return out;
}
