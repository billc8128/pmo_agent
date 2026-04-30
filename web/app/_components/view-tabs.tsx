// View toggle (Time / Project) + Date-range chips.
//
// Both are URL-driven Server Components. They preserve all other
// search params when constructing their hrefs.

import Link from 'next/link';
import { DATE_RANGES, type DateRange, type View } from '@/lib/grouping';

export function ViewToggle({
  basePath,
  searchParams,
  view,
}: {
  basePath: string;
  searchParams: Record<string, string | string[] | undefined>;
  view: View;
}) {
  return (
    <nav className="flex gap-1" aria-label="View">
      <ViewLink
        basePath={basePath}
        searchParams={searchParams}
        target="time"
        active={view === 'time'}
        label="By date"
      />
      <ViewLink
        basePath={basePath}
        searchParams={searchParams}
        target="project"
        active={view === 'project'}
        label="By project"
      />
    </nav>
  );
}

function ViewLink({
  basePath,
  searchParams,
  target,
  active,
  label,
}: {
  basePath: string;
  searchParams: Record<string, string | string[] | undefined>;
  target: View;
  active: boolean;
  label: string;
}) {
  const href = buildHref(basePath, searchParams, { view: target });
  const cls = active
    ? 'rounded-md bg-zinc-900 px-3 py-1 text-xs font-medium text-white dark:bg-zinc-100 dark:text-zinc-900'
    : 'rounded-md px-3 py-1 text-xs font-medium text-zinc-600 hover:bg-zinc-100 dark:text-zinc-400 dark:hover:bg-zinc-800';
  return (
    <Link href={href} className={cls} prefetch={false}>
      {label}
    </Link>
  );
}

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
  // Strip default-valued keys for clean URLs.
  if (merged.range === 'all') delete merged.range;
  if (merged.view === 'time') delete merged.view;

  const qs = new URLSearchParams(merged).toString();
  return qs ? `${basePath}?${qs}` : basePath;
}
