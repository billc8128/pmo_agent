'use server';

import { revalidatePath } from 'next/cache';
import { adminClient } from '@/lib/supabase-admin';
import { serverActionClient } from '@/lib/supabase-server';
import { validateRuleDescription } from '@/lib/notification-rules';

const RULES_PATH = '/notifications/rules';

export async function createNotificationRule(formData: FormData) {
  const description = validateRuleDescription(
    String(formData.get('description') ?? ''),
  );
  const user = await requireUser();

  const { error } = await adminClient().from('subscriptions').insert({
    scope_kind: 'user',
    scope_id: user.id,
    description,
    enabled: true,
    created_by: user.id,
    chat_id: null,
    archived_at: null,
  });
  if (error) {
    throw new Error(`failed to create rule: ${error.message}`);
  }

  revalidateRules();
}

export async function updateNotificationRule(formData: FormData) {
  const id = String(formData.get('id') ?? '').trim();
  const description = validateRuleDescription(
    String(formData.get('description') ?? ''),
  );
  const user = await requireUser();

  const { data, error } = await adminClient()
    .from('subscriptions')
    .update({
      description,
      updated_at: new Date().toISOString(),
    })
    .eq('id', id)
    .eq('scope_kind', 'user')
    .eq('scope_id', user.id)
    .is('archived_at', null)
    .select('id')
    .maybeSingle();

  if (error) {
    throw new Error(`failed to update rule: ${error.message}`);
  }
  if (!data) {
    throw new Error('rule not found or not owned by you');
  }

  revalidateRules();
}

export async function setNotificationRuleEnabled(id: string, enabled: boolean) {
  const user = await requireUser();
  const { data, error } = await adminClient()
    .from('subscriptions')
    .update({
      enabled,
      updated_at: new Date().toISOString(),
    })
    .eq('id', id)
    .eq('scope_kind', 'user')
    .eq('scope_id', user.id)
    .is('archived_at', null)
    .select('id')
    .maybeSingle();

  if (error) {
    throw new Error(`failed to update rule: ${error.message}`);
  }
  if (!data) {
    throw new Error('rule not found or not owned by you');
  }

  revalidateRules();
}

export async function archiveNotificationRule(id: string) {
  const user = await requireUser();
  const now = new Date().toISOString();
  const { data, error } = await adminClient()
    .from('subscriptions')
    .update({
      enabled: false,
      archived_at: now,
      updated_at: now,
    })
    .eq('id', id)
    .eq('scope_kind', 'user')
    .eq('scope_id', user.id)
    .is('archived_at', null)
    .select('id')
    .maybeSingle();

  if (error) {
    throw new Error(`failed to archive rule: ${error.message}`);
  }
  if (!data) {
    throw new Error('rule not found or not owned by you');
  }

  revalidateRules();
}

async function requireUser() {
  const sb = await serverActionClient();
  const {
    data: { user },
  } = await sb.auth.getUser();
  if (!user) {
    throw new Error('not signed in');
  }
  return user;
}

function revalidateRules() {
  revalidatePath(RULES_PATH);
  revalidatePath('/me');
}
