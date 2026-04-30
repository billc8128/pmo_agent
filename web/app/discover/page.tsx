// /discover — global feed with per-user tab filtering.
//
// Anonymous viewers can read everything (RLS allows public select on
// turns and profiles). The default "All" tab interleaves all users'
// turns reverse-chronologically; per-user tabs filter to one user.
//
// MVP fetches the latest 100 turns; pagination ("load more") is left
// for a future iteration.

import Link from 'next/link';
import { serverClient } from '@/lib/supabase';
import type { Turn, Profile } from '@/lib/types';
import { DiscoverTurn } from './discover-turn';

export const dynamic = 'force-dynamic';

const PAGE_SIZE = 100;

export default async function DiscoverPage(props: PageProps<'/discover'>) {
  const sp = await props.searchParams;
  const tab = typeof sp.handle === 'string' ? sp.handle : 'all';

  const sb = serverClient();

  // Profiles for the tab strip (and to attach handles to turns in the
  // "All" view).
  const { data: profilesRaw } = await sb
    .from('profiles')
    .select('id, handle, display_name, created_at')
    .order('created_at', { ascending: true });
  const profiles: Profile[] = profilesRaw ?? [];
  const profileById = new Map(profiles.map((p) => [p.id, p]));

  // Build the turns query, optionally filtered by handle.
  let query = sb
    .from('turns')
    .select('*')
    .order('user_message_at', { ascending: false })
    .limit(PAGE_SIZE);

  if (tab !== 'all') {
    const target = profiles.find((p) => p.handle === tab);
    if (target) {
      query = query.eq('user_id', target.id);
    } else {
      // Unknown tab: show empty rather than 404. Encourages typo
      // recovery via the visible tab strip.
      query = query.eq('user_id', '00000000-0000-0000-0000-000000000000');
    }
  }

  const { data: turnsData } = await query;
  const turns: Turn[] = turnsData ?? [];

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

      {/* Tab strip */}
      <nav className="mb-6 flex flex-wrap gap-1 overflow-x-auto" aria-label="Filter by user">
        <TabLink href="/discover" active={tab === 'all'} label="All" />
        {profiles.map((p) => (
          <TabLink
            key={p.id}
            href={`/discover?handle=${encodeURIComponent(p.handle)}`}
            active={tab === p.handle}
            label={`@${p.handle}`}
          />
        ))}
      </nav>

      {turns.length === 0 ? (
        <p className="text-zinc-500 dark:text-zinc-400">
          No turns yet.
        </p>
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

function TabLink({
  href,
  active,
  label,
}: {
  href: string;
  active: boolean;
  label: string;
}) {
  const base =
    'rounded-full px-3 py-1 text-xs font-medium transition whitespace-nowrap';
  const cls = active
    ? `${base} bg-zinc-900 text-white dark:bg-zinc-100 dark:text-zinc-900`
    : `${base} bg-zinc-100 text-zinc-700 hover:bg-zinc-200 dark:bg-zinc-800 dark:text-zinc-300 dark:hover:bg-zinc-700`;
  return (
    <Link href={href} className={cls} prefetch={false}>
      {label}
    </Link>
  );
}
