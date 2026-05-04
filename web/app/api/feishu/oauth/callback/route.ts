// Feishu OAuth callback.
//
// Steps:
//   1. Verify state cookie matches the `state` query param (CSRF guard).
//   2. Exchange the `code` for a user_access_token.
//      Endpoint: /open-apis/authen/v2/oauth/token  (the v2 endpoint;
//      v1 is being deprecated for self-built apps).
//   3. Call /open-apis/authen/v1/user_info to get open_id + name + email.
//   4. Upsert (open_id → current_user_id) into feishu_links.
//   5. Redirect back to /me with a success flag (or error string).
//
// We use the service-role Supabase client to write the link — RLS
// doesn't allow anon writes here by design.

import { NextResponse, type NextRequest } from 'next/server';
import { serverActionClient } from '@/lib/supabase-server';
import { adminClient } from '@/lib/supabase-admin';

const FEISHU_APP_ID     = process.env.FEISHU_APP_ID!;
const FEISHU_APP_SECRET = process.env.FEISHU_APP_SECRET!;

const TOKEN_URL    = 'https://open.feishu.cn/open-apis/authen/v2/oauth/token';
const USERINFO_URL = 'https://open.feishu.cn/open-apis/authen/v1/user_info';

function backToMe(origin: string, params: Record<string, string>) {
  const u = new URL(`${origin}/me`);
  for (const [k, v] of Object.entries(params)) u.searchParams.set(k, v);
  return NextResponse.redirect(u.toString());
}

export async function GET(request: NextRequest) {
  const { searchParams, origin } = new URL(request.url);
  const code  = searchParams.get('code');
  const state = searchParams.get('state');

  if (!code || !state) {
    return backToMe(origin, { feishu: 'error', reason: 'missing_code_or_state' });
  }

  const expected = request.cookies.get('feishu_oauth_state')?.value;
  if (!expected || expected !== state) {
    return backToMe(origin, { feishu: 'error', reason: 'state_mismatch' });
  }

  const sb = await serverActionClient();
  const { data: { user } } = await sb.auth.getUser();
  if (!user) {
    return backToMe(origin, { feishu: 'error', reason: 'not_signed_in' });
  }

  if (!FEISHU_APP_ID || !FEISHU_APP_SECRET) {
    return backToMe(origin, { feishu: 'error', reason: 'feishu_not_configured' });
  }

  // 1. Exchange code → user_access_token.
  let userAccessToken: string;
  try {
    const tokenResp = await fetch(TOKEN_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json; charset=utf-8' },
      body: JSON.stringify({
        grant_type: 'authorization_code',
        client_id: FEISHU_APP_ID,
        client_secret: FEISHU_APP_SECRET,
        code,
        redirect_uri: `${origin}/api/feishu/oauth/callback`,
      }),
    });
    const tokenJson = await tokenResp.json();
    // v2 endpoint shape: { code, msg?, access_token, refresh_token, ... }
    // (top-level access_token, NOT nested under .data — that was v1).
    if (!tokenResp.ok || tokenJson.code !== 0 && tokenJson.code !== undefined && !tokenJson.access_token) {
      return backToMe(origin, {
        feishu: 'error',
        reason: 'token_exchange_failed',
        detail: String(tokenJson.error_description ?? tokenJson.msg ?? tokenResp.status),
      });
    }
    userAccessToken = tokenJson.access_token;
    if (!userAccessToken) {
      return backToMe(origin, { feishu: 'error', reason: 'no_access_token' });
    }
  } catch {
    return backToMe(origin, { feishu: 'error', reason: 'token_exchange_threw' });
  }

  // 2. Fetch the Feishu user's identity.
  let openId: string;
  let name: string | null = null;
  let email: string | null = null;
  let mobile: string | null = null;
  let timezone: string | null = null;
  try {
    const userResp = await fetch(USERINFO_URL, {
      headers: { Authorization: `Bearer ${userAccessToken}` },
    });
    const userJson = await userResp.json();
    if (!userResp.ok || userJson.code !== 0) {
      return backToMe(origin, {
        feishu: 'error',
        reason: 'userinfo_failed',
        detail: String(userJson.msg ?? userResp.status),
      });
    }
    openId = userJson.data?.open_id;
    name   = userJson.data?.name ?? null;
    email  = userJson.data?.email ?? userJson.data?.enterprise_email ?? null;
    mobile = userJson.data?.mobile ?? userJson.data?.phone ?? null;
    timezone = userJson.data?.timezone ?? null;
    if (!openId) {
      return backToMe(origin, { feishu: 'error', reason: 'no_open_id' });
    }
  } catch {
    return backToMe(origin, { feishu: 'error', reason: 'userinfo_threw' });
  }

  // 3. Upsert into feishu_links. PRIMARY KEY = open_id, UNIQUE = user_id —
  //    so we both:
  //      (a) refresh the linked user if the same open_id reconnects, and
  //      (b) reject the binding if this user_id is already linked to a
  //          different open_id (DB will throw 23505 — we surface that).
  const admin = adminClient();
  const { error: upsertErr } = await admin
    .from('feishu_links')
    .upsert(
      {
        feishu_open_id: openId,
        user_id: user.id,
        feishu_name: name,
        feishu_email: email,
        feishu_mobile: mobile,
        timezone: timezone ?? 'Asia/Shanghai',
      },
      { onConflict: 'feishu_open_id' },
    );

  if (upsertErr) {
    // 23505 unique violation → user already linked to a different feishu account
    if ((upsertErr as { code?: string }).code === '23505') {
      return backToMe(origin, { feishu: 'error', reason: 'already_bound_other' });
    }
    return backToMe(origin, {
      feishu: 'error',
      reason: 'db_write_failed',
      detail: upsertErr.message.slice(0, 80),
    });
  }

  // Clear the state cookie + go home.
  const resp = backToMe(origin, { feishu: 'ok' });
  resp.cookies.set('feishu_oauth_state', '', { path: '/', maxAge: 0 });
  return resp;
}
