// /u/:handle — public timeline of one user's local AI-coding sessions.
//
// Two views, switchable via ?view=:
//   ?view=time (default)    — date → project → turns
//   ?view=project           — project → date → turns
//
// Both share the same shell: header, view toggle, date-range chips,
// optional project filter badge, and a left sticky jump-list whose
// content depends on the active view.

import { notFound } from 'next/navigation';
import { serverClient } from '@/lib/supabase';
import type { Profile, Turn } from '@/lib/types';
import {
  dateRangeStartISO,
  groupByDayAndProject,
  groupByProjectAndDay,
  parseDateRange,
  parseView,
  projectRootForTurn,
} from '@/lib/grouping';
import { loadProjectSummaries } from '@/lib/project-summaries';
import { DateRangeChips, ViewToggle } from '../../_components/view-tabs';
import { DateGroupedTimeline } from '../../_components/date-grouped-timeline';
import { DateNav, MobileDateScroll } from '../../_components/date-nav';
import { ProjectGrid } from '../../_components/project-grid';
import { ProjectFilterBadge } from '../../_components/project-filter-badge';
import { ProjectChips } from '../../_components/project-chips';

export const dynamic = 'force-dynamic';

const PAGE_SIZE = 1000;

export default async function ProfilePage(props: PageProps<'/u/[handle]'>) {
  const { handle } = await props.params;
  const sp = await props.searchParams;
  const view = parseView(sp.view);
  const range = parseDateRange(sp.range);
  const projectFilter = typeof sp.project === 'string' ? sp.project : '';

  const sb = serverClient();

  const { data: profile, error: profileErr } = await sb
    .from('profiles')
    .select('id, handle, display_name, created_at')
    .eq('handle', handle)
    .maybeSingle<Profile>();
  if (profileErr) throw new Error(`Failed to load profile: ${profileErr.message}`);
  if (!profile) notFound();

  let query = sb
    .from('turns')
    .select('*')
    .eq('user_id', profile.id)
    .order('user_message_at', { ascending: false })
    .limit(PAGE_SIZE);

  const since = dateRangeStartISO(range);
  if (since) query = query.gte('user_message_at', since);

  const { data: turnsData, error: turnsErr } = await query;
  if (turnsErr) throw new Error(`Failed to load turns: ${turnsErr.message}`);
  const allTurns = (turnsData ?? []) as Turn[];

  // Compute the chip universe BEFORE applying the project filter, so a
  // user already drilled into one project still sees the others as
  // chips and can switch directly.
  const allRoots = collectRoots(allTurns);

  let turns = allTurns;
  if (projectFilter) {
    turns = turns.filter((t) => projectRootForTurn(t) === projectFilter);
  }

  const summaries = await loadProjectSummaries(turns);
  const basePath = `/u/${handle}`;

  // Once a project filter is active, "By project" doesn't make sense —
  // we're already scoped to one project. Force the view back to time.
  const effectiveView = projectFilter ? 'time' : view;

  return (
    <main className="mx-auto max-w-6xl px-4 py-8 sm:py-12">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
          {profile.display_name ?? profile.handle}
        </h1>
        <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          @{profile.handle} · public timeline of local AI-coding sessions
        </p>
      </header>

      <div className="mb-6 flex flex-wrap items-center justify-between gap-3 border-b border-zinc-200 pb-3 dark:border-zinc-800">
        {projectFilter ? (
          // Filtered to one project — view toggle has nothing to switch to.
          <span className="text-xs text-zinc-500 dark:text-zinc-400">Timeline</span>
        ) : (
          <ViewToggle basePath={basePath} searchParams={sp} view={effectiveView} />
        )}
        <DateRangeChips basePath={basePath} searchParams={sp} range={range} />
      </div>

      {/* Project chips appear in time view (where they let the user
          filter) but not project view (the cards already serve that
          role). */}
      {effectiveView === 'time' && (
        <ProjectChips
          basePath={basePath}
          searchParams={sp}
          roots={allRoots}
          active={projectFilter}
        />
      )}

      {projectFilter && (
        <ProjectFilterBadge basePath={basePath} searchParams={sp} project={projectFilter} />
      )}

      {turns.length === 0 ? (
        <p className="text-zinc-500 dark:text-zinc-400">
          No turns in this view.
        </p>
      ) : effectiveView === 'project' ? (
        <ProjectView turns={turns} summaries={summaries} basePath={basePath} sp={sp} />
      ) : (
        <TimeView turns={turns} summaries={summaries} />
      )}
    </main>
  );
}

function collectRoots(turns: Turn[]): string[] {
  const set = new Set<string>();
  for (const t of turns) set.add(projectRootForTurn(t));
  return [...set].sort();
}

function TimeView({
  turns,
  summaries,
}: {
  turns: Turn[];
  summaries: Map<string, string | null>;
}) {
  const days = groupByDayAndProject(turns);
  return (
    <div className="grid grid-cols-1 gap-8 md:grid-cols-[10rem_minmax(0,1fr)]">
      <DateNav days={days} />
      <div>
        <MobileDateScroll days={days} />
        <DateGroupedTimeline days={days} profileById={null} summaries={summaries} />
      </div>
    </div>
  );
}

function ProjectView({
  turns,
  summaries,
  basePath,
  sp,
}: {
  turns: Turn[];
  summaries: Map<string, string | null>;
  basePath: string;
  sp: Record<string, string | string[] | undefined>;
}) {
  const projects = groupByProjectAndDay(turns);
  // Drill into ?project=<root>: this filters to a single project,
  // which auto-switches to time view (handled by effectiveView above).
  // We strip view=project from the link so the URL is clean.
  const buildDrillHref = (root: string) => {
    const merged: Record<string, string> = {};
    for (const [k, v] of Object.entries(sp)) {
      if (typeof v === 'string') merged[k] = v;
    }
    delete merged.view; // back to default time view, scoped by project filter
    merged.project = root;
    if (merged.range === 'all') delete merged.range;
    const qs = new URLSearchParams(merged).toString();
    return qs ? `${basePath}?${qs}` : basePath;
  };
  return (
    <ProjectGrid
      projects={projects}
      profileById={null}
      summaries={summaries}
      buildDrillHref={buildDrillHref}
    />
  );
}
