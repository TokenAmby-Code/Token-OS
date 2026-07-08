// Bounty: the daemon consumes the shared TS contracts package.
//
// `@token-os/contracts` (token-api/web/contracts/) exports Zod schemas for the
// ops read-model — the same contracts the ops cockpit compiles against. The
// daemon gets the dep with the TS conversion (Terminus Stage 2 PR C).

import assert from 'node:assert/strict';
import { bounty } from './bounty.js';

// Minimal ops-state.v1 payload — schemas are permissive (passthrough,
// optional-friendly), so a skeletal state must parse.
const OPS_STATE_FIXTURE = {
  surface: 'ops',
  contract_version: 'ops-state.v1',
  ui_build_id: null,
  generated_at: '2026-07-08T12:00:00Z',
  instances: {
    active: [
      { id: 'custodes', display_name: 'Custodes', status: 'processing', engine: 'claude' },
      { id: 'fabricator-general', display_name: 'Fabricator-General', status: 'stopped', engine: 'codex' },
    ],
    counts: { active: 2, stale: 0, by_status: {}, by_engine: {}, by_persona: {} },
  },
};

bounty('@token-os/contracts exposes OpsStateSchema (Zod) that parses ops-state.v1', async () => {
  const contracts = await import('@token-os/contracts');
  assert.equal(contracts.CONTRACT_VERSION, 'ops-state.v1');
  const parsed = contracts.OpsStateSchema.parse(OPS_STATE_FIXTURE);
  assert.equal(parsed.contract_version, 'ops-state.v1');
  assert.equal(parsed.instances.active.length, 2);
  assert.equal(parsed.instances.active[0].id, 'custodes');
});
