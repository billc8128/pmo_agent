// Date-range chips for the timeline. The previous "View" tabs
// (Timeline / By project) are gone — the new layout interleaves
// dates and projects in a single hierarchy.

import Link from 'next/link';
import { DATE_RANGES, type DateRange } from '@/lib/grouping';

export function DateRangeChips({
  basePath,
  searchParams,
  range,
}: {
  basePath: string;
  searchParams: Record<string, string | string[] | undefined>;
  range: DateRange;
}) {
  return (
    <nav className="flex gap-1" aria-label="Date range">
      {DATE_RANGES.map((r) => {
        const href = buildHref(basePath, searchParams, { range: r.value });
        const active = range === r.value;
        const cls = active
          ? 'rounded-full bg-zinc-900 px-2.5 py-0.5 text-[11px] font-medium text-white dark:bg-zinc-100 dark:text-zinc-900'
          : 'rounded-full px-2.5 py-0.5 text-[11px] font-medium text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800';
        return (
          <Link key={r.value} href={href} className={cls} prefetch={false}>
            {r.label}
          </Link>
        );
      })}
    </nav>
  );
}

// buildHref preserves all existing search params except those being
// overwritten. Default-valued keys are stripped for clean URLs.
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
  if (merged.range === 'all') delete merged.range;
  // Old "view" param no longer means anything; strip it so legacy
  // URLs don't litter the address bar.
  delete merged.view;

  const qs = new URLSearchParams(merged).toString();
  return qs ? `${basePath}?${qs}` : basePath;
}
