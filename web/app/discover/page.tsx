// /discover — global feed across all users.
//
// Three filter axes:
//   ?handle=foo  — limit to one user's turns; default "all" interleaves
//   ?view=...    — timeline (default) or projects
//   ?range=...   — today | 7d | 30d | all (default)
//
// Anonymous-readable; RLS allows public select.

import Link from 'next/link';
import { serverClient } from '@/lib/supabase';
import type { Turn, Profile } from '@/lib/types';
import {
  dateRangeStartISO,
  groupTurnsByProject,
  parseDateRange,
} from '@/lib/grouping';
import { DiscoverTurn } from './discover-turn';
import { parseView, ViewTabs } from '../_components/view-tabs';
import { ProjectCard } from '../_components/project-card';

export const dynamic = 'force-dynamic';

const PAGE_SIZE = 200;

export default async function DiscoverPage(props: PageProps<'/discover'>) {
  const sp = await props.searchParams;
  const tab = typeof sp.handle === 'string' ? sp.handle : 'all';
  const view = parseView(sp.view);
  const range = parseDateRange(sp.range);
  const projectFilter = typeof sp.project === 'string' ? sp.project : '';

  const sb = serverClient();

  const { data: profilesRaw } = await sb
    .from('profiles')
    .select('id, handle, display_name, created_at')
    .order('created_at', { ascending: true });
  const profiles: Profile[] = profilesRaw ?? [];
  const profileById = new Map(profiles.map((p) => [p.id, p]));

  let query = sb
    .from('turns')
    .select('*')
    .order('user_message_at', { ascending: false })
    .limit(PAGE_SIZE);

  const since = dateRangeStartISO(range);
  if (since) query = query.gte('user_message_at', since);

  if (tab !== 'all') {
    const target = profiles.find((p) => p.handle === tab);
    if (target) {
      query = query.eq('user_id', target.id);
    } else {
      // Unknown handle: show empty rather than 404.
      query = query.eq('user_id', '00000000-0000-0000-0000-000000000000');
    }
  }

  const { data: turnsData } = await query;
  let turns: Turn[] = turnsData ?? [];

  if (view === 'timeline' && projectFilter) {
    // Apply project drill-in client-side via the same heuristic the
    // grouping uses, so user behavior is consistent.
    const { projectRootFromPath } = await import('@/lib/grouping');
    turns = turns.filter(
      (t) => projectRootFromPath(t.project_path) === projectFilter,
    );
  }

  return (
    <main className="mx-auto max-w-3xl px-4 py-8 sm:py-12">
      <header className="mb-6 border-b border-zinc-200 pb-4 dark:border-zinc-800">
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
          Discover
        </h1>
        <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          Public turns from everyone using pmo_agent.
        </p>
      </header>

      {/* Top user-filter strip stays a separate row from the view+range
          tabs so the two axes don't get visually conflated. */}
      <nav className="mb-3 flex flex-wrap gap-1 overflow-x-auto" aria-label="Filter by user">
        <UserTabLink href={hrefWithHandle(sp, undefined)} active={tab === 'all'} label="All" />
        {profiles.map((p) => (
          <UserTabLink
            key={p.id}
            href={hrefWithHandle(sp, p.handle)}
            active={tab === p.handle}
            label={`@${p.handle}`}
          />
        ))}
      </nav>

      <ViewTabs basePath="/discover" searchParams={sp} view={view} range={range} />

      {view === 'timeline' && projectFilter && (
        <ProjectFilterBadge searchParams={sp} project={projectFilter} />
      )}

      {turns.length === 0 ? (
        <p className="text-zinc-500 dark:text-zinc-400">No turns in this view.</p>
      ) : view === 'projects' ? (
        <ProjectsGridForDiscover turns={turns} profileById={profileById} searchParams={sp} />
      ) : (
        <ol className="space-y-6">
          {turns.map((t) => {
            const author = profileById.get(t.user_id);
            return (
              <li key={t.id}>
                <DiscoverTurn turn={t} author={author ?? null} />
              </li>
            );
          })}
        </ol>
      )}
    </main>
  );
}

function UserTabLink({
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

function hrefWithHandle(
  sp: Record<string, string | string[] | undefined>,
  handle: string | undefined,
): string {
  const merged: Record<string, string> = {};
  for (const [k, v] of Object.entries(sp)) {
    if (typeof v === 'string') merged[k] = v;
  }
  if (handle == null) {
    delete merged.handle;
  } else {
    merged.handle = handle;
  }
  // Drop default-valued keys for a clean URL.
  if (merged.view === 'timeline') delete merged.view;
  if (merged.range === 'all') delete merged.range;

  const qs = new URLSearchParams(merged).toString();
  return qs ? `/discover?${qs}` : '/discover';
}

function ProjectFilterBadge({
  searchParams,
  project,
}: {
  searchParams: Record<string, string | string[] | undefined>;
  project: string;
}) {
  const sp: Record<string, string> = {};
  for (const [k, v] of Object.entries(searchParams)) {
    if (typeof v === 'string') sp[k] = v;
  }
  delete sp.project;
  if (sp.view === 'timeline') delete sp.view;
  if (sp.range === 'all') delete sp.range;
  const qs = new URLSearchParams(sp).toString();
  const clearHref = qs ? `/discover?${qs}` : '/discover';

  const slash = project.lastIndexOf('/');
  const name = slash >= 0 && slash < project.length - 1 ? project.slice(slash + 1) : project;

  return (
    <div className="mb-4 flex items-center gap-2 text-xs">
      <span className="text-zinc-500 dark:text-zinc-400">filtered to:</span>
      <span className="rounded-full bg-indigo-50 px-2.5 py-0.5 font-mono text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300">
        {name}
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

// ProjectsGridForDiscover — when viewing All users in projects mode, a
// project card may belong to multiple users. We still group by project
// root regardless of author; the card's recent-turn previews show
// who's working on what. Drill-in goes to /discover with project=...
// preserved; this filters the timeline to that project root across all
// users in the current handle scope.
function ProjectsGridForDiscover({
  turns,
  profileById: _profileById,
  searchParams,
}: {
  turns: Turn[];
  profileById: Map<string, Profile>;
  searchParams: Record<string, string | string[] | undefined>;
}) {
  const groups = groupTurnsByProject(turns, 3);
  const sp: Record<string, string> = {};
  for (const [k, v] of Object.entries(searchParams)) {
    if (typeof v === 'string') sp[k] = v;
  }
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      {groups.map((g) => {
        // Drill into discover's timeline view, scoped to this project root.
        // We keep the user-filter (handle) untouched.
        const params: Record<string, string> = { ...sp, project: g.root };
        delete params.view;
        const qs = new URLSearchParams(params).toString();
        const drillHref = qs ? `/discover?${qs}` : '/discover';
        return <ProjectCard key={g.root} group={g} drillHref={drillHref} />;
      })}
    </div>
  );
}
