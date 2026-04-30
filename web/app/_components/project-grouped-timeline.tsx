// ProjectGroupedTimeline — the "By project" layout.
//
// Top-level: one section per project. Inside each section: the cached
// LLM summary, then per-day sub-blocks of turns. Anchors target
// #p-<encoded-root> for the project sidebar.

import type { ProjectGroupTree } from '@/lib/grouping';
import type { Profile } from '@/lib/types';
import { TurnCard } from '../u/[handle]/turn-card';

export type ProjectSummaryMap = Map<string, string | null>;

export function ProjectGroupedTimeline({
  projects,
  profileById,
  summaries,
}: {
  projects: ProjectGroupTree[];
  profileById: Map<string, Profile> | null;
  summaries: ProjectSummaryMap;
}) {
  return (
    <div className="space-y-12">
      {projects.map((p) => {
        const author = profileById?.get(p.ownerUserId) ?? null;
        const summaryKey = `${p.ownerUserId}:${p.root}`;
        const summary = summaries.get(summaryKey) ?? null;
        const dirPart = parentDir(p.root);
        return (
          <section
            key={p.root}
            id={`p-${anchorize(p.root)}`}
            className="scroll-mt-4"
          >
            <header className="mb-3 border-b border-zinc-200 pb-2 dark:border-zinc-800">
              <div className="flex flex-wrap items-baseline justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-baseline gap-2">
                    {author && (
                      <a
                        href={`/u/${author.handle}`}
                        className="text-xs font-medium text-indigo-600 hover:underline dark:text-indigo-400"
                      >
                        @{author.handle}
                      </a>
                    )}
                    <h2 className="font-mono text-base font-semibold text-zinc-900 dark:text-zinc-100">
                      {p.displayName}
                    </h2>
                  </div>
                  {dirPart && (
                    <p
                      className="mt-0.5 truncate font-mono text-[11px] text-zinc-400 dark:text-zinc-500"
                      title={p.root}
                    >
                      {dirPart}
                    </p>
                  )}
                </div>
                <div className="text-xs text-zinc-500 dark:text-zinc-400">
                  {p.turnCount} turn{p.turnCount === 1 ? '' : 's'}
                  {' · '}
                  {p.days.length} day{p.days.length === 1 ? '' : 's'}
                </div>
              </div>
            </header>

            {summary ? (
              <p className="mb-5 rounded bg-zinc-50 px-3 py-2 text-sm leading-relaxed text-zinc-700 dark:bg-zinc-950 dark:text-zinc-300">
                {summary}
              </p>
            ) : (
              <p className="mb-5 rounded bg-zinc-50 px-3 py-2 text-xs italic text-zinc-500 dark:bg-zinc-950 dark:text-zinc-400">
                Project summary updating…
              </p>
            )}

            <div className="space-y-6">
              {p.days.map((d) => (
                <div key={d.dayKey}>
                  <div className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
                    {d.dayLabel}
                    <span className="ml-2 font-normal normal-case text-zinc-400 dark:text-zinc-500">
                      {d.turns.length} turn{d.turns.length === 1 ? '' : 's'}
                    </span>
                  </div>
                  <ol className="space-y-3">
                    {d.turns.map((t) => (
                      <li key={t.id}>
                        <TurnCard turn={t} />
                      </li>
                    ))}
                  </ol>
                </div>
              ))}
            </div>
          </section>
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

// anchorize converts a path into a DOM-safe anchor id.
export function anchorize(s: string): string {
  return s.replace(/[^A-Za-z0-9]+/g, '-').replace(/^-|-$/g, '');
}
