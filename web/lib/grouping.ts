// Shared utilities for the timeline / by-project / by-date views.
//
// We intentionally compute "project root" in TypeScript instead of in
// the database. Two reasons:
//   1. We don't want a schema change for a view-layer concern. The
//      daemon would have to resolve git roots and we'd backfill.
//   2. The heuristic (first 4 path components) may need to evolve;
//      keeping it in code lets us tune without touching data.

import type { Turn } from './types';

// projectRootFromPath converts an absolute path into a "project root".
// Default heuristic: take the first 4 path components after the
// leading slash.
//
//   /Users/a/Desktop/pmo_agent/daemon   →  /Users/a/Desktop/pmo_agent
//   /home/alice/code/foo                →  /home/alice/code/foo
//   /Users/a/Desktop/pmo_agent          →  /Users/a/Desktop/pmo_agent
//
// Paths with fewer than 4 components return verbatim. Empty / null
// returns "(unknown)".
export function projectRootFromPath(p: string | null | undefined): string {
  if (!p) return '(unknown)';
  const trimmed = p.startsWith('/') ? p.slice(1) : p;
  const parts = trimmed.split('/');
  if (parts.length <= 4) return p;
  return '/' + parts.slice(0, 4).join('/');
}

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

export function parseDateRange(raw: unknown): DateRange {
  if (raw === 'today' || raw === '7d' || raw === '30d' || raw === 'all') {
    return raw;
  }
  return 'all';
}

// ──────────────── Date-grouped, project-grouped tree ────────────────
//
// The core data structure that drives the new timeline. Two-level:
//
//   Day "2026-04-30" {
//     ProjectBlock "/Users/a/Desktop/pmo_agent" {
//       turns: [...]
//     },
//     ProjectBlock "..." {...}
//   }
//
// Day key is the ISO date in the viewer's local timezone (UI only;
// not stored). The day list is sorted newest-first; project blocks
// within a day are sorted by their newest turn within that day, also
// newest-first.

export type ProjectBlock = {
  root: string;          // absolute path
  displayName: string;   // basename
  turns: Turn[];         // newest-first
};

export type DayGroup = {
  dayKey: string;        // YYYY-MM-DD (local)
  dayLabel: string;      // e.g. "Today", "Yesterday", "Apr 30"
  turnCount: number;
  projects: ProjectBlock[];
};

export function groupByDayAndProject(turns: Turn[]): DayGroup[] {
  const dayMap = new Map<string, Map<string, ProjectBlock>>();

  for (const t of turns) {
    const d = new Date(t.user_message_at);
    const dayKey = localDayKey(d);
    let projMap = dayMap.get(dayKey);
    if (!projMap) {
      projMap = new Map();
      dayMap.set(dayKey, projMap);
    }
    const root = projectRootFromPath(t.project_path);
    let block = projMap.get(root);
    if (!block) {
      block = { root, displayName: projectDisplayName(root), turns: [] };
      projMap.set(root, block);
    }
    block.turns.push(t);
  }

  // Convert to arrays + sort. Turns inside each block stay
  // newest-first because the input was already sorted that way.
  const days: DayGroup[] = [];
  for (const [dayKey, projMap] of dayMap) {
    const projects = [...projMap.values()].sort((a, b) =>
      a.turns[0].user_message_at < b.turns[0].user_message_at ? 1 : -1,
    );
    const turnCount = projects.reduce((n, p) => n + p.turns.length, 0);
    days.push({
      dayKey,
      dayLabel: humanDayLabel(dayKey),
      turnCount,
      projects,
    });
  }
  days.sort((a, b) => (a.dayKey < b.dayKey ? 1 : -1));
  return days;
}

// localDayKey returns YYYY-MM-DD in the browser/server local timezone.
// In Vercel serverless we run UTC; the date headers therefore reflect
// UTC. That's fine for a public timeline — far simpler than juggling
// per-viewer timezones, and the deltas are usually obvious from the
// turn timestamps inside.
function localDayKey(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function humanDayLabel(dayKey: string): string {
  const today = localDayKey(new Date());
  const yesterday = localDayKey(new Date(Date.now() - 86_400_000));
  if (dayKey === today) return 'Today';
  if (dayKey === yesterday) return 'Yesterday';
  // Display style: "Apr 30" if same year, otherwise "Apr 30, 2025".
  const [y, m, d] = dayKey.split('-').map(Number);
  const date = new Date(Date.UTC(y, m - 1, d));
  const sameYear = y === new Date().getFullYear();
  return date.toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    ...(sameYear ? {} : { year: 'numeric' }),
  });
}

// ──────────────── Legacy: flat per-project group ────────────────
//
// Kept for back-compat with older callers, but the new timeline uses
// groupByDayAndProject above.

export type ProjectGroup = {
  root: string;
  displayName: string;
  count: number;
  latestAt: string;
  recentTurns: Turn[];
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
    if (t.user_message_at > g.latestAt) g.latestAt = t.user_message_at;
    if (g.recentTurns.length < recentPerGroup) g.recentTurns.push(t);
  }
  return [...map.values()].sort((a, b) => (a.latestAt < b.latestAt ? 1 : -1));
}
