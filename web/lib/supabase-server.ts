// Server-side Supabase clients that read/write the session cookie.
//
// Two factories:
//
//   serverComponentClient()  — for Server Components and route handlers
//                              that DO NOT need to mutate cookies.
//                              Reads the auth cookie from the incoming
//                              request.
//
//   serverActionClient()     — for Server Actions and route handlers
//                              that DO need to set/clear cookies (the
//                              OAuth callback, sign-out, etc).
//
// Both honor RLS just like the anon-key client; the difference is they
// know how to find the *current user's* JWT in cookies, so RLS
// policies that reference auth.uid() work end-to-end.

import { createServerClient, type CookieOptions } from '@supabase/ssr';
import { cookies } from 'next/headers';

const URL  = process.env.NEXT_PUBLIC_SUPABASE_URL!;
const ANON = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!;

if (!URL || !ANON) {
  throw new Error(
    'Missing NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY.'
  );
}

// Server Component variant: cookies are read-only at this layer
// (Next 16 doesn't let SC set cookies — you'd need a Route Handler
// or Server Action). Trying to set anyway is a no-op + warning.
export async function serverComponentClient() {
  const cookieStore = await cookies();
  return createServerClient(URL, ANON, {
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      setAll(_cookiesToSet) {
        // Server Components can't set cookies. supabase-js will try
        // to refresh the access token on read; we accept that the
        // refresh isn't persisted here. Server Actions / Route
        // Handlers handle that path.
      },
    },
  });
}

// Server Action / Route Handler variant: full cookie read/write.
export async function serverActionClient() {
  const cookieStore = await cookies();
  return createServerClient(URL, ANON, {
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      setAll(cookiesToSet) {
        for (const { name, value, options } of cookiesToSet) {
          cookieStore.set(name, value, options as CookieOptions);
        }
      },
    },
  });
}
