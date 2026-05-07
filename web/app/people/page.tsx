import { adminClient } from '@/lib/supabase-admin';
import {
  type PeopleMemoryRow,
  type PublicPersonMemory,
  toPublicPeopleMemory,
} from '@/lib/people-memory';

export const dynamic = 'force-dynamic';

export default async function PeoplePage() {
  const { data, error } = await adminClient()
    .from('people_memory')
    .select(
      'display_name, handle, pmo_notes, notes_updated_at, last_observed_at, created_at, updated_at',
    )
    .not('pmo_notes', 'is', null)
    .order('last_observed_at', { ascending: false })
    .limit(500);

  if (error) {
    throw new Error(`failed to load people memory: ${error.message}`);
  }

  const people = toPublicPeopleMemory((data ?? []) as PeopleMemoryRow[]);

  return (
    <main className="mx-auto max-w-5xl px-4 py-8 sm:py-12">
      <header className="mb-8 border-b border-zinc-200 pb-5 dark:border-zinc-800">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-3xl font-semibold tracking-tight text-zinc-950 dark:text-zinc-50">
              People
            </h1>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-zinc-600 dark:text-zinc-400">
              Public PMO notes about people the bot has observed in opted-in chats.
            </p>
          </div>
          <p className="text-sm text-zinc-500 dark:text-zinc-400">
            {people.length} {people.length === 1 ? 'person' : 'people'}
          </p>
        </div>
      </header>

      {people.length === 0 ? (
        <EmptyState />
      ) : (
        <section aria-label="People memory" className="divide-y divide-zinc-200 dark:divide-zinc-800">
          {people.map((person, index) => (
            <PersonRow key={`${person.viewKey}:${index}`} person={person} />
          ))}
        </section>
      )}
    </main>
  );
}

function PersonRow({ person }: { person: PublicPersonMemory }) {
  return (
    <article className="grid gap-4 py-6 md:grid-cols-[14rem_minmax(0,1fr)]">
      <div className="min-w-0">
        <h2 className="truncate text-base font-semibold text-zinc-950 dark:text-zinc-50">
          {person.displayName}
        </h2>
        {person.handle && (
          <p className="mt-1 font-mono text-xs text-zinc-500 dark:text-zinc-400">
            @{person.handle}
          </p>
        )}
        <div className="mt-3 space-y-1 text-xs text-zinc-500 dark:text-zinc-400">
          {person.lastObservedAt && (
            <p>Observed {formatDateTime(person.lastObservedAt)}</p>
          )}
          {person.notesUpdatedAt && (
            <p>Updated {formatDateTime(person.notesUpdatedAt)}</p>
          )}
        </div>
      </div>
      <p className="whitespace-pre-wrap text-sm leading-6 text-zinc-800 dark:text-zinc-200">
        {person.note}
      </p>
    </article>
  );
}

function EmptyState() {
  return (
    <section className="border border-dashed border-zinc-300 px-6 py-14 text-center dark:border-zinc-700">
      <h2 className="text-base font-medium text-zinc-900 dark:text-zinc-100">
        No people notes yet.
      </h2>
      <p className="mt-2 text-sm text-zinc-500 dark:text-zinc-400">
        Notes appear after chat memory has observed enough opted-in group context.
      </p>
    </section>
  );
}

function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('en', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(date);
}
