// /discover — global feed across all users.
//
// Filter axes:
//   ?handle=foo      limit to one user (default "all" interleaves)
//   ?view=time | project
//   ?range=...       today | 7d | 30d | all
//   ?project=<root>  drill into one project root (still cross-user)

import Link from 'next/link';
import { serverClient } from '@/lib/supabase';
import type { Turn, Profile } from '@/lib/types';
import {
  dateRangeStartISO,
  groupByDayAndProject,
  groupByProjectAndDay,
  parseDateRange,
  parseView,
  projectRootFromPath,
} from '@/lib/grouping';
import { loadProjectSummaries } from '@/lib/project-summaries';
import { DateRangeChips, ViewToggle } from '../_components/view-tabs';
import { DateGroupedTimeline } from '../_components/date-grouped-timeline';
import { DateNav, MobileDateScroll } from '../_components/date-nav';
import { ProjectGrid } from '../_components/project-grid';
import { ProjectFilterBadge } from '../_components/project-filter-badge';
import { ProjectChips } from '../_components/project-chips';

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
    query = target
      ? query.eq('user_id', target.id)
      : query.eq('user_id', '00000000-0000-0000-0000-000000000000');
  }

  const { data: turnsData } = await query;
  const allTurns: Turn[] = turnsData ?? [];

  // Compute chip universe BEFORE the project filter (so user can
  // switch projects without first clearing the active filter).
  const allRoots = collectRoots(allTurns);

  let turns = allTurns;
  if (projectFilter) {
    turns = turns.filter(
      (t) => projectRootFromPath(t.project_path) === projectFilter,
    );
  }

  const summaries = await loadProjectSummaries(turns);
  const effectiveView = projectFilter ? 'time' : view;

  return (
    <main className="mx-auto max-w-6xl px-4 py-8 sm:py-12">
      <header className="mb-6 border-b border-zinc-200 pb-4 dark:border-zinc-800">
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
          Discover
        </h1>
        <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          Public turns from everyone using pmo_agent.
        </p>
      </header>

      <nav
        className="mb-3 flex flex-wrap gap-1 overflow-x-auto"
        aria-label="Filter by user"
      >
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

      <div className="mb-6 flex flex-wrap items-center justify-between gap-3 border-b border-zinc-200 pb-3 dark:border-zinc-800">
        {projectFilter ? (
          <span className="text-xs text-zinc-500 dark:text-zinc-400">Timeline</span>
        ) : (
          <ViewToggle basePath="/discover" searchParams={sp} view={effectiveView} />
        )}
        <DateRangeChips basePath="/discover" searchParams={sp} range={range} />
      </div>

      {effectiveView === 'time' && (
        <ProjectChips
          basePath="/discover"
          searchParams={sp}
          roots={allRoots}
          active={projectFilter}
        />
      )}

      {projectFilter && (
        <ProjectFilterBadge basePath="/discover" searchParams={sp} project={projectFilter} />
      )}

      {turns.length === 0 ? (
        <p className="text-zinc-500 dark:text-zinc-400">No turns in this view.</p>
      ) : effectiveView === 'project' ? (
        <ProjectView turns={turns} profileById={profileById} summaries={summaries} sp={sp} />
      ) : (
        <TimeView turns={turns} profileById={profileById} summaries={summaries} />
      )}
    </main>
  );
}

function collectRoots(turns: Turn[]): string[] {
  const set = new Set<string>();
  for (const t of turns) set.add(projectRootFromPath(t.project_path));
  return [...set].sort();
}

function TimeView({
  turns,
  profileById,
  summaries,
}: {
  turns: Turn[];
  profileById: Map<string, Profile>;
  summaries: Map<string, string | null>;
}) {
  const days = groupByDayAndProject(turns);
  return (
    <div className="grid grid-cols-1 gap-8 md:grid-cols-[10rem_minmax(0,1fr)]">
      <DateNav days={days} />
      <div>
        <MobileDateScroll days={days} />
        <DateGroupedTimeline days={days} profileById={profileById} summaries={summaries} />
      </div>
    </div>
  );
}

function ProjectView({
  turns,
  profileById,
  summaries,
  sp,
}: {
  turns: Turn[];
  profileById: Map<string, Profile>;
  summaries: Map<string, string | null>;
  sp: Record<string, string | string[] | undefined>;
}) {
  const projects = groupByProjectAndDay(turns);
  const buildDrillHref = (root: string) => {
    const merged: Record<string, string> = {};
    for (const [k, v] of Object.entries(sp)) {
      if (typeof v === 'string') merged[k] = v;
    }
    delete merged.view;
    merged.project = root;
    if (merged.range === 'all') delete merged.range;
    const qs = new URLSearchParams(merged).toString();
    return qs ? `/discover?${qs}` : '/discover';
  };
  return (
    <ProjectGrid
      projects={projects}
      profileById={profileById}
      summaries={summaries}
      buildDrillHref={buildDrillHref}
    />
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
  if (merged.range === 'all') delete merged.range;
  if (merged.view === 'time') delete merged.view;

  const qs = new URLSearchParams(merged).toString();
  return qs ? `/discover?${qs}` : '/discover';
}
