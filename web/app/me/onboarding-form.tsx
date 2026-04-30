'use client';

import { useState, useTransition } from 'react';
import { createProfile } from './actions';

export function OnboardingForm({
  userId,
  email,
  suggestedHandle,
}: {
  userId: string;
  email: string | null;
  suggestedHandle: string;
}) {
  const [handle, setHandle] = useState(suggestedHandle);
  const [displayName, setDisplayName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [pending, startTransition] = useTransition();

  function onSubmit(formData: FormData) {
    setError(null);
    startTransition(async () => {
      try {
        await createProfile(formData);
      } catch (e) {
        setError((e as Error).message);
      }
    });
  }

  return (
    <form action={onSubmit} className="flex flex-col gap-4">
      <input type="hidden" name="user_id" value={userId} />

      <label className="flex flex-col gap-1">
        <span className="text-xs font-medium text-zinc-700 dark:text-zinc-300">
          Handle
        </span>
        <div className="flex items-stretch overflow-hidden rounded border border-zinc-300 focus-within:border-indigo-500 dark:border-zinc-700">
          <span className="flex items-center bg-zinc-50 px-2 text-sm text-zinc-500 dark:bg-zinc-800 dark:text-zinc-400">
            /u/
          </span>
          <input
            name="handle"
            value={handle}
            onChange={(e) => setHandle(e.target.value)}
            required
            minLength={2}
            maxLength={32}
            pattern="[a-z0-9_-]+"
            className="flex-1 bg-white px-2 py-1.5 font-mono text-sm text-zinc-900 outline-none dark:bg-zinc-900 dark:text-zinc-100"
            autoComplete="off"
          />
        </div>
        <span className="text-[11px] text-zinc-500 dark:text-zinc-400">
          2–32 chars: lowercase letters, digits, &quot;-&quot;, &quot;_&quot;
        </span>
      </label>

      <label className="flex flex-col gap-1">
        <span className="text-xs font-medium text-zinc-700 dark:text-zinc-300">
          Display name <span className="text-zinc-400">(optional)</span>
        </span>
        <input
          name="display_name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          maxLength={80}
          placeholder={email ?? 'Your name'}
          className="rounded border border-zinc-300 bg-white px-2 py-1.5 text-sm text-zinc-900 outline-none focus:border-indigo-500 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-100"
        />
      </label>

      {error && (
        <p className="rounded bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950 dark:text-red-300">
          {error}
        </p>
      )}

      <div>
        <button
          type="submit"
          disabled={pending}
          className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-zinc-800 disabled:opacity-60 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-200"
        >
          {pending ? 'Creating…' : 'Create profile'}
        </button>
      </div>
    </form>
  );
}
