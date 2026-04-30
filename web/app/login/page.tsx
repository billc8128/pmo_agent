// Sign-in page. Uses Google OAuth (configured in Supabase Auth).

import { LoginButton } from './login-button';

export default function LoginPage() {
  return (
    <main className="mx-auto flex max-w-md flex-col items-start gap-6 px-4 py-16 sm:py-24">
      <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
        Sign in
      </h1>
      <p className="text-sm leading-relaxed text-zinc-600 dark:text-zinc-400">
        pmo_agent uses your Google account for identity. We never see
        your password — just enough to know who you are so the daemon
        on your machine can post under your handle.
      </p>
      <LoginButton />
    </main>
  );
}
