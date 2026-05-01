'use client';

// Feishu account linking UI on /me. Two states:
//   - unlinked: a "Bind Feishu account" button that hits the OAuth start
//   - linked:   shows the linked name + email, with an "Unbind" button.

import { useState, useTransition } from 'react';
import { useRouter } from 'next/navigation';

type Props = {
  link: {
    feishu_name: string | null;
    feishu_email: string | null;
    linked_at: string;
  } | null;
};

export function FeishuLink({ link }: Props) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  function handleBind() {
    setError(null);
    // Full navigation — the start route will redirect to Feishu.
    window.location.href = '/api/feishu/oauth/start';
  }

  function handleUnbind() {
    setError(null);
    if (!confirm('Unbind your Feishu account from pmo_agent?')) return;
    startTransition(async () => {
      const r = await fetch('/api/feishu/unbind', { method: 'POST' });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}));
        setError(j.error ?? `unbind failed (${r.status})`);
        return;
      }
      router.refresh();
    });
  }

  if (!link) {
    return (
      <div className="rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
        <p className="text-sm text-zinc-600 dark:text-zinc-400">
          Bind your Feishu account so the PMO bot can recognize you when
          you say &ldquo;我&rdquo; / &ldquo;me&rdquo; in chat.
        </p>
        <button
          type="button"
          onClick={handleBind}
          className="mt-3 rounded bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-indigo-500"
        >
          Bind Feishu account
        </button>
        {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
      </div>
    );
  }

  return (
    <div className="rounded-md border border-zinc-200 p-4 dark:border-zinc-800">
      <p className="text-sm text-zinc-700 dark:text-zinc-300">
        Linked as{' '}
        <strong>{link.feishu_name ?? '(unnamed)'}</strong>
        {link.feishu_email ? (
          <span className="text-zinc-500 dark:text-zinc-500"> · {link.feishu_email}</span>
        ) : null}
      </p>
      <p className="mt-1 text-xs text-zinc-400 dark:text-zinc-500">
        Linked {new Date(link.linked_at).toLocaleString()}.
      </p>
      <button
        type="button"
        onClick={handleUnbind}
        disabled={isPending}
        className="mt-3 rounded border border-zinc-300 px-3 py-1.5 text-xs text-zinc-600 transition hover:border-zinc-400 hover:text-zinc-900 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-400 dark:hover:border-zinc-500 dark:hover:text-zinc-100"
      >
        {isPending ? 'Unbinding…' : 'Unbind'}
      </button>
      {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
    </div>
  );
}
