// ProjectNav — sticky sidebar with anchor links to each project,
// mirroring DateNav. Used in the "By project" view.

import type { ProjectGroupTree } from '@/lib/grouping';
import { anchorize } from './project-grouped-timeline';

export function ProjectNav({ projects }: { projects: ProjectGroupTree[] }) {
  if (projects.length === 0) return null;
  return (
    <nav
      aria-label="Jump to project"
      className="sticky top-4 hidden self-start text-xs md:block"
    >
      <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
        Jump to project
      </div>
      <ol className="flex flex-col gap-0.5">
        {projects.map((p) => (
          <li key={p.root}>
            <a
              href={`#p-${anchorize(p.root)}`}
              className="flex items-baseline justify-between gap-3 rounded px-2 py-1 text-zinc-600 transition hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
            >
              <span className="truncate font-mono">{p.displayName}</span>
              <span className="shrink-0 text-[10px] text-zinc-400 dark:text-zinc-500">
                {p.turnCount}
              </span>
            </a>
          </li>
        ))}
      </ol>
    </nav>
  );
}

export function MobileProjectScroll({ projects }: { projects: ProjectGroupTree[] }) {
  if (projects.length === 0) return null;
  return (
    <div className="mb-4 -mx-4 overflow-x-auto px-4 md:hidden">
      <div className="inline-flex gap-1">
        {projects.map((p) => (
          <a
            key={p.root}
            href={`#p-${anchorize(p.root)}`}
            className="rounded-full bg-zinc-100 px-2.5 py-0.5 font-mono text-[11px] font-medium text-zinc-600 hover:bg-zinc-200 dark:bg-zinc-800 dark:text-zinc-300 dark:hover:bg-zinc-700 whitespace-nowrap"
          >
            {p.displayName}{' '}
            <span className="font-sans text-zinc-400 dark:text-zinc-500">
              {p.turnCount}
            </span>
          </a>
        ))}
      </div>
    </div>
  );
}
