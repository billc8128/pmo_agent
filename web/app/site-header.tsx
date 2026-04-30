// SiteHeader is the single nav bar that appears on every page. We
// keep it as a Server Component so anonymous visitors get full SSR
// without JS hydration overhead. It does NOT show a "you're logged
// in" indicator — that would force every public page to depend on a
// session lookup, which is the wrong tradeoff for a public-by-default
// app. /me is the place where session state matters.

import Link from 'next/link';

export function SiteHeader() {
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
          <NavLink href="/login">Sign in</NavLink>
          <NavLink href="/me">My account</NavLink>
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
