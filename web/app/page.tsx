// Landing page.

import Link from 'next/link';
import { serverClient } from '@/lib/supabase';
import { CopyCommand } from './_components/copy-command';

export const dynamic = 'force-dynamic';

export default async function Home() {
  // Pull a tiny bit of live data so the landing reflects reality
  // ("X turns from Y people, last update Z").
  const sb = serverClient();
  const { count: turnCount } = await sb
    .from('turns')
    .select('id', { count: 'exact', head: true })
    .not('agent_response_full', 'is', null)
    .neq('agent_response_full', '');
  const { count: profileCount } = await sb
    .from('profiles')
    .select('id', { count: 'exact', head: true });
  const { data: latest } = await sb
    .from('turns')
    .select('user_message_at')
    .not('agent_response_full', 'is', null)
    .neq('agent_response_full', '')
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
        <a
          href="#install"
          className="rounded-md border border-zinc-300 px-4 py-2 text-sm font-medium text-zinc-700 transition hover:border-zinc-400 hover:bg-zinc-50 dark:border-zinc-700 dark:text-zinc-300 dark:hover:border-zinc-600 dark:hover:bg-zinc-900"
        >
          Get started →
        </a>
      </div>

      {/* Install — the most important section for new visitors */}
      <section id="install" className="mt-16 scroll-mt-4 border-t border-zinc-200 pt-8 dark:border-zinc-800">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
          Install (macOS / Linux)
        </h2>
        <p className="mt-3 text-sm leading-relaxed text-zinc-600 dark:text-zinc-300">
          One command — auto-detects your platform and downloads the
          binary from{' '}
          <a
            href="https://github.com/billc8128/pmo_agent/releases"
            target="_blank"
            rel="noopener noreferrer"
            className="text-indigo-600 underline decoration-dotted underline-offset-2 dark:text-indigo-400"
          >
            GitHub releases
          </a>
          .
        </p>
        <div className="mt-3">
          <CopyCommand command="curl -fsSL https://pmo-agent-sigma.vercel.app/install.sh | bash" />
        </div>
      </section>

      {/* Then run these */}
      <section className="mt-10">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
          Then in your terminal
        </h2>

        <ol className="mt-4 space-y-5">
          <Step
            n={1}
            title="Authorize this machine"
            sub="Opens your browser. Sign in with Google, pick a handle, click Authorize."
          >
            <CopyCommand command="pmo-agent login" />
          </Step>
          <Step
            n={2}
            title="Install as a background service"
            sub="Registers a macOS LaunchAgent so the daemon survives reboots and closed terminals."
          >
            <CopyCommand command="pmo-agent install" />
          </Step>
          <Step
            n={3}
            title="That's it"
            sub="Open Claude Code or Codex. Each completed turn appears on your profile within seconds."
          >
            <p className="text-xs text-zinc-500 dark:text-zinc-400">
              You&apos;ll get a macOS notification each time new turns
              are uploaded (throttled to once every 5&nbsp;min).
            </p>
          </Step>
        </ol>
      </section>

      {/* Quick reference */}
      <section className="mt-12 rounded-lg border border-zinc-200 bg-zinc-50 p-5 dark:border-zinc-800 dark:bg-zinc-900">
        <h2 className="text-sm font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
          Useful commands
        </h2>
        <dl className="mt-4 space-y-3 text-sm">
          <CommandRef cmd="pmo-agent status" desc="Show service state, recent uploads, and discovered transcripts." />
          <CommandRef cmd="pmo-agent uninstall" desc="Remove the LaunchAgent. Doesn't delete config or local state." />
          <CommandRef cmd="tail -f ~/.pmo-agent/daemon.log" desc="Follow the live daemon log." />
        </dl>
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

      <footer className="mt-16 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-zinc-400 dark:text-zinc-500">
        <span>Public-by-default. No private mode in MVP.</span>
        <span aria-hidden="true">·</span>
        <a
          href="https://github.com/billc8128/pmo_agent"
          target="_blank"
          rel="noopener noreferrer"
          className="underline decoration-dotted hover:text-zinc-600 dark:hover:text-zinc-300"
        >
          source on GitHub
        </a>
        <span aria-hidden="true">·</span>
        <span>Don&apos;t paste secrets into your AI agent.</span>
      </footer>
    </main>
  );
}

function Step({
  n,
  title,
  sub,
  children,
}: {
  n: number;
  title: string;
  sub?: string;
  children: React.ReactNode;
}) {
  return (
    <li className="flex gap-4">
      <span className="flex h-7 w-7 flex-none items-center justify-center rounded-full bg-zinc-900 text-xs font-semibold text-white dark:bg-zinc-100 dark:text-zinc-900">
        {n}
      </span>
      <div className="min-w-0 flex-1 pt-0.5">
        <div className="font-medium text-zinc-900 dark:text-zinc-100">
          {title}
        </div>
        {sub && (
          <div className="mt-0.5 mb-2 text-xs leading-relaxed text-zinc-500 dark:text-zinc-400">
            {sub}
          </div>
        )}
        {children}
      </div>
    </li>
  );
}

function CommandRef({ cmd, desc }: { cmd: string; desc: string }) {
  return (
    <div className="flex flex-col gap-1 sm:flex-row sm:items-baseline sm:gap-3">
      <code className="shrink-0 rounded bg-white px-2 py-0.5 font-mono text-xs text-zinc-900 dark:bg-zinc-950 dark:text-zinc-100">
        {cmd}
      </code>
      <span className="text-xs text-zinc-500 dark:text-zinc-400">{desc}</span>
    </div>
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
