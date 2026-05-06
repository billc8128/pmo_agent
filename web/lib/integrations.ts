export const EXTERNAL_PROVIDERS = ['github', 'gitea'] as const;

export type ExternalProvider = (typeof EXTERNAL_PROVIDERS)[number];

export type ExternalRepoRow = {
  id: string;
  provider: string | null;
  repo_full_name: string | null;
  project_root: string | null;
  created_by: string | null;
  created_at: string;
  updated_at: string | null;
  profiles?: IntegrationCreatorProfile | IntegrationCreatorProfile[] | null;
};

export type ExternalDeliveryRow = {
  provider: string | null;
  delivery_id: string | null;
  event_type: string | null;
  received_at: string;
  event_id: number | null;
  ignored_reason?: string | null;
  raw_body?: unknown;
};

export type IntegrationCreatorProfile = {
  handle: string | null;
  display_name: string | null;
};

export type PublicExternalRepo = {
  id: string;
  provider: ExternalProvider;
  repoFullName: string;
  projectRoot: string;
  createdAt: string;
  updatedAt: string | null;
  creatorHandle: string | null;
  creatorDisplayName: string | null;
};

export type PublicExternalDelivery = {
  viewKey: string;
  provider: ExternalProvider;
  deliveryIdShort: string;
  eventType: string;
  receivedAt: string;
  linkedToEvent: boolean;
  ignoredReason: string | null;
  repoFullName: string | null;
};

export function validateExternalRepoInput(
  providerValue: string,
  repoValue: string,
  projectRootValue: string,
): {
  provider: ExternalProvider;
  repoFullName: string;
  projectRoot: string;
} {
  const provider = normalizeProvider(providerValue);
  const repoFullName = repoValue.trim().toLowerCase();
  const projectRoot = projectRootValue.trim().replace(/\/+$/, '');

  if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repoFullName)) {
    throw new Error('repo must look like owner/name');
  }
  if (!projectRoot.startsWith('/')) {
    throw new Error('project root must be an absolute path');
  }
  if (projectRoot.includes('\n') || projectRoot.includes('\0')) {
    throw new Error('project root contains invalid characters');
  }
  return { provider, repoFullName, projectRoot };
}

export function normalizeProvider(value: string): ExternalProvider {
  const provider = value.trim().toLowerCase();
  if (provider === 'github' || provider === 'gitea') return provider;
  throw new Error('provider must be github or gitea');
}

export function toPublicExternalRepo(row: ExternalRepoRow): PublicExternalRepo | null {
  if (!row.id || !row.provider || !row.repo_full_name || !row.project_root) {
    return null;
  }
  const provider = normalizeProvider(row.provider);
  const creator = normalizeProfile(row.profiles);
  return {
    id: row.id,
    provider,
    repoFullName: row.repo_full_name.toLowerCase(),
    projectRoot: row.project_root,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    creatorHandle: creator?.handle ?? null,
    creatorDisplayName: creator?.display_name ?? null,
  };
}

export function toPublicExternalDelivery(
  row: ExternalDeliveryRow,
): PublicExternalDelivery | null {
  if (!row.provider || !row.delivery_id || !row.event_type) return null;
  const provider = normalizeProvider(row.provider);
  return {
    viewKey: `${row.received_at}:${provider}:${row.delivery_id}`,
    provider,
    deliveryIdShort: shortenDeliveryId(row.delivery_id),
    eventType: row.event_type,
    receivedAt: row.received_at,
    linkedToEvent: row.event_id != null,
    ignoredReason: typeof row.ignored_reason === 'string' && row.ignored_reason.trim()
      ? row.ignored_reason.trim()
      : null,
    repoFullName: repoFullNameFromRawBody(row.raw_body),
  };
}

export function buildWebhookUrl(baseUrl: string, provider: ExternalProvider): string {
  const base = baseUrl.trim().replace(/\/+$/, '');
  if (!base) return `/webhooks/${provider}`;
  return `${base}/webhooks/${provider}`;
}

function shortenDeliveryId(value: string): string {
  if (value.length <= 12) return value;
  return `${value.slice(0, 8)}...${value.slice(-4)}`;
}

function repoFullNameFromRawBody(rawBody: unknown): string | null {
  if (!rawBody || typeof rawBody !== 'object') return null;
  const body = rawBody as { repository?: unknown; repo?: unknown };
  const repo = body.repository ?? body.repo;
  if (!repo || typeof repo !== 'object') return null;
  const fullName = (repo as { full_name?: unknown; fullName?: unknown }).full_name
    ?? (repo as { full_name?: unknown; fullName?: unknown }).fullName;
  return typeof fullName === 'string' && fullName.trim()
    ? fullName.trim().toLowerCase()
    : null;
}

function normalizeProfile(
  profile: IntegrationCreatorProfile | IntegrationCreatorProfile[] | null | undefined,
): IntegrationCreatorProfile | null {
  if (Array.isArray(profile)) return profile[0] ?? null;
  return profile ?? null;
}
