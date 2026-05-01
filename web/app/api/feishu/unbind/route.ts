// POST /api/feishu/unbind — delete the current user's feishu_links row.
// RLS allows owners to delete their own row, so we use the regular
// session-cookie client (no service role needed).

import { NextResponse } from 'next/server';
import { serverActionClient } from '@/lib/supabase-server';

export async function POST() {
  const sb = await serverActionClient();
  const { data: { user } } = await sb.auth.getUser();
  if (!user) {
    return NextResponse.json({ error: 'not signed in' }, { status: 401 });
  }
  const { error } = await sb
    .from('feishu_links')
    .delete()
    .eq('user_id', user.id);
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  return NextResponse.json({ ok: true });
}
