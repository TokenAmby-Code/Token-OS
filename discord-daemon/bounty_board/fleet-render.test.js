// Bounty: pure read-model renderer for the #fleet-status message.
//
// `fleet-render.ts` (Terminus Stage 2 PR D) turns an OpsState into the
// edit-in-place Discord message body: pure function, no I/O, output must fit
// Discord's 2000-char content limit.

import assert from 'node:assert/strict';
import { bounty } from './bounty.js';

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

bounty('fleet-render produces a <=2000-char message naming every active instance', async () => {
  const { renderFleetStatus } = await import('../fleet-render.ts');
  const { content } = renderFleetStatus(OPS_STATE_FIXTURE);
  assert.equal(typeof content, 'string');
  assert.ok(content.includes('custodes') || content.includes('Custodes'));
  assert.ok(content.includes('fabricator-general') || content.includes('Fabricator-General'));
  assert.ok(content.length <= 2000, `content exceeds Discord limit: ${content.length}`);
});
