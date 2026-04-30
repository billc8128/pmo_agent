// ProjectGrid — the new "By project" view: a clickable card per
// project, no turn details inline. Clicking a card drills into
// ?project=<root> on the same page, which (a) filters the timeline
// to that project and (b) implicitly switches view back to time
// (the project filter renders as the user's full timeline scoped
// to one project).

import Link from 'next/link';
import type { ProjectGroupTree } from '@/lib/grouping';
import type { Profile } from '@/lib/types';

export type ProjectSummaryMap = Map<string, string | null>;

export function ProjectGrid({
  projects,
  profileById,
  summaries,
  buildDrillHref,
}: {
  projects: ProjectGroupTree[];
  profileById: Map<string, Profile> | null;
  summaries: ProjectSummaryMap;
  buildDrillHref: (root: string) => string;
}) {
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      {projects.map((p) => {
        const author = profileById?.get(p.ownerUserId) ?? null;
        const summaryKey = `${p.ownerUserId}:${p.root}`;
        const summary = summaries.get(summaryKey) ?? null;
        const dirPart = parentDir(p.root);
        const ago = humanAgo(new Date(p.latestAt));
        return (
          <Link
            key={p.root}
            href={buildDrillHref(p.root)}
            prefetch={false}
            className="group flex flex-col rounded-lg border border-zinc-200 bg-white p-4 shadow-sm transition hover:border-indigo-400 hover:shadow-md dark:border-zinc-800 dark:bg-zinc-900 dark:hover:border-indigo-600"
          >
            <header className="mb-2 flex items-baseline justify-between gap-3">
              <div className="min-w-0">
                {author && (
                  <div className="text-xs font-medium text-indigo-600 dark:text-indigo-400">
                    @{author.handle}
                  </div>
                )}
                <h3 className="truncate font-mono text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                  {p.displayName}
                </h3>
                {dirPart && (
                  <p
                    className="mt-0.5 truncate font-mono text-[11px] text-zinc-400 dark:text-zinc-500"
                    title={p.root}
                  >
                    {dirPart}
                  </p>
                )}
              </div>
              <div className="shrink-0 text-right">
                <div className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                  {p.turnCount}
                </div>
                <div className="text-[11px] text-zinc-500 dark:text-zinc-400">
                  {p.turnCount === 1 ? 'turn' : 'turns'}
                </div>
              </div>
            </header>

            <p className="mb-3 text-[11px] text-zinc-500 dark:text-zinc-400">
              {p.days.length} day{p.days.length === 1 ? '' : 's'}
              {' · '}last active {ago}
            </p>

            {summary ? (
              <p className="text-sm leading-relaxed text-zinc-700 dark:text-zinc-300">
                {summary}
              </p>
            ) : (
              <p className="text-xs italic text-zinc-500 dark:text-zinc-400">
                Project summary updating…
              </p>
            )}

            <div className="mt-3 text-[11px] text-indigo-600 opacity-0 transition group-hover:opacity-100 dark:text-indigo-400">
              View turns →
            </div>
          </Link>
        );
      })}
    </div>
  );
}

function parentDir(root: string): string {
  const slash = root.lastIndexOf('/');
  if (slash <= 0) return '';
  return root.slice(0, slash);
}

function humanAgo(d: Date): string {
  const ms = Date.now() - d.getTime();
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return 'just now';
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day < 7) return `${day}d ago`;
  return d.toLocaleDateString();
}
