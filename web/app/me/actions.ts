'use server';

// Server actions for /me. All of these run with the user's session
// (RLS enforces ownership).

import { revalidatePath } from 'next/cache';
import { redirect } from 'next/navigation';
import { createHash, randomBytes } from 'node:crypto';
import { serverActionClient } from '@/lib/supabase-server';

const HANDLE_RE = /^[a-z0-9_-]{2,32}$/;

// createProfile: called from the onboarding form on first /me visit.
export async function createProfile(formData: FormData) {
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
  redirect('/me');
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

// createToken: mints a fresh PAT and returns the plaintext ONCE.
//
// Returns { plaintext, label }. The caller is responsible for showing
// the plaintext to the user and never persisting it. The DB only ever
// sees the SHA-256 hash, mirroring the daemon's expectations.
export async function createToken(label: string): Promise<{
  plaintext: string;
  label: string;
}> {
  const cleanLabel = label.trim().slice(0, 64) || 'daemon';

  const sb = await serverActionClient();
  const {
    data: { user },
  } = await sb.auth.getUser();
  if (!user) throw new Error('not signed in');

  // 24 random bytes → 32 base64url chars. Prefixed with "pmo_" for
  // grep-ability and so secret scanners (GitHub, TruffleHog) can
  // identify accidental leaks.
  const rawBytes = randomBytes(24);
  const tail = rawBytes
    .toString('base64')
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
  const plaintext = `pmo_${tail}`;
  const tokenHash = createHash('sha256').update(plaintext).digest('hex');

  const { error } = await sb.from('tokens').insert({
    user_id: user.id,
    token_hash: tokenHash,
    label: cleanLabel,
  });
  if (error) {
    throw new Error(`failed to mint token: ${error.message}`);
  }
  revalidatePath('/me');
  return { plaintext, label: cleanLabel };
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
