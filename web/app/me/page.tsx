// /me — authenticated dashboard.
//
// Three concerns, in order:
//   1. Session: redirect to /login if not signed in.
//   2. Profile: if missing, show onboarding (handle picker) before
//      rendering the rest.
//   3. PAT management.

import { redirect } from 'next/navigation';
import { serverComponentClient } from '@/lib/supabase-server';
import { OnboardingForm } from './onboarding-form';
import { ProfileEditor } from './profile-editor';
import { PatManager } from './pat-manager';

export const dynamic = 'force-dynamic';

export default async function MePage() {
  const sb = await serverComponentClient();

  const {
    data: { user },
  } = await sb.auth.getUser();

  if (!user) {
    redirect('/login');
  }

  // Try to load the profile. RLS on profiles allows public select.
  const { data: profile } = await sb
    .from('profiles')
    .select('id, handle, display_name, created_at')
    .eq('id', user.id)
    .maybeSingle();

  // First-time visit: no profile row yet. Suggest a default handle and
  // ask the user to confirm/change it.
  if (!profile) {
    const suggested = suggestHandle(user);
    return (
      <main className="mx-auto max-w-xl px-4 py-12">
        <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
          Pick a handle
        </h1>
        <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-400">
          This is the URL slug for your public timeline:
          <code className="mx-1 rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-xs dark:bg-zinc-800">
            /u/&lt;handle&gt;
          </code>
          . You can change it later.
        </p>
        <div className="mt-6">
          <OnboardingForm
            userId={user.id}
            email={user.email ?? null}
            suggestedHandle={suggested}
          />
        </div>
      </main>
    );
  }

  // Load tokens, newest first.
  const { data: tokens } = await sb
    .from('tokens')
    .select('id, label, created_at, last_used_at, revoked_at')
    .eq('user_id', user.id)
    .order('created_at', { ascending: false });

  return (
    <main className="mx-auto max-w-2xl px-4 py-12">
      <header className="mb-8 flex items-end justify-between gap-4 border-b border-zinc-200 pb-6 dark:border-zinc-800">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
            {profile.display_name ?? profile.handle}
          </h1>
          <p className="mt-1 text-sm text-zinc-500 dark:text-zinc-400">
            Signed in as {user.email}
          </p>
          <p className="mt-2 text-xs text-zinc-400 dark:text-zinc-500">
            Public profile:{' '}
            <a
              href={`/u/${profile.handle}`}
              className="text-indigo-600 underline decoration-dotted underline-offset-2 dark:text-indigo-400"
            >
              /u/{profile.handle}
            </a>
          </p>
        </div>
        <form action="/auth/signout" method="post">
          <button
            type="submit"
            className="rounded border border-zinc-300 px-3 py-1.5 text-xs text-zinc-600 transition hover:border-zinc-400 hover:text-zinc-900 dark:border-zinc-700 dark:text-zinc-400 dark:hover:border-zinc-500 dark:hover:text-zinc-100"
          >
            Sign out
          </button>
        </form>
      </header>

      <section className="mb-10">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
          Profile
        </h2>
        <ProfileEditor
          initialHandle={profile.handle}
          initialDisplayName={profile.display_name ?? ''}
        />
      </section>

      <section>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
          Daemon tokens
        </h2>
        <p className="mb-4 text-xs text-zinc-500 dark:text-zinc-400">
          A token authenticates the daemon on your machine. The plaintext
          is shown ONCE at creation — copy it into{' '}
          <code className="rounded bg-zinc-100 px-1 py-0.5 font-mono text-[11px] dark:bg-zinc-800">
            pmo-agent login
          </code>{' '}
          and store it nowhere else.
        </p>
        <PatManager
          tokens={(tokens ?? []).map((t) => ({
            id: t.id,
            label: t.label,
            created_at: t.created_at,
            last_used_at: t.last_used_at,
            revoked_at: t.revoked_at,
          }))}
        />
      </section>
    </main>
  );
}

// suggestHandle picks a default handle from the user's email or sub.
// e.g. "alice@example.com" → "alice"; falls back to "user_<6chars>".
function suggestHandle(user: { id: string; email?: string | null }): string {
  if (user.email) {
    const local = user.email.split('@')[0] ?? '';
    const cleaned = local.toLowerCase().replace(/[^a-z0-9_-]/g, '').slice(0, 24);
    if (cleaned.length >= 2) return cleaned;
  }
  return `user_${user.id.slice(0, 6)}`;
}
