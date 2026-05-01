// Service-role Supabase client. Bypasses RLS — only use server-side,
// only when the route handler has already authenticated the caller via
// the normal session-cookie path.
//
// Today's only consumer is the Feishu OAuth callback, which needs to
// upsert a row into feishu_links on behalf of the user. RLS doesn't
// allow that with the anon key by design (we don't want a logged-in
// user to claim an arbitrary open_id from the browser).

import { createClient } from '@supabase/supabase-js';

const URL  = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const SK   = process.env.SUPABASE_SERVICE_ROLE_KEY;

if (!URL) {
  throw new Error('Missing NEXT_PUBLIC_SUPABASE_URL.');
}

export function adminClient() {
  if (!SK) {
    throw new Error(
      'SUPABASE_SERVICE_ROLE_KEY missing. Add it to env (server-only — never NEXT_PUBLIC_).',
    );
  }
  return createClient(URL, SK, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
}
