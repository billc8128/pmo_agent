import { adminClient } from '@/lib/supabase-admin';
import { serverComponentClient } from '@/lib/supabase-server';
import {
  type SubscriptionRuleRow,
  toPublicNotificationRule,
} from '@/lib/notification-rules';
import { RulesPanel } from './rules-panel';

export const dynamic = 'force-dynamic';

export default async function NotificationRulesPage() {
  const sb = await serverComponentClient();
  const {
    data: { user },
  } = await sb.auth.getUser();

  const { data, error } = await adminClient()
    .from('subscriptions')
    .select(
      'id, scope_kind, scope_id, description, enabled, created_at, updated_at, archived_at, profiles:created_by(handle, display_name)',
    )
    .eq('scope_kind', 'user')
    .is('archived_at', null)
    .order('enabled', { ascending: false })
    .order('created_at', { ascending: false })
    .limit(500);

  if (error) {
    throw new Error(`failed to load notification rules: ${error.message}`);
  }

  const rules = ((data ?? []) as SubscriptionRuleRow[])
    .map((row) => toPublicNotificationRule(row, user?.id ?? null))
    .filter((rule) => rule != null);

  const publicRules = rules.filter((rule) => rule.enabled);
  const ownRules = rules.filter((rule) => rule.ownedByViewer);

  return (
    <main className="mx-auto max-w-4xl px-4 py-8 sm:py-12">
      <RulesPanel
        publicRules={publicRules}
        ownRules={ownRules}
        signedIn={Boolean(user)}
        loginHref={`/login?next=${encodeURIComponent('/notifications/rules')}`}
      />
    </main>
  );
}
