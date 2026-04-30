'use client';

// One row in the timeline: a USER block + an AGENT block.
//
// USER block: full prompt as plain pre-wrapped text. Already redacted
// upstream (in the daemon, before upload).
//
// AGENT block has three states (handled inline below):
//   1. agent_response_full empty → "(no agent response — interrupted)".
//      Real situation: the user sent a prompt but the assistant never
//      finished replying (next prompt came first, network dropped, etc).
//   2. summary populated → show it by default, expand for full markdown.
//   3. response present but summary null → "Summary updating…" — the
//      summarize trigger hasn't completed yet (or failed). A page reload
//      usually pulls in the fresh summary.

import { useState } from 'react';
import type { Turn } from '@/lib/types';
import { ResponseMarkdown } from './response-markdown';

export function TurnCard({ turn }: { turn: Turn }) {
  const [expanded, setExpanded] = useState(false);

  const t = new Date(turn.user_message_at);
  const time = t.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const date = t.toLocaleDateString([], {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });

  const agentLabel =
    turn.agent === 'claude_code'
      ? 'CC'
      : turn.agent === 'codex'
        ? 'CDX'
        : turn.agent;
  const projectName = turn.project_path
    ? turn.project_path.split('/').filter(Boolean).pop() ?? turn.project_path
    : null;
  const deviceLabel = turn.device_label?.trim() || null;
  const responseEmpty =
    !turn.agent_response_full || turn.agent_response_full.trim() === '';

  return (
    <article className="rounded-lg border border-zinc-200 bg-white px-5 py-4 shadow-sm dark:border-zinc-800 dark:bg-zinc-900">
      {/* Meta line */}
      <div className="mb-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-zinc-500 dark:text-zinc-400">
        <time dateTime={turn.user_message_at} title={t.toString()}>
          {date} · {time}
        </time>
        <span aria-hidden="true">·</span>
        <span className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wide text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
          {agentLabel}
        </span>
        {projectName && (
          <>
            <span aria-hidden="true">·</span>
            <span className="font-mono text-[11px]">{projectName}</span>
          </>
        )}
        {deviceLabel && (
          <>
            <span aria-hidden="true">·</span>
            <span
              className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-[10px] text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400"
              title={`uploaded by ${deviceLabel}`}
            >
              {deviceLabel}
            </span>
          </>
        )}
      </div>

      {/* USER */}
      <div className="mb-3">
        <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
          You
        </div>
        <pre className="whitespace-pre-wrap break-words font-sans text-sm leading-relaxed text-zinc-900 dark:text-zinc-100">
          {turn.user_message}
        </pre>
      </div>

      {/* AGENT */}
      <div>
        <div className="mb-1 flex items-center gap-2">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-indigo-600 dark:text-indigo-400">
            Agent
          </span>
          {turn.agent_summary && !responseEmpty && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="rounded border border-zinc-300 px-2 py-0.5 text-[11px] text-zinc-600 transition hover:border-zinc-400 hover:text-zinc-900 dark:border-zinc-700 dark:text-zinc-400 dark:hover:border-zinc-500 dark:hover:text-zinc-100"
              aria-expanded={expanded}
            >
              {expanded ? 'collapse' : 'expand'}
            </button>
          )}
        </div>

        {responseEmpty ? (
          <p className="text-sm italic text-zinc-500 dark:text-zinc-400">
            (no agent response — interrupted before the assistant could reply)
          </p>
        ) : turn.agent_summary === null ? (
          <p className="text-sm italic text-zinc-500 dark:text-zinc-400">
            Summary updating…
          </p>
        ) : !expanded ? (
          <p className="text-sm leading-relaxed text-zinc-800 dark:text-zinc-200">
            {turn.agent_summary}
          </p>
        ) : (
          <ResponseMarkdown source={turn.agent_response_full ?? ''} />
        )}
      </div>
    </article>
  );
}
