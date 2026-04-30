// Project card used by /u/[handle]?view=projects and
// /discover?view=projects. Compact: header (name + path + count),
// then up to 3 recent turn previews from the group.

import Link from 'next/link';
import type { ProjectGroup } from '@/lib/grouping';

export function ProjectCard({
  group,
  drillHref,
}: {
  group: ProjectGroup;
  drillHref: string;
}) {
  const latest = new Date(group.latestAt);
  const ago = humanAgo(latest);
  const dirPart = parentDir(group.root);

  return (
    <Link
      href={drillHref}
      prefetch={false}
      className="block rounded-lg border border-zinc-200 bg-white p-4 shadow-sm transition hover:border-indigo-400 hover:shadow-md dark:border-zinc-800 dark:bg-zinc-900 dark:hover:border-indigo-600"
    >
      <header className="mb-3 flex items-baseline justify-between gap-3">
        <div className="min-w-0">
          <h3 className="truncate font-mono text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            {group.displayName}
          </h3>
          {dirPart && (
            <p
              className="mt-0.5 truncate font-mono text-[11px] text-zinc-400 dark:text-zinc-500"
              title={group.root}
            >
              {dirPart}
            </p>
          )}
        </div>
        <div className="shrink-0 text-right">
          <div className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            {group.count}
          </div>
          <div className="text-[11px] text-zinc-500 dark:text-zinc-400">
            {group.count === 1 ? 'turn' : 'turns'}
          </div>
        </div>
      </header>

      <div className="mb-2 text-[11px] text-zinc-500 dark:text-zinc-400">
        last active {ago}
      </div>

      {group.recentTurns.length > 0 && (
        <ul className="space-y-1.5">
          {group.recentTurns.map((t) => (
            <li
              key={t.id}
              className="truncate text-xs text-zinc-700 dark:text-zinc-300"
            >
              <span className="text-zinc-400">›</span>{' '}
              {oneLine(t.user_message, 90)}
            </li>
          ))}
        </ul>
      )}
    </Link>
  );
}

// Small helpers, kept here so the card has no dependencies.

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

function oneLine(s: string, n: number): string {
  let collapsed = s.replace(/\s+/g, ' ').trim();
  if (collapsed.length <= n) return collapsed;
  return collapsed.slice(0, n) + '…';
}
