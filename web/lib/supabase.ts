// Supabase client factories.
//
// The anon-key client is safe to expose to the browser: RLS policies
// in 0001_initial.sql enforce that only `select` is open to anonymous
// users. Writes happen elsewhere (daemon → ingest Edge Function).
//
// We export TWO factories rather than a global singleton:
//
//   browserClient()  — for client components (the singleton lives in
//                      window-bound module state; safe to call many
//                      times)
//   serverClient()   — for server components / route handlers (a fresh
//                      client per request, no auth state shared
//                      across requests)
//
// This split is a Next.js App Router convention: server components
// must NOT share auth state, and the supabase-js library was not
// designed with that constraint in mind.

import { createClient, type SupabaseClient } from '@supabase/supabase-js';

const URL  = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const ANON = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;

if (!URL || !ANON) {
  // Fail loudly in dev/build. Without these we would silently render
  // empty timelines, which is a confusing failure mode.
  throw new Error(
    'Missing NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY. ' +
    'Set them in web/.env.local (development) or Vercel project env (production).'
  );
}

let _browser: SupabaseClient | null = null;

export function browserClient(): SupabaseClient {
  if (_browser) return _browser;
  _browser = createClient(URL, ANON, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  return _browser;
}

export function serverClient(): SupabaseClient {
  // Always a fresh instance — no shared state across requests.
  return createClient(URL, ANON, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
}
