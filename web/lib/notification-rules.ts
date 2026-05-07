export const MAX_RULE_DESCRIPTION_LENGTH = 240;

export type SubscriptionRuleRow = {
  id: string;
  scope_kind: string;
  scope_id: string | null;
  description: string | null;
  enabled: boolean | null;
  created_at: string;
  updated_at: string | null;
  archived_at?: string | null;
  created_by?: string | null;
  target_kind?: string | null;
  target_id?: string | null;
  target_user_open_id?: string | null;
  consent_anchor?: string | null;
  profiles?: RuleOwnerProfile | RuleOwnerProfile[] | null;
};

export type RuleOwnerProfile = {
  handle: string | null;
  display_name: string | null;
};

export type PublicNotificationRule = {
  viewKey: string;
  subscriptionId: string | null;
  description: string;
  enabled: boolean;
  createdAt: string;
  updatedAt: string | null;
  ownerHandle: string | null;
  ownerDisplayName: string | null;
  ownedByViewer: boolean;
  targetKind: string;
  targetLabel: string;
};

export function validateRuleDescription(value: string): string {
  const description = value.trim();
  if (!description) {
    throw new Error('rule description is empty');
  }
  if (description.length > MAX_RULE_DESCRIPTION_LENGTH) {
    throw new Error(
      `rule description must be ${MAX_RULE_DESCRIPTION_LENGTH} characters or fewer`,
    );
  }
  return description;
}

export function toPublicNotificationRule(
  row: SubscriptionRuleRow,
  viewerUserId: string | null,
): PublicNotificationRule | null {
  if (row.archived_at) return null;

  const owner = normalizeProfile(row.profiles);
  const description = validateRuleDescription(row.description ?? '');
  const ownedByViewer = Boolean(viewerUserId && row.created_by === viewerUserId);
  return {
    viewKey: `${row.created_at}:${owner?.handle ?? 'unknown'}:${description.slice(0, 40)}`,
    subscriptionId: ownedByViewer ? row.id : null,
    description,
    enabled: row.enabled === true,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    ownerHandle: owner?.handle ?? null,
    ownerDisplayName: owner?.display_name ?? null,
    ownedByViewer,
    targetKind: row.target_kind ?? 'user_dm',
    targetLabel: targetLabel(row, ownedByViewer),
  };
}

function targetLabel(row: SubscriptionRuleRow, ownedByViewer: boolean): string {
  if (row.target_kind === 'chat') return 'Feishu chat';
  if (row.target_kind === 'mention_in_chat') return 'Feishu chat @mention';
  if (row.target_kind === 'user_dm' && ownedByViewer) return 'Your DM';
  if (row.target_kind === 'user_dm' && row.scope_kind === 'user' && row.target_id === row.scope_id) {
    return 'Owner DM';
  }
  return 'User DM';
}

function normalizeProfile(
  profile: RuleOwnerProfile | RuleOwnerProfile[] | null | undefined,
): RuleOwnerProfile | null {
  if (Array.isArray(profile)) return profile[0] ?? null;
  return profile ?? null;
}
