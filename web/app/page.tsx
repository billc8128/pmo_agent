// Landing page. For MVP we redirect to the single demo profile;
// Milestone 5 will replace this with a proper landing/discover page.

import Link from 'next/link';

export default function Home() {
  return (
    <main className="mx-auto max-w-2xl px-4 py-16 sm:py-24">
      <h1 className="text-3xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
        pmo_agent
      </h1>
      <p className="mt-3 text-zinc-600 dark:text-zinc-400">
        Public timelines of local AI-coding sessions. A daemon on your
        machine watches Claude Code and Codex transcripts and uploads
        each turn here, with one-sentence summaries.
      </p>
      <p className="mt-8 text-sm text-zinc-500 dark:text-zinc-400">
        Demo profile:{' '}
        <Link
          href="/u/tester"
          className="text-indigo-600 underline decoration-dotted underline-offset-2 hover:decoration-solid dark:text-indigo-400"
        >
          /u/tester
        </Link>
      </p>
    </main>
  );
}
