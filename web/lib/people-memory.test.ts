import assert from 'node:assert/strict';
import test from 'node:test';

process.env.TZ = 'UTC';

const { toPublicPeopleMemory, toPublicPersonMemory } = await import('./people-memory.ts');

function row(overrides = {}) {
  return {
    person_key: 'feishu:ou_hidden',
    profile_id: '11111111-1111-4111-8111-111111111111',
    feishu_open_id: 'ou_hidden',
    display_name: '晨晨',
    handle: 'bcc',
    pmo_notes: '  擅长把复杂方案拆成可以推进的工程任务。  ',
    notes_updated_at: '2026-05-07T00:00:00.000Z',
    last_observed_at: '2026-05-06T12:00:00.000Z',
    metadata: { source: 'hidden' },
    created_at: '2026-05-05T00:00:00.000Z',
    updated_at: '2026-05-07T00:01:00.000Z',
    ...overrides,
  };
}

test('maps people memory rows to safe public objects', () => {
  const person = toPublicPersonMemory(row());

  assert.deepEqual(person, {
    viewKey: 'bcc:2026-05-07T00:00:00.000Z',
    displayName: '晨晨',
    handle: 'bcc',
    note: '擅长把复杂方案拆成可以推进的工程任务。',
    notesUpdatedAt: '2026-05-07T00:00:00.000Z',
    lastObservedAt: '2026-05-06T12:00:00.000Z',
    updatedAt: '2026-05-07T00:01:00.000Z',
  });
  assert.equal(Object.hasOwn(person, 'person_key'), false);
  assert.equal(Object.hasOwn(person, 'profile_id'), false);
  assert.equal(Object.hasOwn(person, 'feishu_open_id'), false);
  assert.equal(Object.hasOwn(person, 'metadata'), false);
});

test('hides rows without PMO notes', () => {
  assert.equal(toPublicPersonMemory(row({ pmo_notes: '   ' })), null);
  assert.equal(toPublicPersonMemory(row({ pmo_notes: null })), null);
});

test('normalizes missing names without exposing stable ids', () => {
  const person = toPublicPersonMemory(
    row({
      display_name: null,
      handle: null,
      person_key: 'feishu:ou_still_hidden',
      profile_id: null,
      feishu_open_id: 'ou_still_hidden',
    }),
  );

  assert.equal(person?.displayName, 'Unknown member');
  assert.equal(person?.handle, null);
  assert.equal(person?.viewKey, 'unknown:2026-05-07T00:00:00.000Z');
});

test('sorts people by last observed time then note update time', () => {
  const rows = [
    row({
      display_name: 'Old',
      handle: 'old',
      last_observed_at: '2026-05-01T00:00:00.000Z',
      notes_updated_at: '2026-05-07T00:00:00.000Z',
    }),
    row({
      display_name: 'Recent',
      handle: 'recent',
      last_observed_at: '2026-05-06T00:00:00.000Z',
      notes_updated_at: '2026-05-06T00:00:00.000Z',
    }),
  ];

  assert.deepEqual(
    toPublicPeopleMemory(rows).map((person) => person.handle),
    ['recent', 'old'],
  );
});
