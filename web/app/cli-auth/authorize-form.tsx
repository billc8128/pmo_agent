'use client';

// AuthorizeForm: one button. On click, calls the server action which
// mints a token and returns a redirect URL containing the plaintext.
// We use window.location.replace so the URL with the token doesn't
// live in browser history.

import { useState, useTransition } from 'react';
import { authorizeCLI } from './actions';

export function AuthorizeForm({
  session,
  redirectURL,
  label,
}: {
  session: string;
  redirectURL: string;
  label: string;
}) {
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  function onAuthorize() {
    setError(null);
    startTransition(async () => {
      try {
        const dest = await authorizeCLI({ session, redirectURL, label });
        // Use replace so the URL-with-token is not a back-button entry.
        window.location.replace(dest);
      } catch (e) {
        setError((e as Error).message);
      }
    });
  }

  return (
    <div className="flex flex-col gap-3">
      <button
        onClick={onAuthorize}
        disabled={pending}
        className="rounded-md bg-indigo-600 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-indigo-500 disabled:opacity-60"
      >
        {pending ? 'Authorizing…' : 'Authorize'}
      </button>
      {error && (
        <p className="rounded bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950 dark:text-red-300">
          {error}
        </p>
      )}
    </div>
  );
}
