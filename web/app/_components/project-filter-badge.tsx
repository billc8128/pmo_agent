// ProjectFilterBadge — the small chip shown when the page is filtered
// to one project root, with a link to clear the filter.

import Link from 'next/link';
import { projectDisplayName } from '@/lib/grouping';

export function ProjectFilterBadge({
  basePath,
  searchParams,
  project,
}: {
  basePath: string;
  searchParams: Record<string, string | string[] | undefined>;
  project: string;
}) {
  const sp: Record<string, string> = {};
  for (const [k, v] of Object.entries(searchParams)) {
    if (typeof v === 'string') sp[k] = v;
  }
  delete sp.project;
  if (sp.range === 'all') delete sp.range;
  delete sp.view;
  const qs = new URLSearchParams(sp).toString();
  const clearHref = qs ? `${basePath}?${qs}` : basePath;

  return (
    <div className="mb-4 flex items-center gap-2 text-xs">
      <span className="text-zinc-500 dark:text-zinc-400">filtered to:</span>
      <span className="rounded-full bg-indigo-50 px-2.5 py-0.5 font-mono text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300">
        {projectDisplayName(project)}
      </span>
      <Link
        href={clearHref}
        prefetch={false}
        className="text-zinc-500 underline decoration-dotted hover:text-zinc-700 dark:text-zinc-400 dark:hover:text-zinc-200"
      >
        clear
      </Link>
    </div>
  );
}
