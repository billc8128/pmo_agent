import assert from 'node:assert/strict';
import test from 'node:test';

process.env.TZ = 'UTC';

const {
  buildWebhookUrl,
  toPublicExternalDelivery,
  toPublicExternalRepo,
  validateExternalRepoInput,
} = await import('./integrations.ts');

test('validates external repo mappings for supported providers', () => {
  assert.deepEqual(
    validateExternalRepoInput(' GitHub ', ' billc8128/vibelive ', '/Users/a/Desktop/vibelive/'),
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
    repo_full_name: 'team/internal',
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
    event_id: 42,
    raw_body: { repository: { full_name: 'billc8128/vibelive' }, secret: 'hidden' },
  });

  assert.deepEqual(delivery, {
    viewKey: '2026-05-06T00:00:00.000Z:github:1234567890abcdef',
    provider: 'github',
    deliveryIdShort: '12345678...cdef',
    eventType: 'pull_request',
    receivedAt: '2026-05-06T00:00:00.000Z',
    linkedToEvent: true,
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
