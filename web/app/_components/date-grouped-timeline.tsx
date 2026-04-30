// DateGroupedTimeline renders the new "date → project → turns"
// hierarchy. Server Component; the per-turn TurnCard is its own
// client island that handles the expand toggle.

import type { DayGroup } from '@/lib/grouping';
import type { Profile } from '@/lib/types';
import { TurnCard } from '../u/[handle]/turn-card';

export type ProjectSummaryMap = Map<string, string | null>;
//                                  key: `${user_id}:${project_root}`
//                                  value: cached summary text or null

export function DateGroupedTimeline({
  days,
  profileById,
  summaries,
}: {
  days: DayGroup[];
  profileById: Map<string, Profile> | null;
  summaries: ProjectSummaryMap;
}) {
  return (
    <div className="space-y-10">
      {days.map((d) => (
        <section key={d.dayKey} id={`d-${d.dayKey}`} className="scroll-mt-4">
          <header className="mb-4 flex items-baseline gap-3 border-b border-zinc-200 pb-2 dark:border-zinc-800">
            <h2 className="text-lg font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
              {d.dayLabel}
            </h2>
            <span className="text-xs text-zinc-500 dark:text-zinc-400">
              {d.turnCount} turn{d.turnCount === 1 ? '' : 's'}
            </span>
          </header>

          <div className="space-y-6">
            {d.projects.map((p) => {
              // Project block within this day. Each block belongs to
              // a single user (the turns came from one user_id), so
              // we can pick the author from the first turn.
              const author = profileById?.get(p.turns[0].user_id) ?? null;
              const userId = p.turns[0].user_id;
              const summary = summaries.get(`${userId}:${p.root}`) ?? null;
              const dirPart = parentDir(p.root);

              return (
                <article
                  key={p.root}
                  className="rounded-lg border border-zinc-200 bg-white p-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900"
                >
                  <header className="mb-3 flex flex-wrap items-baseline justify-between gap-3 border-b border-zinc-100 pb-2 dark:border-zinc-800">
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
                        <h3 className="font-mono text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                          {p.displayName}
                        </h3>
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
                    <div className="text-[11px] text-zinc-500 dark:text-zinc-400">
                      {p.turns.length} turn{p.turns.length === 1 ? '' : 's'} today
                    </div>
                  </header>

                  {summary ? (
                    <p className="mb-4 rounded bg-zinc-50 px-3 py-2 text-sm leading-relaxed text-zinc-700 dark:bg-zinc-950 dark:text-zinc-300">
                      {summary}
                    </p>
                  ) : (
                    <p className="mb-4 rounded bg-zinc-50 px-3 py-2 text-xs italic text-zinc-500 dark:bg-zinc-950 dark:text-zinc-400">
                      Project summary updating…
                    </p>
                  )}

                  <ol className="space-y-3">
                    {p.turns.map((t) => (
                      <li key={t.id}>
                        <TurnCard turn={t} />
                      </li>
                    ))}
                  </ol>
                </article>
              );
            })}
          </div>
        </section>
      ))}
    </div>
  );
}

function parentDir(root: string): string {
  const slash = root.lastIndexOf('/');
  if (slash <= 0) return '';
  return root.slice(0, slash);
}
