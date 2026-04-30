// View tabs (Timeline / By project) and date-range chips, used by both
// /u/[handle] and /discover. Server Component because the choice is
// driven entirely by URL query params; no client state needed.

import Link from 'next/link';
import { DATE_RANGES, type DateRange } from '@/lib/grouping';

export type View = 'timeline' | 'projects';

export function parseView(raw: unknown): View {
  return raw === 'projects' ? 'projects' : 'timeline';
}

export function ViewTabs({
  basePath,
  searchParams,
  view,
  range,
}: {
  basePath: string; // e.g. "/u/bcc" or "/discover"
  searchParams: Record<string, string | string[] | undefined>;
  view: View;
  range: DateRange;
}) {
  return (
    <div className="mb-6 flex flex-wrap items-center justify-between gap-3 border-b border-zinc-200 pb-3 dark:border-zinc-800">
      <nav className="flex gap-1" aria-label="View">
        <ViewLink
          basePath={basePath}
          searchParams={searchParams}
          targetView="timeline"
          active={view === 'timeline'}
          label="Timeline"
        />
        <ViewLink
          basePath={basePath}
          searchParams={searchParams}
          targetView="projects"
          active={view === 'projects'}
          label="By project"
        />
      </nav>
      <nav className="flex gap-1" aria-label="Date range">
        {DATE_RANGES.map((r) => (
          <DateLink
            key={r.value}
            basePath={basePath}
            searchParams={searchParams}
            targetRange={r.value}
            active={range === r.value}
            label={r.label}
          />
        ))}
      </nav>
    </div>
  );
}

function ViewLink({
  basePath,
  searchParams,
  targetView,
  active,
  label,
}: {
  basePath: string;
  searchParams: Record<string, string | string[] | undefined>;
  targetView: View;
  active: boolean;
  label: string;
}) {
  const href = buildHref(basePath, searchParams, { view: targetView });
  const cls = active
    ? 'rounded-md bg-zinc-900 px-3 py-1 text-xs font-medium text-white dark:bg-zinc-100 dark:text-zinc-900'
    : 'rounded-md px-3 py-1 text-xs font-medium text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800';
  return (
    <Link href={href} className={cls} prefetch={false}>
      {label}
    </Link>
  );
}

function DateLink({
  basePath,
  searchParams,
  targetRange,
  active,
  label,
}: {
  basePath: string;
  searchParams: Record<string, string | string[] | undefined>;
  targetRange: DateRange;
  active: boolean;
  label: string;
}) {
  const href = buildHref(basePath, searchParams, { range: targetRange });
  const cls = active
    ? 'rounded-full bg-zinc-900 px-2.5 py-0.5 text-[11px] font-medium text-white dark:bg-zinc-100 dark:text-zinc-900'
    : 'rounded-full px-2.5 py-0.5 text-[11px] font-medium text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800';
  return (
    <Link href={href} className={cls} prefetch={false}>
      {label}
    </Link>
  );
}

// buildHref preserves all existing search params except those being
// overwritten. `range=all` and `view=timeline` are treated as defaults
// and get stripped from the URL for cleanliness.
function buildHref(
  basePath: string,
  current: Record<string, string | string[] | undefined>,
  overrides: Record<string, string | undefined>,
): string {
  const merged: Record<string, string> = {};
  for (const [k, v] of Object.entries(current)) {
    if (typeof v === 'string') merged[k] = v;
  }
  for (const [k, v] of Object.entries(overrides)) {
    if (v == null) continue;
    merged[k] = v;
  }
  // Strip defaults
  if (merged.view === 'timeline') delete merged.view;
  if (merged.range === 'all') delete merged.range;

  const qs = new URLSearchParams(merged).toString();
  return qs ? `${basePath}?${qs}` : basePath;
}
