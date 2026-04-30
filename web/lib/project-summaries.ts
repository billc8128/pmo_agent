// Server-side helper: for a set of (user_id, project_root) pairs that
// the page is about to render, fetch any cached summaries from the
// database and fire-and-forget a refresh request for any that are
// missing or stale.
//
// Why fire-and-forget?
//   The summary takes ~10s to generate (OpenRouter call). Blocking the
//   SSR response for that long is unacceptable. Instead:
//     1. We render the page with whatever is currently cached (or
//        "Project summary updating…" placeholder if nothing).
//     2. We kick off the regenerate for stale rows in the background.
//     3. The next reload picks up the fresh value.
//
// Cache key: (user_id, project_root). Staleness: turn_count !=
// matchingLiveCount (counted by us at SSR time). The Edge Function
// re-counts live and double-checks, so concurrent triggers are safe.

import { serverClient } from './supabase';
import { projectRootFromPath } from './grouping';
import type { Turn } from './types';

const SUMMARIZE_PROJECT_URL =
  (process.env.NEXT_PUBLIC_SUPABASE_URL ?? '') +
  '/functions/v1/summarize_project';
const ANON_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? '';

export type SummaryMap = Map<string, string | null>; // key = `${user_id}:${project_root}`

export async function loadProjectSummaries(turns: Turn[]): Promise<SummaryMap> {
  const out: SummaryMap = new Map();
  if (turns.length === 0) return out;

  // Compute live counts per (user, root) from the in-memory turn list.
  // Note: this only reflects the visible window (we may have filtered
  // by date), which is fine — staleness check on the server function
  // recomputes against the full database.
  const liveCount = new Map<string, number>();
  for (const t of turns) {
    const root = projectRootFromPath(t.project_path);
    const key = `${t.user_id}:${root}`;
    liveCount.set(key, (liveCount.get(key) ?? 0) + 1);
  }

  if (liveCount.size === 0) return out;

  // Bulk-fetch existing rows for the visible (user, root) pairs.
  // We use an OR query: (user_id = X and project_root = Y) OR ...
  // For typical pages with <20 distinct projects, this is fine.
  const sb = serverClient();
  const filterPairs = [...liveCount.keys()].map((k) => {
    const [uid, root] = k.split(':', 2);
    // Trim the user_id off; the rest is the project_root (which may
    // itself contain colons in pathological cases — but a path
    // starting with "/" never will).
    const colonIdx = k.indexOf(':');
    const realRoot = k.slice(colonIdx + 1);
    return { uid, root: realRoot };
  });

  // Single round-trip: fetch all rows whose (user_id, project_root)
  // is in the visible set. Supabase doesn't support tuple IN out of
  // the box, so we OR a list of conjunctions.
  const orClause = filterPairs
    .map(
      (p) =>
        `and(user_id.eq.${p.uid},project_root.eq.${escapeForFilter(p.root)})`,
    )
    .join(',');
  const { data, error } = await sb
    .from('project_summaries')
    .select('user_id, project_root, summary, turn_count')
    .or(orClause);

  if (error) {
    // Non-fatal: render without summaries.
    console.warn('project_summaries fetch failed:', error.message);
    return out;
  }

  const stale: { user_id: string; project_root: string }[] = [];

  for (const pair of filterPairs) {
    const key = `${pair.uid}:${pair.root}`;
    const row = data?.find(
      (r: { user_id: string; project_root: string }) =>
        r.user_id === pair.uid && r.project_root === pair.root,
    );
    if (row && row.summary) {
      out.set(key, row.summary);
    } else {
      out.set(key, null);
    }
    // Determine staleness: if no row at all, definitely stale.
    // If row exists, the Edge Function will compare turn_counts
    // itself; we send the trigger and let it decide.
    if (!row || row.turn_count !== liveCount.get(key)) {
      stale.push({ user_id: pair.uid, project_root: pair.root });
    }
  }

  // Fire-and-forget refresh for stale entries. We deliberately do
  // NOT await these — we want SSR to complete with whatever's already
  // cached. Each call costs ~10s of LLM time; serializing them on the
  // page render path would be terrible.
  for (const s of stale) {
    void fetch(SUMMARIZE_PROJECT_URL, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${ANON_KEY}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(s),
    }).catch((e) => {
      console.warn(
        'summarize_project trigger failed for',
        s.project_root,
        e instanceof Error ? e.message : String(e),
      );
    });
  }

  return out;
}

// escapeForFilter: PostgREST .or() expects values as bare strings;
// commas, parens, and spaces in the value need to be quoted with
// double-quotes per the docs. Project roots are paths, which can
// contain none of those typically (just slashes), but we escape
// defensively.
function escapeForFilter(v: string): string {
  if (/[(),\s"]/.test(v)) {
    return `"${v.replace(/"/g, '""')}"`;
  }
  return v;
}
