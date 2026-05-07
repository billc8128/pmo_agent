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
    target_kind: 'user_dm',
    target_id: VIEWER_ID,
    target_user_open_id: null,
    consent_anchor: null,
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
    targetKind: 'user_dm',
    targetLabel: 'Your DM',
  });
  assert.equal(Object.hasOwn(rule, 'id'), false);
  assert.equal(Object.hasOwn(rule, 'scope_id'), false);
  assert.equal(Object.hasOwn(rule, 'created_by'), false);
  assert.equal(Object.hasOwn(rule, 'chat_id'), false);
  assert.equal(Object.hasOwn(rule, 'target_id'), false);
});

test('does not expose subscription ids for rules owned by other users', () => {
  const rule = toPublicNotificationRule(
    row({ created_by: '22222222-2222-4222-8222-222222222222' }),
    VIEWER_ID,
  );

  assert.equal(rule?.ownedByViewer, false);
  assert.equal(rule?.subscriptionId, null);
});

test('shows chat-scoped rules and lets their creator manage them', () => {
  const rule = toPublicNotificationRule(
    row({
      scope_kind: 'chat',
      scope_id: 'oc_123',
      target_kind: 'chat',
      target_id: 'oc_123',
    }),
    VIEWER_ID,
  );

  assert.equal(rule?.ownedByViewer, true);
  assert.equal(rule?.subscriptionId, 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa');
  assert.equal(rule?.targetLabel, 'Feishu chat');
  assert.equal(Object.hasOwn(rule, 'created_by'), false);
  assert.equal(Object.hasOwn(rule, 'scope_id'), false);
});

test('hides archived rules from the public directory', () => {
  assert.equal(
    toPublicNotificationRule(row({ archived_at: '2026-05-05T01:00:00.000Z' }), VIEWER_ID),
    null,
  );
});

test('validates free-text rule descriptions', () => {
  assert.equal(validateRuleDescription('  bcc 的改动都通知我  '), 'bcc 的改动都通知我');
  assert.throws(() => validateRuleDescription(''), /empty/);
  assert.throws(() => validateRuleDescription('x'.repeat(241)), /240/);
});
