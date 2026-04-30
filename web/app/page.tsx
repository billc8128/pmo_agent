// Landing page.

import Link from 'next/link';
import { serverClient } from '@/lib/supabase';

export const dynamic = 'force-dynamic';

export default async function Home() {
  // Pull a tiny bit of live data so the landing reflects reality
  // ("X turns from Y people, last update Z").
  const sb = serverClient();
  const { count: turnCount } = await sb
    .from('turns')
    .select('id', { count: 'exact', head: true });
  const { count: profileCount } = await sb
    .from('profiles')
    .select('id', { count: 'exact', head: true });
  const { data: latest } = await sb
    .from('turns')
    .select('user_message_at')
    .order('user_message_at', { ascending: false })
    .limit(1)
    .maybeSingle();

  const latestAgo = latest?.user_message_at
    ? humanAgo(new Date(latest.user_message_at))
    : null;

  return (
    <main className="mx-auto max-w-2xl px-4 py-12 sm:py-20">
      <h1 className="text-4xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
        Public timelines of your AI-coding sessions.
      </h1>
      <p className="mt-4 text-lg leading-relaxed text-zinc-600 dark:text-zinc-300">
        A small daemon on your machine watches your Claude Code and
        Codex transcripts. Each finished turn gets a one-line summary
        and shows up at <code className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-base dark:bg-zinc-800">/u/&lt;your-handle&gt;</code>.
      </p>

      {/* Live counts */}
      {turnCount != null && profileCount != null && (
        <p className="mt-6 text-sm text-zinc-500 dark:text-zinc-400">
          {turnCount} turn{turnCount === 1 ? '' : 's'} from {profileCount}{' '}
          {profileCount === 1 ? 'person' : 'people'}
          {latestAgo && <> · last update {latestAgo}</>}
        </p>
      )}

      {/* Primary CTA */}
      <div className="mt-8 flex flex-wrap gap-3">
        <Link
          href="/discover"
          className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-zinc-800 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-200"
        >
          See what people are doing →
        </Link>
        <Link
          href="/login"
          className="rounded-md border border-zinc-300 px-4 py-2 text-sm font-medium text-zinc-700 transition hover:border-zinc-400 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:border-zinc-600 dark:hover:bg-zinc-900"
        >
          Sign in to publish your own
        </Link>
      </div>

      {/* How it works */}
      <section className="mt-16 border-t border-zinc-200 pt-8 dark:border-zinc-800">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
          How it works
        </h2>
        <ol className="mt-4 space-y-4">
          <Step n={1} title="Sign in with Google, pick a handle">
            That gives you the URL{' '}
            <code className="rounded bg-zinc-100 px-1 py-0.5 font-mono text-xs dark:bg-zinc-800">
              /u/&lt;handle&gt;
            </code>
            .
          </Step>
          <Step n={2} title="Mint a daemon token on /me">
            One token per machine. Plaintext is shown once, only its
            hash is stored. Revoke from the same page.
          </Step>
          <Step n={3} title="Run pmo-agent on your machine">
            <code className="rounded bg-zinc-100 px-1 py-0.5 font-mono text-xs dark:bg-zinc-800">
              pmo-agent login
            </code>
            , paste the token, then{' '}
            <code className="rounded bg-zinc-100 px-1 py-0.5 font-mono text-xs dark:bg-zinc-800">
              pmo-agent start
            </code>
            . New turns appear on your profile within a few seconds.
          </Step>
        </ol>
      </section>

      {/* What gets published */}
      <section className="mt-12 rounded-lg border border-zinc-200 bg-zinc-50 p-5 dark:border-zinc-800 dark:bg-zinc-900">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
          What gets published
        </h2>
        <ul className="mt-3 space-y-2 text-sm leading-relaxed text-zinc-700 dark:text-zinc-300">
          <li>
            <strong>Your prompts</strong> as plain text.
          </li>
          <li>
            <strong>The agent&apos;s response</strong> as a one-line
            summary by default; click <em>expand</em> for the full
            markdown response and tool calls.
          </li>
          <li>
            <strong>Tool calls</strong> as one-line tags (e.g.{' '}
            <code className="rounded bg-zinc-100 px-1 font-mono text-xs dark:bg-zinc-800">
              [Bash] command=ls -la
            </code>
            ). Tool <em>output</em> (file contents, command stdout) is
            never published.
          </li>
        </ul>
      </section>

      <footer className="mt-16 text-xs text-zinc-400 dark:text-zinc-500">
        Public-by-default. No private mode in MVP. Don&apos;t paste
        secrets into your AI agent.
      </footer>
    </main>
  );
}

function Step({
  n,
  title,
  children,
}: {
  n: number;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <li className="flex gap-4">
      <span className="flex h-7 w-7 flex-none items-center justify-center rounded-full bg-zinc-900 text-xs font-semibold text-white dark:bg-zinc-100 dark:text-zinc-900">
        {n}
      </span>
      <div className="pt-0.5">
        <div className="font-medium text-zinc-900 dark:text-zinc-100">
          {title}
        </div>
        <div className="mt-0.5 text-sm leading-relaxed text-zinc-600 dark:text-zinc-400">
          {children}
        </div>
      </div>
    </li>
  );
}

// Tiny humanizer; we only need rough buckets, no library.
function humanAgo(d: Date): string {
  const ms = Date.now() - d.getTime();
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return 'just now';
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} min ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ago`;
  const day = Math.floor(hr / 24);
  if (day < 7) return `${day}d ago`;
  return d.toLocaleDateString();
}
