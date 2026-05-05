// SiteHeader is the single nav bar that appears on every page.
//
// It is an async Server Component that consults the session cookie:
//   - Anonymous: shows "Discover" + "Sign in".
//   - Authenticated: shows "Discover" + the user's @handle (linking
//     to /me) + a "Sign out" form.
//
// We tolerate the per-page session lookup because the header is the
// only place the session-aware difference matters; the rest of the
// pages remain RLS-driven.

import Link from 'next/link';
import { serverComponentClient } from '@/lib/supabase-server';

export async function SiteHeader() {
  const sb = await serverComponentClient();
  const {
    data: { user },
  } = await sb.auth.getUser();

  let handle: string | null = null;
  if (user) {
    const { data } = await sb
      .from('profiles')
      .select('handle')
      .eq('id', user.id)
      .maybeSingle();
    handle = data?.handle ?? null;
  }

  return (
    <header className="border-b border-zinc-200 bg-white/80 backdrop-blur dark:border-zinc-800 dark:bg-zinc-950/80">
      <div className="mx-auto flex max-w-3xl items-center justify-between px-4 py-3">
        <Link
          href="/"
          className="font-mono text-sm font-semibold tracking-tight text-zinc-900 hover:text-indigo-600 dark:text-zinc-100 dark:hover:text-indigo-400"
        >
          pmo_agent
        </Link>
        <nav className="flex items-center gap-1 text-xs">
          <NavLink href="/discover">Discover</NavLink>
          <NavLink href="/notifications/rules">Rules</NavLink>
          {user ? (
            <>
              <NavLink href="/me">
                {handle ? `@${handle}` : 'My account'}
              </NavLink>
              <form action="/auth/signout" method="post">
                <button
                  type="submit"
                  className="rounded px-2 py-1 text-zinc-600 transition hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
                >
                  Sign out
                </button>
              </form>
            </>
          ) : (
            <NavLink href="/login">Sign in</NavLink>
          )}
        </nav>
      </div>
    </header>
  );
}

function NavLink({ href, children }: { href: string; children: React.ReactNode }) {
  return (
    <Link
      href={href}
      className="rounded px-2 py-1 text-zinc-600 transition hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
      prefetch={false}
    >
      {children}
    </Link>
  );
}
