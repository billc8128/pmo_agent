'use server';

import { createHash, randomBytes } from 'node:crypto';
import { serverActionClient } from '@/lib/supabase-server';

const LABEL_RE = /^[A-Za-z0-9 _\-.]{1,64}$/;

// authorizeCLI: mint a token and return a URL to redirect the browser
// to. The URL points at the daemon's loopback callback and carries
// the plaintext token in its query string.
//
// We re-validate the redirect URL server-side: the page-level check
// can be bypassed by a crafted client.
export async function authorizeCLI(params: {
  session: string;
  redirectURL: string;
  label: string;
}): Promise<string> {
  const { session, redirectURL, label } = params;

  if (!/^[A-Za-z0-9_-]{4,64}$/.test(session)) {
    throw new Error('invalid session');
  }
  if (label && !LABEL_RE.test(label)) {
    throw new Error('invalid label');
  }
  let parsed: URL;
  try {
    parsed = new URL(redirectURL);
  } catch {
    throw new Error('invalid redirect URL');
  }
  if (
    parsed.protocol !== 'http:' ||
    (parsed.hostname !== 'localhost' && parsed.hostname !== '127.0.0.1')
  ) {
    throw new Error('refusing to send token to a non-loopback host');
  }

  const sb = await serverActionClient();
  const {
    data: { user },
  } = await sb.auth.getUser();
  if (!user) {
    throw new Error('not signed in');
  }

  const cleanLabel = (label || 'daemon').slice(0, 64);

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

  // Build the daemon callback URL. We hand the daemon both the
  // plaintext and the session nonce so it can match up the response
  // to its in-flight request (defense against a stale browser tab
  // posting an old token).
  const out = new URL(redirectURL);
  out.searchParams.set('token', plaintext);
  out.searchParams.set('session', session);
  return out.toString();
}
