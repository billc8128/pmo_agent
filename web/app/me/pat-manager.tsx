'use client';

// PAT manager — read-only listing + revoke.
//
// New tokens are created by the CLI auth flow (pmo-agent login →
// /cli-auth), not from this page. This avoids the failure mode where
// a user creates a plaintext token, looks at it, copies it wrong, and
// loses it. Putting the mint on the daemon side means plaintext
// flows directly into ~/.pmo-agent/config.toml without ever going
// through the user's clipboard.

import { useState, useTransition } from 'react';
import { revokeToken } from './actions';

type TokenRow = {
  id: string;
  label: string | null;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
};

export function PatManager({ tokens }: { tokens: TokenRow[] }) {
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const [revokingId, setRevokingId] = useState<string | null>(null);

  function onRevoke(id: string) {
    if (!confirm('Revoke this token? Daemons using it will start failing immediately.')) {
      return;
    }
    setError(null);
    setRevokingId(id);
    startTransition(async () => {
      try {
        await revokeToken(id);
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setRevokingId(null);
      }
    });
  }

  return (
    <div className="flex flex-col gap-3">
      {error && (
        <p className="rounded bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950 dark:text-red-300">
          {error}
        </p>
      )}

      {tokens.length === 0 ? (
        <div className="rounded-md border border-dashed border-zinc-300 px-4 py-6 text-center dark:border-zinc-700">
          <p className="text-sm text-zinc-600 dark:text-zinc-400">
            No daemons connected yet.
          </p>
          <p className="mt-2 text-xs text-zinc-500 dark:text-zinc-500">
            On your machine, run:{' '}
            <code className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-xs dark:bg-zinc-800">
              pmo-agent login
            </code>
          </p>
        </div>
      ) : (
        <ul className="divide-y divide-zinc-200 rounded border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
          {tokens.map((t) => (
            <li key={t.id} className="flex items-center justify-between gap-3 px-3 py-2">
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="truncate text-sm font-medium text-zinc-900 dark:text-zinc-100">
                    {t.label ?? '(no label)'}
                  </span>
                  {t.revoked_at && (
                    <span className="rounded bg-zinc-100 px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
                      revoked
                    </span>
                  )}
                </div>
                <div className="mt-0.5 flex flex-wrap gap-x-3 text-[11px] text-zinc-500 dark:text-zinc-400">
                  <span>created {new Date(t.created_at).toLocaleDateString()}</span>
                  <span>
                    last used{' '}
                    {t.last_used_at
                      ? new Date(t.last_used_at).toLocaleDateString()
                      : '—'}
                  </span>
                </div>
              </div>
              {!t.revoked_at && (
                <button
                  onClick={() => onRevoke(t.id)}
                  disabled={revokingId === t.id}
                  className="rounded border border-zinc-300 px-2 py-1 text-xs text-zinc-600 transition hover:border-red-400 hover:text-red-600 disabled:opacity-50 dark:border-zinc-700 dark:text-zinc-400"
                >
                  {revokingId === t.id ? 'Revoking…' : 'Revoke'}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
