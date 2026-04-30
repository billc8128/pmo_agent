// Sign-in page. Uses Google OAuth (configured in Supabase Auth).
//
// Honors a ?next= query parameter so the OAuth callback can bring the
// user back to the page that sent them here (e.g. /cli-auth flow).

import { LoginButton } from './login-button';

export default async function LoginPage(props: PageProps<'/login'>) {
  const sp = await props.searchParams;
  const next = typeof sp.next === 'string' ? sp.next : '/me';

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
      <LoginButton next={next} />
    </main>
  );
}
