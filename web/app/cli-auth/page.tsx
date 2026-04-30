// /cli-auth — the daemon's pmo-agent login command opens a browser
// here. We authenticate the user (via Google OAuth if needed),
// confirm the session nonce against what the terminal printed, and
// — on click — mint a token and redirect the browser to the daemon's
// loopback callback URL with the plaintext token in the query.
//
// Security gates:
//   1. The `redirect` parameter MUST point to localhost / 127.0.0.1.
//      We refuse anything else. Otherwise a malicious link could
//      send a freshly-minted PAT to an attacker-controlled URL.
//   2. The `session` parameter is shown verbatim in both the
//      terminal and the browser; the user is asked to verify they
//      match. This is the same pattern as `gh auth login --web`.
//   3. The mint happens in a Server Action, so the plaintext leaves
//      the server only as a 302 Location header to the trusted
//      loopback URL — it never lives in the page DOM.

import { redirect } from 'next/navigation';
import { serverComponentClient } from '@/lib/supabase-server';
import { AuthorizeForm } from './authorize-form';

export const dynamic = 'force-dynamic';

export default async function CliAuthPage(props: PageProps<'/cli-auth'>) {
  const sp = await props.searchParams;
  const session = typeof sp.session === 'string' ? sp.session : '';
  const redirectURL =
    typeof sp.redirect === 'string' ? sp.redirect : '';
  const label = typeof sp.label === 'string' ? sp.label : 'daemon';

  // Sanity-check the inputs. We keep these errors in-page rather
  // than throwing so a confused user lands on a readable screen.
  const inputErr = validateInputs(session, redirectURL);
  if (inputErr) {
    return <ErrorScreen title="Bad CLI link" message={inputErr} />;
  }

  // Require a session, otherwise bounce through Google. We preserve
  // the full URL so we come back to this page with the same query.
  const sb = await serverComponentClient();
  const {
    data: { user },
  } = await sb.auth.getUser();

  if (!user) {
    const here = `/cli-auth?session=${encodeURIComponent(session)}&redirect=${encodeURIComponent(redirectURL)}&label=${encodeURIComponent(label)}`;
    redirect(`/login?next=${encodeURIComponent(here)}`);
  }

  // Require a profile (so a freshly signed-up user finishes
  // onboarding before binding a daemon).
  const { data: profile } = await sb
    .from('profiles')
    .select('id, handle, display_name')
    .eq('id', user.id)
    .maybeSingle();
  if (!profile) {
    const here = `/cli-auth?session=${encodeURIComponent(session)}&redirect=${encodeURIComponent(redirectURL)}&label=${encodeURIComponent(label)}`;
    redirect(`/me?next=${encodeURIComponent(here)}`);
  }

  return (
    <main className="mx-auto max-w-md px-4 py-12">
      <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-100">
        Authorize this CLI
      </h1>
      <p className="mt-2 text-sm leading-relaxed text-zinc-600 dark:text-zinc-300">
        Your terminal is asking to bind a new daemon to{' '}
        <strong>@{profile.handle}</strong>. Make sure the code below
        matches the one printed in your terminal, then click Authorize.
      </p>

      <div className="mt-6 rounded-lg border border-zinc-200 bg-zinc-50 p-4 text-center dark:border-zinc-800 dark:bg-zinc-900">
        <div className="text-[11px] uppercase tracking-wider text-zinc-500 dark:text-zinc-400">
          Session code
        </div>
        <div className="mt-1 font-mono text-2xl font-semibold tracking-widest text-zinc-900 dark:text-zinc-100">
          {session}
        </div>
      </div>

      <div className="mt-2 grid grid-cols-2 gap-2 text-xs text-zinc-500 dark:text-zinc-400">
        <div>
          <div className="font-medium text-zinc-700 dark:text-zinc-300">Label</div>
          <div className="font-mono">{label}</div>
        </div>
        <div>
          <div className="font-medium text-zinc-700 dark:text-zinc-300">Returns to</div>
          <div className="break-all font-mono">{redirectURL}</div>
        </div>
      </div>

      <div className="mt-6">
        <AuthorizeForm
          session={session}
          redirectURL={redirectURL}
          label={label}
        />
      </div>

      <p className="mt-6 text-xs text-zinc-400 dark:text-zinc-500">
        Don&apos;t recognize this request? Just close this tab — nothing
        is created until you click Authorize.
      </p>
    </main>
  );
}

function ErrorScreen({ title, message }: { title: string; message: string }) {
  return (
    <main className="mx-auto max-w-md px-4 py-16">
      <h1 className="text-xl font-semibold text-red-700 dark:text-red-300">
        {title}
      </h1>
      <p className="mt-2 text-sm text-zinc-600 dark:text-zinc-400">{message}</p>
    </main>
  );
}

// validateInputs returns an error message string, or "" if OK.
//
// `redirect` must be http://localhost:<port>/<path> or
// http://127.0.0.1:<port>/<path>. Any other host is refused — that
// includes other private IPs, file://, https://, etc. — to keep
// freshly-minted plaintext tokens from being shipped offsite.
//
// `session` must be 4–64 url-safe characters; we don't enforce
// uniqueness server-side because the daemon generates its own nonce
// and only it cares about the value.
function validateInputs(session: string, redirectURL: string): string {
  if (!session) return 'Missing "session" parameter.';
  if (!/^[A-Za-z0-9_-]{4,64}$/.test(session)) {
    return 'Invalid "session" parameter.';
  }
  if (!redirectURL) return 'Missing "redirect" parameter.';
  let parsed: URL;
  try {
    parsed = new URL(redirectURL);
  } catch {
    return 'Invalid "redirect" URL.';
  }
  if (parsed.protocol !== 'http:') {
    return 'Refusing to send a token over a non-loopback URL.';
  }
  if (parsed.hostname !== 'localhost' && parsed.hostname !== '127.0.0.1') {
    return 'Refusing to send a token to a non-loopback host.';
  }
  return '';
}
