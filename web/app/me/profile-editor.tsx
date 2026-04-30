'use client';

import { useState, useTransition } from 'react';
import { updateProfile } from './actions';

export function ProfileEditor({
  initialHandle,
  initialDisplayName,
}: {
  initialHandle: string;
  initialDisplayName: string;
}) {
  const [handle, setHandle] = useState(initialHandle);
  const [displayName, setDisplayName] = useState(initialDisplayName);
  const [error, setError] = useState<string | null>(null);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [pending, startTransition] = useTransition();

  const dirty = handle !== initialHandle || displayName !== initialDisplayName;

  function onSubmit(formData: FormData) {
    setError(null);
    startTransition(async () => {
      try {
        await updateProfile(formData);
        setSavedAt(Date.now());
      } catch (e) {
        setError((e as Error).message);
      }
    });
  }

  return (
    <form action={onSubmit} className="flex flex-col gap-3">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <label className="flex flex-col gap-1">
          <span className="text-xs font-medium text-zinc-700 dark:text-zinc-300">
            Handle
          </span>
          <input
            name="handle"
            value={handle}
            onChange={(e) => setHandle(e.target.value.toLowerCase())}
            required
            minLength={2}
            maxLength={32}
            pattern="[a-z0-9_-]+"
            className="rounded border border-zinc-300 bg-white px-2 py-1.5 font-mono text-sm dark:border-zinc-700 dark:bg-zinc-900"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="text-xs font-medium text-zinc-700 dark:text-zinc-300">
            Display name
          </span>
          <input
            name="display_name"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
            maxLength={80}
            className="rounded border border-zinc-300 bg-white px-2 py-1.5 text-sm dark:border-zinc-700 dark:bg-zinc-900"
          />
        </label>
      </div>

      {error && (
        <p className="rounded bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950 dark:text-red-300">
          {error}
        </p>
      )}

      <div className="flex items-center gap-3">
        <button
          type="submit"
          disabled={pending || !dirty}
          className="rounded-md bg-zinc-900 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-zinc-800 disabled:opacity-50 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-200"
        >
          {pending ? 'Saving…' : 'Save changes'}
        </button>
        {savedAt && !dirty && (
          <span className="text-xs text-emerald-600 dark:text-emerald-400">Saved.</span>
        )}
      </div>
    </form>
  );
}
