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
  statusLabel: string;
  statusDetail: string | null;
  statusTone: 'ok' | 'warn' | 'idle';
  repoFullName: string | null;
};

export function validateExternalRepoInput(
  providerValue: string,
  repoValue: string,
): {
  provider: ExternalProvider;
  repoFullName: string;
  projectRoot: string;
  repoUrl: string;
} {
  const provider = normalizeProvider(providerValue);
  const { repoFullName, repoUrl } = parseRepoUrl(provider, repoValue);
  const projectRoot = `${provider}:${repoFullName}`;

  return { provider, repoFullName, projectRoot, repoUrl };
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
  const ignoredReason = typeof row.ignored_reason === 'string' && row.ignored_reason.trim()
    ? row.ignored_reason.trim()
    : null;
  const status = deliveryStatus(row.event_id != null, ignoredReason);
  return {
    viewKey: `${row.received_at}:${provider}:${row.delivery_id}`,
    provider,
    deliveryIdShort: shortenDeliveryId(row.delivery_id),
    eventType: row.event_type,
    receivedAt: row.received_at,
    linkedToEvent: row.event_id != null,
    ignoredReason,
    statusLabel: status.label,
    statusDetail: status.detail,
    statusTone: status.tone,
    repoFullName: repoFullNameFromRawBody(row.raw_body),
  };
}

export function buildWebhookUrl(baseUrl: string, provider: ExternalProvider): string {
  const base = baseUrl.trim().replace(/\/+$/, '');
  if (!base) return `/webhooks/${provider}`;
  return `${base}/webhooks/${provider}`;
}

function parseRepoUrl(
  provider: ExternalProvider,
  rawValue: string,
): { repoFullName: string; repoUrl: string } {
  const value = rawValue.trim();
  if (!value) {
    throw new Error('repository URL is required');
  }

  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error('repository must be a GitHub or Gitea URL');
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
    throw new Error('repository URL must start with https:// or http://');
  }

  const host = parsed.hostname.toLowerCase();
  if (provider === 'github' && host !== 'github.com' && host !== 'www.github.com') {
    throw new Error('GitHub URL must use github.com');
  }

  const [owner, repoRaw] = parsed.pathname
    .split('/')
    .map((part) => part.trim())
    .filter(Boolean);
  const repo = (repoRaw ?? '').replace(/\.git$/i, '');
  const repoFullName = `${owner ?? ''}/${repo}`.toLowerCase();

  if (!/^[a-z0-9_.-]+\/[a-z0-9_.-]+$/.test(repoFullName)) {
    throw new Error('repository URL must include owner/name');
  }

  return {
    repoFullName,
    repoUrl: `${parsed.protocol}//${host}/${repoFullName}`,
  };
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

function deliveryStatus(
  linkedToEvent: boolean,
  ignoredReason: string | null,
): { label: string; detail: string | null; tone: 'ok' | 'warn' | 'idle' } {
  if (linkedToEvent) {
    return { label: 'Event created', detail: null, tone: 'ok' };
  }
  if (!ignoredReason) {
    return {
      label: 'Archived only',
      detail: 'The bot accepted this delivery but has not linked it to a proactive event yet.',
      tone: 'idle',
    };
  }
  switch (ignoredReason) {
    case 'unsupported_event_type':
      return {
        label: 'Unsupported event type',
        detail: 'Enable pull request, push, release, or issue comment events in the provider webhook settings.',
        tone: 'warn',
      };
    case 'bot_actor':
      return {
        label: 'Bot actor ignored',
        detail: 'The delivery came from a bot account, so it was archived without creating a proactive event.',
        tone: 'idle',
      };
    case 'missing_source_identity':
      return {
        label: 'Missing resource id',
        detail: 'The bot archived this webhook, but the payload did not include the PR number, commit SHA, release tag, or comment id needed to create an event.',
        tone: 'warn',
      };
    case 'unsupported_provider':
      return {
        label: 'Unsupported provider',
        detail: 'Only GitHub and Gitea webhook payloads are supported in this deployment.',
        tone: 'warn',
      };
    default:
      return {
        label: 'Archived only',
        detail: `The bot archived this delivery with reason: ${ignoredReason}.`,
        tone: 'idle',
      };
  }
}

function normalizeProfile(
  profile: IntegrationCreatorProfile | IntegrationCreatorProfile[] | null | undefined,
): IntegrationCreatorProfile | null {
  if (Array.isArray(profile)) return profile[0] ?? null;
  return profile ?? null;
}
