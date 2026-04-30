'use server';

// Server actions for /me. All of these run with the user's session
// (RLS enforces ownership).
//
// PAT minting lives in app/cli-auth/actions.ts because the only way
// to create a token is via the daemon's pmo-agent login flow.

import { revalidatePath } from 'next/cache';
import { redirect } from 'next/navigation';
import { serverActionClient } from '@/lib/supabase-server';

const HANDLE_RE = /^[a-z0-9_-]{2,32}$/;

// createProfile: called from the onboarding form on first /me visit.
// If the caller passed `next`, redirect there after success (used by
// the /cli-auth flow when a fresh signup needs to finish onboarding
// before binding a daemon).
export async function createProfile(formData: FormData) {
  const handle = String(formData.get('handle') ?? '').trim().toLowerCase();
  const displayName = String(formData.get('display_name') ?? '').trim();
  const next = String(formData.get('next') ?? '').trim();

  if (!HANDLE_RE.test(handle)) {
    throw new Error(
      'handle must be 2–32 chars, lowercase letters / digits / "-" / "_"',
    );
  }

  const sb = await serverActionClient();
  const {
    data: { user },
  } = await sb.auth.getUser();
  if (!user) throw new Error('not signed in');

  const { error } = await sb.from('profiles').insert({
    id: user.id,
    handle,
    display_name: displayName || null,
  });
  if (error) {
    if (error.code === '23505') {
      throw new Error(`handle "${handle}" is already taken`);
    }
    throw new Error(`failed to create profile: ${error.message}`);
  }
  redirect(next && next.startsWith('/') ? next : '/me');
}

// updateProfile: edit handle / display_name from the /me dashboard.
export async function updateProfile(formData: FormData) {
  const handle = String(formData.get('handle') ?? '').trim().toLowerCase();
  const displayName = String(formData.get('display_name') ?? '').trim();

  if (!HANDLE_RE.test(handle)) {
    throw new Error(
      'handle must be 2–32 chars, lowercase letters / digits / "-" / "_"',
    );
  }

  const sb = await serverActionClient();
  const {
    data: { user },
  } = await sb.auth.getUser();
  if (!user) throw new Error('not signed in');

  const { error } = await sb
    .from('profiles')
    .update({ handle, display_name: displayName || null })
    .eq('id', user.id);
  if (error) {
    if (error.code === '23505') {
      throw new Error(`handle "${handle}" is already taken`);
    }
    throw new Error(`failed to update profile: ${error.message}`);
  }
  revalidatePath('/me');
}

// revokeToken: soft-revoke by stamping revoked_at. Active daemon
// requests will start failing with 401 immediately because the
// ingest Edge Function checks revoked_at on every request.
export async function revokeToken(tokenId: string) {
  const sb = await serverActionClient();
  const {
    data: { user },
  } = await sb.auth.getUser();
  if (!user) throw new Error('not signed in');

  const { error } = await sb
    .from('tokens')
    .update({ revoked_at: new Date().toISOString() })
    .eq('id', tokenId)
    .eq('user_id', user.id) // belt-and-suspenders; RLS already enforces this
    .is('revoked_at', null);

  if (error) {
    throw new Error(`failed to revoke: ${error.message}`);
  }
  revalidatePath('/me');
}
