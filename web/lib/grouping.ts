// Shared utilities for the timeline / by-project / by-date views.
//
// We intentionally compute "project root" in TypeScript instead of in
// the database. Two reasons:
//   1. We don't want to introduce a schema change for a view-layer
//      concern. The daemon would have to resolve git roots and we'd
//      have to backfill historical rows; way too much for what is
//      effectively a UI grouping.
//   2. The heuristic (first 4 path components — /Users/<u>/<dir>/<proj>)
//      may need to evolve. Keeping it in code lets us tune without
//      touching data.

import type { Turn } from './types';

// projectRootFromPath converts an absolute path into a "project root".
// Default heuristic: take the first 4 path components after the leading
// slash. So:
//
//   /Users/a/Desktop/pmo_agent/daemon   →  /Users/a/Desktop/pmo_agent
//   /home/alice/code/foo                →  /home/alice/code/foo
//   /Users/a/Desktop/pmo_agent          →  /Users/a/Desktop/pmo_agent
//
// Paths with fewer than 4 components return verbatim. Empty/null
// returns "(unknown)" so it groups separately rather than crashing.
export function projectRootFromPath(p: string | null | undefined): string {
  if (!p) return '(unknown)';
  const trimmed = p.startsWith('/') ? p.slice(1) : p;
  const parts = trimmed.split('/');
  if (parts.length <= 4) {
    return p; // already at or below root depth
  }
  return '/' + parts.slice(0, 4).join('/');
}

// projectDisplayName returns just the last component of a project root,
// for headings / chips. Falls back to the full path if it has no slash.
export function projectDisplayName(root: string): string {
  const slash = root.lastIndexOf('/');
  if (slash < 0 || slash === root.length - 1) return root;
  return root.slice(slash + 1);
}

// ──────────────── Date filtering ────────────────

export type DateRange = 'today' | '7d' | '30d' | 'all';

export const DATE_RANGES: { value: DateRange; label: string }[] = [
  { value: 'today', label: 'Today' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
  { value: 'all', label: 'All' },
];

// dateRangeStartISO returns the UTC ISO timestamp that bounds the
// requested range, or null for "all". The boundary is inclusive on
// the lower end ("at or after this moment").
export function dateRangeStartISO(range: DateRange): string | null {
  const now = Date.now();
  const day = 24 * 60 * 60 * 1000;
  switch (range) {
    case 'today': {
      const d = new Date();
      d.setHours(0, 0, 0, 0);
      return d.toISOString();
    }
    case '7d':
      return new Date(now - 7 * day).toISOString();
    case '30d':
      return new Date(now - 30 * day).toISOString();
    case 'all':
    default:
      return null;
  }
}

// parseDateRange normalizes a raw query-string value to a DateRange.
export function parseDateRange(raw: unknown): DateRange {
  if (raw === 'today' || raw === '7d' || raw === '30d' || raw === 'all') {
    return raw;
  }
  return 'all';
}

// ──────────────── Project aggregation ────────────────

export type ProjectGroup = {
  root: string;             // /Users/a/Desktop/pmo_agent
  displayName: string;       // pmo_agent
  count: number;             // total turns in this group, in current filter
  latestAt: string;          // ISO of newest user_message_at in group
  recentTurns: Turn[];       // up to N most-recent turns, newest first
};

export function groupTurnsByProject(
  turns: Turn[],
  recentPerGroup = 3,
): ProjectGroup[] {
  const map = new Map<string, ProjectGroup>();
  for (const t of turns) {
    const root = projectRootFromPath(t.project_path);
    let g = map.get(root);
    if (!g) {
      g = {
        root,
        displayName: projectDisplayName(root),
        count: 0,
        latestAt: t.user_message_at,
        recentTurns: [],
      };
      map.set(root, g);
    }
    g.count += 1;
    if (t.user_message_at > g.latestAt) {
      g.latestAt = t.user_message_at;
    }
    if (g.recentTurns.length < recentPerGroup) {
      g.recentTurns.push(t);
    }
  }
  // Sort: most-recent activity first.
  return [...map.values()].sort((a, b) => (a.latestAt < b.latestAt ? 1 : -1));
}
