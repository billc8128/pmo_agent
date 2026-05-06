import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

process.env.TZ = 'UTC';

const {
  buildWebhookUrl,
  toPublicExternalDelivery,
  toPublicExternalRepo,
  validateExternalRepoInput,
} = await import('./integrations.ts');
const { canManageIntegrations } = await import('./integration-permissions.ts');

test('validates external repo mappings for supported providers', () => {
  assert.deepEqual(
    validateExternalRepoInput(' GitHub ', ' BillC8128/VibeLive ', '/Users/a/Desktop/vibelive/'),
    {
      provider: 'github',
      repoFullName: 'billc8128/vibelive',
      projectRoot: '/Users/a/Desktop/vibelive',
    },
  );
  assert.throws(
    () => validateExternalRepoInput('gitlab', 'billc8128/vibelive', '/repo/vibelive'),
    /provider/,
  );
  assert.throws(
    () => validateExternalRepoInput('github', 'not-a-repo', '/repo/vibelive'),
    /owner\/name/,
  );
  assert.throws(
    () => validateExternalRepoInput('github', 'billc8128/vibelive', 'relative/path'),
    /absolute path/,
  );
});

test('maps external repo rows without exposing created_by ids', () => {
  const repo = toPublicExternalRepo({
    id: 'repo-id',
    provider: 'gitea',
    repo_full_name: 'Team/Internal',
    project_root: '/srv/internal',
    created_by: 'hidden-user-id',
    created_at: '2026-05-06T00:00:00.000Z',
    updated_at: '2026-05-06T00:01:00.000Z',
    profiles: { handle: 'chenchen', display_name: '晨晨' },
  });

  assert.deepEqual(repo, {
    id: 'repo-id',
    provider: 'gitea',
    repoFullName: 'team/internal',
    projectRoot: '/srv/internal',
    createdAt: '2026-05-06T00:00:00.000Z',
    updatedAt: '2026-05-06T00:01:00.000Z',
    creatorHandle: 'chenchen',
    creatorDisplayName: '晨晨',
  });
  assert.equal(Object.hasOwn(repo, 'created_by'), false);
});

test('maps recent deliveries to safe public fields', () => {
  const delivery = toPublicExternalDelivery({
    provider: 'github',
    delivery_id: '1234567890abcdef',
    event_type: 'pull_request',
    received_at: '2026-05-06T00:00:00.000Z',
    event_id: null,
    ignored_reason: 'missing_source_identity',
    raw_body: { repository: { full_name: 'BillC8128/VibeLive' }, secret: 'hidden' },
  });

  assert.deepEqual(delivery, {
    viewKey: '2026-05-06T00:00:00.000Z:github:1234567890abcdef',
    provider: 'github',
    deliveryIdShort: '12345678...cdef',
    eventType: 'pull_request',
    receivedAt: '2026-05-06T00:00:00.000Z',
    linkedToEvent: false,
    ignoredReason: 'missing_source_identity',
    repoFullName: 'billc8128/vibelive',
  });
  assert.equal(Object.hasOwn(delivery, 'raw_body'), false);
});

test('builds provider webhook URLs from configured bot base URL', () => {
  assert.equal(
    buildWebhookUrl('https://pmo-bot.up.railway.app/', 'github'),
    'https://pmo-bot.up.railway.app/webhooks/github',
  );
  assert.equal(buildWebhookUrl('', 'gitea'), '/webhooks/gitea');
});

test('integration management is fail-closed unless admin env is configured', () => {
  const previousIds = process.env.PMO_INTEGRATION_ADMIN_USER_IDS;
  const previousHandles = process.env.PMO_INTEGRATION_ADMIN_HANDLES;
  delete process.env.PMO_INTEGRATION_ADMIN_USER_IDS;
  delete process.env.PMO_INTEGRATION_ADMIN_HANDLES;
  try {
    assert.equal(canManageIntegrations({ id: 'user-1', handle: 'chenchen' }), false);

    process.env.PMO_INTEGRATION_ADMIN_HANDLES = 'chenchen,@bcc';
    assert.equal(canManageIntegrations({ id: 'user-1', handle: 'chenchen' }), true);
    assert.equal(canManageIntegrations({ id: 'user-2', handle: 'other' }), false);

    process.env.PMO_INTEGRATION_ADMIN_USER_IDS = 'user-3';
    assert.equal(canManageIntegrations({ id: 'user-3', handle: 'other' }), true);
  } finally {
    if (previousIds === undefined) {
      delete process.env.PMO_INTEGRATION_ADMIN_USER_IDS;
    } else {
      process.env.PMO_INTEGRATION_ADMIN_USER_IDS = previousIds;
    }
    if (previousHandles === undefined) {
      delete process.env.PMO_INTEGRATION_ADMIN_HANDLES;
    } else {
      process.env.PMO_INTEGRATION_ADMIN_HANDLES = previousHandles;
    }
  }
});

test('integrations page does not create service-role client for anonymous viewers', () => {
  const source = readFileSync(new URL('../app/integrations/page.tsx', import.meta.url), 'utf8');
  const loginGate = source.indexOf('if (!user)');
  const adminClientUse = source.indexOf('adminClient()');

  assert.ok(loginGate > 0, 'expected explicit anonymous viewer gate');
  assert.ok(adminClientUse > 0, 'expected page to still use admin client after auth');
  assert.ok(loginGate < adminClientUse, 'anonymous gate must run before adminClient()');
});
