// ProjectChips — horizontal strip showing "All projects" plus one
// chip per project root present in the current view. Selecting a
// chip sets ?project=<root> in the URL; the host page's existing
// project-filter logic kicks in.
//
// Hidden entirely when fewer than 2 projects are available — there's
// nothing to choose between.

import Link from 'next/link';
import { projectDisplayName } from '@/lib/grouping';

export function ProjectChips({
  basePath,
  searchParams,
  roots,
  active,
}: {
  basePath: string;
  searchParams: Record<string, string | string[] | undefined>;
  roots: string[];                   // distinct project roots in scope
  active: string;                    // current ?project=, or "" for All
}) {
  if (roots.length < 2) return null;

  return (
    <nav
      className="mb-3 flex flex-wrap gap-1 overflow-x-auto"
      aria-label="Filter by project"
    >
      <ChipLink
        href={hrefWith(basePath, searchParams, undefined)}
        active={active === ''}
        label="All projects"
      />
      {roots.map((r) => (
        <ChipLink
          key={r}
          href={hrefWith(basePath, searchParams, r)}
          active={active === r}
          label={projectDisplayName(r)}
        />
      ))}
    </nav>
  );
}

function ChipLink({
  href,
  active,
  label,
}: {
  href: string;
  active: boolean;
  label: string;
}) {
  const base = 'rounded-full px-3 py-1 text-xs font-medium transition whitespace-nowrap';
  const cls = active
    ? `${base} bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900`
    : `${base} bg-zinc-100 text-zinc-700 hover:bg-zinc-200 dark:bg-zinc-800 dark:text-zinc-300 dark:hover:bg-zinc-700`;
  return (
    <Link href={href} className={cls} prefetch={false}>
      {label}
    </Link>
  );
}

function hrefWith(
  basePath: string,
  current: Record<string, string | string[] | undefined>,
  project: string | undefined,
): string {
  const merged: Record<string, string> = {};
  for (const [k, v] of Object.entries(current)) {
    if (typeof v === 'string') merged[k] = v;
  }
  if (project == null) {
    delete merged.project;
  } else {
    merged.project = project;
  }
  // Strip default-valued keys for clean URLs.
  if (merged.range === 'all') delete merged.range;
  if (merged.view === 'time') delete merged.view;

  const qs = new URLSearchParams(merged).toString();
  return qs ? `${basePath}?${qs}` : basePath;
}
