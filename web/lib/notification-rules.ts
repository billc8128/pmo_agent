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
  if (row.scope_kind !== 'user') return null;
  if (row.archived_at) return null;

  const owner = normalizeProfile(row.profiles);
  const description = validateRuleDescription(row.description ?? '');
  const ownedByViewer = Boolean(viewerUserId && row.scope_id === viewerUserId);
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
  };
}

function normalizeProfile(
  profile: RuleOwnerProfile | RuleOwnerProfile[] | null | undefined,
): RuleOwnerProfile | null {
  if (Array.isArray(profile)) return profile[0] ?? null;
  return profile ?? null;
}
