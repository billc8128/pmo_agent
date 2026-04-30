// Browser-side Supabase client with cookie-based auth.
//
// This replaces the anon-only browserClient() in lib/supabase.ts for
// any client component that needs to know "who is logged in". The
// session is read from cookies set during the OAuth callback flow.

'use client';

import { createBrowserClient } from '@supabase/ssr';

const URL  = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const ANON = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;

let _client: ReturnType<typeof createBrowserClient> | null = null;

export function authedBrowserClient() {
  if (_client) return _client;
  _client = createBrowserClient(URL, ANON);
  return _client;
}
