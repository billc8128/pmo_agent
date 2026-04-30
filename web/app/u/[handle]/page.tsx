// /u/:handle — public timeline of one user's local AI-coding sessions.
//
// Layout:
//   - Date filter chips (Today / 7d / 30d / All) at the top.
//   - Sticky left-side date nav on md+ screens; horizontal scroll
//     strip on narrow screens.
//   - Main column: turns grouped by date → project. Each project
//     block carries an LLM-generated paragraph summarizing recent
//     activity in that project, plus the actual turn cards.
//
// Per spec §6.2: no realtime — page reload picks up new turns.

import { notFound } from 'next/navigation';
import { serverClient } from '@/lib/supabase';
import type { Profile, Turn } from '@/lib/types';
import {
  dateRangeStartISO,
  groupByDayAndProject,
  parseDateRange,
  projectRootFromPath,
} from '@/lib/grouping';
import { loadProjectSummaries } from '@/lib/project-summaries';
import { DateRangeChips } from '../../_components/view-tabs';
import {
  DateGroupedTimeline,
} from '../../_components/date-grouped-timeline';
import { DateNav, MobileDateScroll } from '../../_components/date-nav';
import { ProjectFilterBadge } from '../../_components/project-filter-badge';

export const dynamic = 'force-dynamic';

const PAGE_SIZE = 200;

export default async function ProfilePage(props: PageProps<'/u/[handle]'>) {
  const { handle } = await props.params;
  const sp = await props.searchParams;
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
  let turns: Turn[] = turnsData ?? [];

  if (projectFilter) {
    turns = turns.filter(
      (t) => projectRootFromPath(t.project_path) === projectFilter,
    );
  }

  const days = groupByDayAndProject(turns);
  const summaries = await loadProjectSummaries(turns);

  const basePath = `/u/${handle}`;

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

      <div className="mb-6 flex items-center justify-end border-b border-zinc-200 pb-3 dark:border-zinc-800">
        <DateRangeChips basePath={basePath} searchParams={sp} range={range} />
      </div>

      {projectFilter && (
        <ProjectFilterBadge basePath={basePath} searchParams={sp} project={projectFilter} />
      )}

      {turns.length === 0 ? (
        <p className="text-zinc-500 dark:text-zinc-400">
          No turns in this view.
        </p>
      ) : (
        <div className="grid grid-cols-1 gap-8 md:grid-cols-[10rem_minmax(0,1fr)]">
          <DateNav days={days} />
          <div>
            <MobileDateScroll days={days} />
            <DateGroupedTimeline
              days={days}
              profileById={null}
              summaries={summaries}
            />
          </div>
        </div>
      )}
    </main>
  );
}
