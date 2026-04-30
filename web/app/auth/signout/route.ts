// POST /auth/signout — clears the session cookie and redirects home.

import { NextResponse, type NextRequest } from 'next/server';
import { serverActionClient } from '@/lib/supabase-server';

export async function POST(request: NextRequest) {
  const supabase = await serverActionClient();
  await supabase.auth.signOut();
  return NextResponse.redirect(new URL('/', request.url), { status: 303 });
}
