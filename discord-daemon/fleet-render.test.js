// Pure read-model renderer for the #fleet-status message.
//
// `fleet-render.ts` turns an OpsState into the edit-in-place Discord message
// body: pure function, no I/O, output must fit Discord's 2000-char content
// limit. Graduated from the bounty lane in Terminus Stage 2 PR D.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { renderFleetStatus, DISCORD_CONTENT_LIMIT } from './fleet-render.ts';

const OPS_STATE_FIXTURE = {
  surface: 'ops',
  contract_version: 'ops-state.v1',
  ui_build_id: null,
  generated_at: '2026-07-08T12:00:00Z',
  timer: { mode: 'working', activity: 'work', productivity_active: true },
  instances: {
    active: [
      { id: 'custodes', display_name: 'Custodes', status: 'processing', engine: 'claude' },
      { id: 'fabricator-general', display_name: 'Fabricator-General', status: 'stopped', engine: 'codex' },
    ],
    counts: { active: 2, stale: 0, by_status: {}, by_engine: {}, by_persona: {} },
  },
};

test('fleet-render produces a <=2000-char message naming every active instance', () => {
  const { content } = renderFleetStatus(OPS_STATE_FIXTURE);
  assert.equal(typeof content, 'string');
  assert.ok(content.includes('custodes') || content.includes('Custodes'));
  assert.ok(content.includes('fabricator-general') || content.includes('Fabricator-General'));
  assert.ok(content.length <= 2000, `content exceeds Discord limit: ${content.length}`);
});

test('fleet-render stays under the limit on a huge fleet and reports the cut', () => {
  const active = Array.from({ length: 200 }, (_, i) => ({
    id: `worker-${i}`,
    display_name: `Worker ${i}`,
    status: 'processing',
    engine: 'codex',
  }));
  const { content } = renderFleetStatus({
    ...OPS_STATE_FIXTURE,
    instances: { active, counts: { active: active.length, stale: 0 } },
  });
  assert.ok(content.length <= DISCORD_CONTENT_LIMIT, `content exceeds limit: ${content.length}`);
  assert.match(content, /… \+\d+ more/);
});

test('fleet-render renders an explicit empty state', () => {
  const { content } = renderFleetStatus({
    ...OPS_STATE_FIXTURE,
    instances: { active: [], counts: { active: 0, stale: 0 } },
  });
  assert.ok(content.includes('no active instances'));
});
