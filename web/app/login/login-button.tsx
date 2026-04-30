'use client';

import { authedBrowserClient } from '@/lib/supabase-browser';

export function LoginButton({ next }: { next: string }) {
  async function signIn() {
    const sb = authedBrowserClient();
    // Pass `next` through the OAuth round-trip via a query string on
    // /auth/callback. The callback route then redirects there.
    const callback = new URL('/auth/callback', window.location.origin);
    callback.searchParams.set('next', next);
    const { error } = await sb.auth.signInWithOAuth({
      provider: 'google',
      options: {
        redirectTo: callback.toString(),
      },
    });
    if (error) {
      alert(`Sign-in failed: ${error.message}`);
    }
  }

  return (
    <button
      onClick={signIn}
      className="rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white shadow-sm transition hover:bg-zinc-800 dark:bg-zinc-100 dark:text-zinc-900 dark:hover:bg-zinc-200"
    >
      Continue with Google
    </button>
  );
}
