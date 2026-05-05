import assert from 'node:assert/strict';
import test from 'node:test';

process.env.TZ = 'UTC';

const {
  validateRuleDescription,
  toPublicNotificationRule,
} = await import('./notification-rules.ts');

const VIEWER_ID = '11111111-1111-4111-8111-111111111111';

function row(overrides = {}) {
  return {
    id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    scope_kind: 'user',
    scope_id: VIEWER_ID,
    description: '  vibelive 播放器进展告诉我  ',
    enabled: true,
    created_by: VIEWER_ID,
    chat_id: 'oc_hidden',
    created_at: '2026-05-05T00:00:00.000Z',
    updated_at: '2026-05-05T00:01:00.000Z',
    archived_at: null,
    profiles: {
      handle: 'chenchen',
      display_name: '晨晨',
    },
    ...overrides,
  };
}

test('maps raw subscription rows to safe public rule objects', () => {
  const rule = toPublicNotificationRule(row(), VIEWER_ID);

  assert.deepEqual(rule, {
    viewKey: '2026-05-05T00:00:00.000Z:chenchen:vibelive 播放器进展告诉我',
    subscriptionId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
    description: 'vibelive 播放器进展告诉我',
    enabled: true,
    createdAt: '2026-05-05T00:00:00.000Z',
    updatedAt: '2026-05-05T00:01:00.000Z',
    ownerHandle: 'chenchen',
    ownerDisplayName: '晨晨',
    ownedByViewer: true,
  });
  assert.equal(Object.hasOwn(rule, 'id'), false);
  assert.equal(Object.hasOwn(rule, 'scope_id'), false);
  assert.equal(Object.hasOwn(rule, 'created_by'), false);
  assert.equal(Object.hasOwn(rule, 'chat_id'), false);
});

test('does not expose subscription ids for rules owned by other users', () => {
  const rule = toPublicNotificationRule(
    row({ scope_id: '22222222-2222-4222-8222-222222222222' }),
    VIEWER_ID,
  );

  assert.equal(rule?.ownedByViewer, false);
  assert.equal(rule?.subscriptionId, null);
});

test('hides archived and chat-scoped rules from the public directory', () => {
  assert.equal(
    toPublicNotificationRule(row({ archived_at: '2026-05-05T01:00:00.000Z' }), VIEWER_ID),
    null,
  );
  assert.equal(
    toPublicNotificationRule(row({ scope_kind: 'chat', scope_id: 'oc_123' }), VIEWER_ID),
    null,
  );
});

test('validates free-text rule descriptions', () => {
  assert.equal(validateRuleDescription('  bcc 的改动都通知我  '), 'bcc 的改动都通知我');
  assert.throws(() => validateRuleDescription(''), /empty/);
  assert.throws(() => validateRuleDescription('x'.repeat(241)), /240/);
});
