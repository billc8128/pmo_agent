'use client';

import { useState, useTransition } from 'react';
import { createToken, revokeToken } from './actions';

type TokenRow = {
  id: string;
  label: string | null;
  created_at: string;
  last_used_at: string | null;
  revoked_at: string | null;
};

export function PatManager({ tokens }: { tokens: TokenRow[] }) {
  const [label, setLabel] = useState('');
  const [created, setCreated] = useState<{ plaintext: string; label: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();
  const [revokingId, setRevokingId] = useState<string | null>(null);

  function onCreate() {
    setError(null);
    startTransition(async () => {
      try {
        const out = await createToken(label || 'daemon');
        setCreated(out);
        setLabel('');
      } catch (e) {
        setError((e as Error).message);
      }
    });
  }

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
    <div className="flex flex-col gap-4">
      {/* Create form */}
      <div className="flex items-end gap-2">
        <label className="flex-1 flex flex-col gap-1">
          <span className="text-xs font-medium text-zinc-700 dark:text-zinc-300">
            New token label
          </span>
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="e.g. macbook-pro"
            maxLength={64}
            className="rounded border border-zinc-300 bg-white px-2 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-900"
          />
        </label>
        <button
          onClick={onCreate}
          disabled={pending}
          className="rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-indigo-500 disabled:opacity-60"
        >
          {pending ? 'Creating…' : 'Create token'}
        </button>
      </div>

      {error && (
        <p className="rounded bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950 dark:text-red-300">
          {error}
        </p>
      )}

      {/* One-shot reveal modal */}
      {created && (
        <RevealModal plaintext={created.plaintext} label={created.label} onClose={() => setCreated(null)} />
      )}

      {/* Token list */}
      {tokens.length === 0 ? (
        <p className="text-xs italic text-zinc-500 dark:text-zinc-400">
          No tokens yet. Create one to authenticate the daemon.
        </p>
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

function RevealModal({
  plaintext,
  label,
  onClose,
}: {
  plaintext: string;
  label: string;
  onClose: () => void;
}) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(plaintext);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Older browsers / insecure contexts: fall back to manual.
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4">
      <div className="w-full max-w-md rounded-lg bg-white p-5 shadow-xl dark:bg-zinc-900">
        <h3 className="text-base font-semibold text-zinc-900 dark:text-zinc-100">
          New token: {label}
        </h3>
        <p className="mt-1 text-xs text-zinc-600 dark:text-zinc-400">
          Copy this now. Once you close this dialog, the plaintext is
          gone — we only store its hash.
        </p>

        <div className="mt-4 break-all rounded border border-zinc-200 bg-zinc-50 p-3 font-mono text-[12px] leading-relaxed text-zinc-900 dark:border-zinc-800 dark:bg-zinc-950 dark:text-zinc-100">
          {plaintext}
        </div>

        <div className="mt-4 flex items-center justify-between gap-2">
          <button
            onClick={copy}
            className="rounded-md bg-indigo-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-indigo-500"
          >
            {copied ? 'Copied!' : 'Copy to clipboard'}
          </button>
          <button
            onClick={onClose}
            className="rounded-md border border-zinc-300 px-3 py-1.5 text-xs font-medium text-zinc-700 transition hover:border-zinc-400 dark:border-zinc-700 dark:text-zinc-300 dark:hover:border-zinc-500"
          >
            I&apos;ve saved it
          </button>
        </div>
      </div>
    </div>
  );
}
