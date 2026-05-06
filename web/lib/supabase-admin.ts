// Service-role Supabase client. Bypasses RLS — only use server-side, after
// the route/page/action has authenticated the caller or has otherwise gated
// what public data may be returned.
//
// Consumers include Feishu OAuth, integration management, and public-safe
// server-rendered views that explicitly filter service-role rows before they
// reach the browser.

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
