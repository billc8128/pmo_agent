// OAuth callback. Supabase redirects here after the user finishes the
// Google flow with a `code` query param. We exchange it for a session
// (Supabase sets the auth cookies for us via @supabase/ssr).
//
// On success: redirect to /me. The /me page handles handle-picker
// onboarding for first-time users.

import { NextResponse, type NextRequest } from 'next/server';
import { serverActionClient } from '@/lib/supabase-server';

export async function GET(request: NextRequest) {
  const { searchParams, origin } = new URL(request.url);
  const code = searchParams.get('code');
  const next = searchParams.get('next') ?? '/me';

  if (!code) {
    return NextResponse.redirect(`${origin}/?error=missing_code`);
  }

  const supabase = await serverActionClient();
  const { error } = await supabase.auth.exchangeCodeForSession(code);
  if (error) {
    return NextResponse.redirect(
      `${origin}/?error=${encodeURIComponent(error.message)}`,
    );
  }

  return NextResponse.redirect(`${origin}${next}`);
}
