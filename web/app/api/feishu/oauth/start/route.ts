// Kicks off the Feishu OAuth flow.
//
// Flow:
//   1. User must already be signed in (cookie-based Supabase session).
//      We need to know which pmo_agent account to bind the Feishu
//      identity to.
//   2. Generate a single-use `state` value (CSRF + carries the user_id),
//      store its hash in a short-lived cookie.
//   3. Redirect to Feishu's authorize page.
//
// On callback, we verify the state cookie and finish the bind.

import { NextResponse, type NextRequest } from 'next/server';
import { serverActionClient } from '@/lib/supabase-server';
import { buildFeishuAuthorizeUrl } from '@/lib/feishu-oauth';

const FEISHU_APP_ID = process.env.FEISHU_APP_ID!;

export async function GET(request: NextRequest) {
  if (!FEISHU_APP_ID) {
    return NextResponse.json({ error: 'FEISHU_APP_ID not configured' }, { status: 500 });
  }

  const sb = await serverActionClient();
  const { data: { user } } = await sb.auth.getUser();
  if (!user) {
    // Bounce through login so the callback ends up with a real session.
    const url = new URL(request.url);
    const next = `/api/feishu/oauth/start`;
    return NextResponse.redirect(
      `${url.origin}/login?next=${encodeURIComponent(next)}`,
    );
  }

  const { origin } = new URL(request.url);
  const redirectUri = `${origin}/api/feishu/oauth/callback`;

  // Random state — Feishu echoes it back. We also store it in an
  // httpOnly cookie so the callback can verify the request started
  // from this same browser session.
  const state = crypto.randomUUID();

  const resp = NextResponse.redirect(buildFeishuAuthorizeUrl({
    appId: FEISHU_APP_ID,
    redirectUri,
    state,
  }));
  resp.cookies.set('feishu_oauth_state', state, {
    httpOnly: true,
    sameSite: 'lax',
    secure: origin.startsWith('https://'),
    path: '/',
    maxAge: 600,        // 10 minutes
  });
  return resp;
}
