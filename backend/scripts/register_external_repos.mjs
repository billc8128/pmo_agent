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
const dryRun = !process.argv.includes('--apply');

const repos = parseRepos(env.EXTERNAL_REPOS_JSON) ?? [
  {
    provider: 'github',
    repo_full_name: 'billc8128/vibelive',
    project_root: '/Users/a/Desktop/vibelive',
  },
  {
    provider: 'github',
    repo_full_name: 'billc8128/oneship',
    project_root: '/Users/a/Desktop/oneship',
  },
];

if (!supabaseURL || !serviceRole) {
  throw new Error('SUPABASE_URL/NEXT_PUBLIC_SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required');
}

const rows = repos.map(normalizeRepo);
console.log(JSON.stringify({ mode: dryRun ? 'dry-run' : 'apply', repos: rows }, null, 2));

if (dryRun) {
  console.log('dry-run only; re-run with --apply to upsert external_repos');
} else {
  await sb('POST', 'external_repos?on_conflict=provider,repo_full_name', rows, {
    prefer: 'resolution=merge-duplicates,return=minimal',
  });
  console.log(`upserted ${rows.length} external repo mappings`);
}

function normalizeRepo(row) {
  const provider = String(row.provider || '').trim().toLowerCase();
  const repoFullName = String(row.repo_full_name || row.repo || '').trim();
  const projectRoot = String(row.project_root || '').trim().replace(/\/+$/, '');
  if (!['github', 'gitea'].includes(provider)) {
    throw new Error(`invalid provider: ${provider}`);
  }
  if (!repoFullName || !repoFullName.includes('/')) {
    throw new Error(`invalid repo_full_name: ${repoFullName}`);
  }
  if (!projectRoot.startsWith('/')) {
    throw new Error(`invalid project_root for ${repoFullName}: ${projectRoot}`);
  }
  return {
    provider,
    repo_full_name: repoFullName,
    project_root: projectRoot,
  };
}

function parseRepos(value) {
  if (!value) return null;
  const parsed = JSON.parse(value);
  if (!Array.isArray(parsed)) throw new Error('EXTERNAL_REPOS_JSON must be a JSON array');
  return parsed;
}

async function sb(method, resource, body, extraHeaders = {}) {
  const res = await fetch(`${supabaseURL}/rest/v1/${resource}`, {
    method,
    headers: {
      apikey: serviceRole,
      authorization: `Bearer ${serviceRole}`,
      'content-type': 'application/json',
      ...extraHeaders,
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  if (!res.ok) {
    throw new Error(`${method} ${resource} failed: ${res.status} ${text.slice(0, 500)}`);
  }
  return text ? JSON.parse(text) : null;
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
