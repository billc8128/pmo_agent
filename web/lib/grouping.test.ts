import assert from 'node:assert/strict';
import test from 'node:test';

process.env.TZ = 'UTC';

const { groupByDayAndProject } = await import('./grouping.ts');

function turnAt(userMessageAt: string) {
  return {
    id: 1,
    user_id: 'user-1',
    agent: 'codex',
    agent_session_id: 'session-1',
    project_path: '/Users/a/Desktop/pmo_agent',
    project_root: '/Users/a/Desktop/pmo_agent',
    turn_index: 0,
    user_message: 'hello',
    agent_response_full: 'world',
    agent_summary: 'world',
    device_label: 'MacBook-Air',
    user_message_at: userMessageAt,
    agent_response_at: null,
    created_at: userMessageAt,
    updated_at: userMessageAt,
  };
}

test('groups UTC evening turns by the app timeline day', () => {
  const days = groupByDayAndProject([
    turnAt('2026-05-03T16:25:37.946+00:00'),
  ]);

  assert.equal(days[0]?.dayKey, '2026-05-04');
});
