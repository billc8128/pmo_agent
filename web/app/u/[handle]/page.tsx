// /u/:handle — public timeline of one user's local AI-coding sessions.
//
// Server-rendered: anonymous viewers don't need JS to see the timeline.
// A small client island handles the "expand response" toggle and 10s
// polling for new turns (Milestones 3.4 and 3.5).

import { notFound } from 'next/navigation';
import { serverClient } from '@/lib/supabase';
import type { Profile, Turn } from '@/lib/types';
import { TimelineClient } from './timeline-client';

// Always render fresh — turns are mutable and we want the SSR'd page
// to reflect "right now". The client island will then poll for newer
// turns.
export const dynamic = 'force-dynamic';

const PAGE_SIZE = 50;

export default async function ProfilePage(props: PageProps<'/u/[handle]'>) {
  const { handle } = await props.params;
  const sb = serverClient();

  // Fetch the profile by handle. Single().limit(1) would be fine; we
  // use maybeSingle so 404 is a clean null vs throwing.
  const { data: profile, error: profileErr } = await sb
    .from('profiles')
    .select('id, handle, display_name, created_at')
    .eq('handle', handle)
    .maybeSingle<Profile>();

  if (profileErr) {
    // RLS prevents most read errors; surface anything else explicitly.
    throw new Error(`Failed to load profile: ${profileErr.message}`);
  }
  if (!profile) {
    notFound();
  }

  // Most-recent turns first. PAGE_SIZE matches what the client polling
  // expects to fit in memory; the cursor for "load older" is future
  // work (Milestone 5).
  const { data: turnsData, error: turnsErr } = await sb
    .from('turns')
    .select('*')
    .eq('user_id', profile.id)
    .order('user_message_at', { ascending: false })
    .limit(PAGE_SIZE);

  if (turnsErr) {
    throw new Error(`Failed to load turns: ${turnsErr.message}`);
  }
  const turns: Turn[] = turnsData ?? [];

  return (
    <main className="mx-auto max-w-3xl px-4 py-8 sm:py-12">
      <header className="mb-8 border-b border-zinc-200 pb-6 dark:border-zinc-800">
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

      {turns.length === 0 ? (
        <p className="text-zinc-500 dark:text-zinc-400">
          No turns yet. The daemon will upload them as they happen.
        </p>
      ) : (
        <TimelineClient userId={profile.id} initialTurns={turns} />
      )}
    </main>
  );
}
