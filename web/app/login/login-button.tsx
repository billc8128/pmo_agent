'use client';

import { authedBrowserClient } from '@/lib/supabase-browser';

export function LoginButton() {
  async function signIn() {
    const sb = authedBrowserClient();
    const { error } = await sb.auth.signInWithOAuth({
      provider: 'google',
      options: {
        redirectTo: `${window.location.origin}/auth/callback`,
      },
    });
    if (error) {
      alert(`Sign-in failed: ${error.message}`);
    }
    // On success, Supabase has already redirected the browser.
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
