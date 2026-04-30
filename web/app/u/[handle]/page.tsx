// /u/:handle — public timeline of one user's local AI-coding sessions.
//
// Server-rendered: anonymous viewers don't need JS to see the timeline.
// A small client island handles the "expand response" toggle and 10s
// polling for new turns.
//
// Three views via the ?view= query param:
//   ?view=timeline (default) — newest-first feed of turns
//   ?view=projects           — turns grouped by project root
//
// Date filter via ?range=  (today | 7d | 30d | all). Applies to both views.
//
// Optional ?project=<encoded-path> filters timeline to one project root,
// linked from a project card click.

import { notFound } from 'next/navigation';
import Link from 'next/link';
import { serverClient } from '@/lib/supabase';
import type { Profile, Turn } from '@/lib/types';
import {
  dateRangeStartISO,
  groupTurnsByProject,
  parseDateRange,
  projectRootFromPath,
} from '@/lib/grouping';
import { TimelineClient } from './timeline-client';
import { parseView, ViewTabs } from '../../_components/view-tabs';
import { ProjectCard } from '../../_components/project-card';

export const dynamic = 'force-dynamic';

const PAGE_SIZE = 200; // larger than timeline-only because projects view needs more

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

  if (profileErr) {
    throw new Error(`Failed to load profile: ${profileErr.message}`);
  }
  if (!profile) {
    notFound();
  }

  let query = sb
    .from('turns')
    .select('*')
    .eq('user_id', profile.id)
    .order('user_message_at', { ascending: false })
    .limit(PAGE_SIZE);

  const since = dateRangeStartISO(range);
  if (since) {
    query = query.gte('user_message_at', since);
  }

  const { data: turnsData, error: turnsErr } = await query;
  if (turnsErr) {
    throw new Error(`Failed to load turns: ${turnsErr.message}`);
  }
  let turns: Turn[] = turnsData ?? [];

  // Project drill-in filter (timeline view only). We compute the root
  // client-side, so the filter happens here too.
  if (view === 'timeline' && projectFilter) {
    turns = turns.filter(
      (t) => projectRootFromPath(t.project_path) === projectFilter,
    );
  }

  const basePath = `/u/${handle}`;

  return (
    <main className="mx-auto max-w-3xl px-4 py-8 sm:py-12">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
          {profile.display_name ?? profile.handle}
        </h1>
        <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
          @{profile.handle} · public timeline of local AI-coding sessions
        </p>
        <p className="mt-2 text-xs text-zinc-400 dark:text-zinc-500">
          Everything below was captured by an AI coding agent on this user&apos;s
          local machine and uploaded automatically. No manual editing.
        </p>
      </header>

      <ViewTabs basePath={basePath} searchParams={sp} view={view} range={range} />

      {view === 'timeline' && projectFilter && (
        <ProjectFilterBadge basePath={basePath} searchParams={sp} project={projectFilter} />
      )}

      {turns.length === 0 ? (
        <p className="text-zinc-500 dark:text-zinc-400">
          No turns in this view.
        </p>
      ) : view === 'projects' ? (
        <ProjectsGrid turns={turns} basePath={basePath} searchParams={sp} />
      ) : (
        <TimelineClient userId={profile.id} initialTurns={turns} />
      )}
    </main>
  );
}

// ProjectsGrid — server-renders a card per project root.
function ProjectsGrid({
  turns,
  basePath,
  searchParams,
}: {
  turns: Turn[];
  basePath: string;
  searchParams: Record<string, string | string[] | undefined>;
}) {
  const groups = groupTurnsByProject(turns, 3);
  const sp = stringParams(searchParams);
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
      {groups.map((g) => {
        // Click drills into timeline view filtered to this project
        // root, preserving the active date range.
        const params: Record<string, string> = { ...sp, project: g.root };
        delete params.view; // default = timeline
        const qs = new URLSearchParams(params).toString();
        const drillHref = qs ? `${basePath}?${qs}` : basePath;
        return <ProjectCard key={g.root} group={g} drillHref={drillHref} />;
      })}
    </div>
  );
}

// ProjectFilterBadge — chip shown when ?project=... is active, with
// a clear-link to remove the filter.
function ProjectFilterBadge({
  basePath,
  searchParams,
  project,
}: {
  basePath: string;
  searchParams: Record<string, string | string[] | undefined>;
  project: string;
}) {
  const sp = stringParams(searchParams);
  delete sp.project;
  const qs = new URLSearchParams(sp).toString();
  const clearHref = qs ? `${basePath}?${qs}` : basePath;

  // Display name = basename
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

function stringParams(
  src: Record<string, string | string[] | undefined>,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(src)) {
    if (typeof v === 'string') out[k] = v;
  }
  return out;
}
