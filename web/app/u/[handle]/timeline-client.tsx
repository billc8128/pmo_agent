'use client';

// TimelineClient renders the list of turns and handles two interactive
// behaviors that can't live in the server component:
//
//   1. Expand the agent's full response (default view shows the
//      one-sentence summary).
//   2. Poll every 10s for newer turns. Per spec §6.2, no realtime;
//      polling is intentional for MVP.
//
// initialTurns from SSR is the most-recent-first slice. Newer turns
// from polling are merged in front, deduped by id.

import { useEffect, useState } from 'react';
import { browserClient } from '@/lib/supabase';
import type { Turn } from '@/lib/types';
import { TurnCard } from './turn-card';

const POLL_INTERVAL_MS = 10_000;

export function TimelineClient({
  userId,
  initialTurns,
}: {
  userId: string;
  initialTurns: Turn[];
}) {
  const [turns, setTurns] = useState<Turn[]>(initialTurns);

  useEffect(() => {
    let cancelled = false;
    const sb = browserClient();

    async function pollOnce() {
      // Use the most recent user_message_at we have as a cursor.
      // (created_at would also work; either is monotonic per row.)
      const cursor = turns[0]?.user_message_at ?? '1970-01-01T00:00:00Z';
      const { data, error } = await sb
        .from('turns')
        .select('*')
        .eq('user_id', userId)
        .gt('user_message_at', cursor)
        .order('user_message_at', { ascending: false })
        .limit(50);

      if (cancelled) return;
      if (error) {
        // Soft-fail: log and try again on the next tick.
        console.error('poll failed:', error.message);
        return;
      }
      if (!data || data.length === 0) return;

      setTurns((prev) => {
        // Dedupe by id in case the cursor missed a write that arrived
        // out of order.
        const seen = new Set(prev.map((t) => t.id));
        const fresh = (data as Turn[]).filter((t) => !seen.has(t.id));
        if (fresh.length === 0) return prev;
        return [...fresh, ...prev];
      });
    }

    const timer = setInterval(pollOnce, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
    // We intentionally don't include `turns` in deps — the cursor is
    // read inside pollOnce closure each tick; rebuilding the interval
    // every render would defeat polling.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId]);

  return (
    <ol className="space-y-6">
      {turns.map((t) => (
        <li key={t.id}>
          <TurnCard turn={t} />
        </li>
      ))}
    </ol>
  );
}
