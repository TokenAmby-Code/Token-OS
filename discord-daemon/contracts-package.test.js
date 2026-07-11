// The daemon consumes the shared TS contracts package.
//
// `@token-os/contracts` (token-api/web/contracts/) exports Zod schemas for the
// ops read-model — the same contracts the ops cockpit compiles against. The
// daemon gained the dep with the TS conversion (Terminus Stage 2 PR C);
// graduated from the bounty lane in that PR.

import { test } from 'node:test';
import assert from 'node:assert/strict';

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

test('@token-os/contracts exposes OpsStateSchema (Zod) that parses ops-state.v1', async () => {
  const contracts = await import('@token-os/contracts');
  assert.equal(contracts.CONTRACT_VERSION, 'ops-state.v1');
  const parsed = contracts.OpsStateSchema.parse(OPS_STATE_FIXTURE);
  assert.equal(parsed.contract_version, 'ops-state.v1');
  assert.equal(parsed.instances.active.length, 2);
  assert.equal(parsed.instances.active[0].id, 'custodes');
});

// voice-selftest.v1 — the probe report emitted by the daemon's own
// voice-selftest module and consumed via /voice/selftest + events log.
const SELFTEST_REPORT_FIXTURE = {
  contract_version: 'voice-selftest.v1',
  probe_id: 'probe-abc-1',
  variant: 'full',
  trigger: 'cron',
  started_at: '2026-07-10T07:30:00.000Z',
  finished_at: '2026-07-10T07:30:24.000Z',
  duration_ms: 24000,
  overall: 'degraded',
  abort_reason: null,
  first_failed_stage: null,
  stages: [
    { stage: 'operator_gate', ok: true, ms: 120 },
    { stage: 'voice_join', ok: true, ms: 2200 },
    { stage: 'audio_loop', ok: true, ms: 18000, detail: 'matched 6/7 tokens (attempt 2)' },
    { stage: 'tmuxctld_session', ok: true, ms: 300 },
    { stage: 'cleanup', ok: true, ms: 900 },
  ],
  transcript_match: {
    matched: true,
    matched_tokens: 6,
    total_tokens: 7,
    attempts: 2,
    passed_on_retry: true,
    transcript: 'golden signal probe verifying discord audio loop',
  },
  daemon: { version: null, pid: 1234, node: 'v22.22.0' },
};

test('@token-os/contracts SelftestReportSchema parses voice-selftest.v1 and rejects a bad overall', async () => {
  const contracts = await import('@token-os/contracts');
  assert.equal(contracts.VOICE_SELFTEST_CONTRACT_VERSION, 'voice-selftest.v1');
  const parsed = contracts.SelftestReportSchema.parse(SELFTEST_REPORT_FIXTURE);
  assert.equal(parsed.overall, 'degraded');
  assert.equal(parsed.stages.length, 5);
  assert.equal(parsed.transcript_match.passed_on_retry, true);

  const bad = { ...SELFTEST_REPORT_FIXTURE, overall: 'sort-of-fine' };
  assert.throws(() => contracts.SelftestReportSchema.parse(bad));
});

// voice-drafts-reconcile.v1 — the three-way draft-truth compare returned by
// the daemon's POST /voice/drafts/reconcile.
const RECONCILE_REPORT_FIXTURE = {
  contract_version: 'voice-drafts-reconcile.v1',
  auto_clear: true,
  counts: { daemon_drafts: 1, tmuxctld_sessions: 2, token_api_drafts: 0 },
  sources: { tmuxctld: { ok: true }, token_api: { ok: false, error: 'ECONNREFUSED' } },
  orphans: [
    {
      source: 'tmuxctld_session',
      bot_name: 'custodes',
      author_id: 'user-1',
      voice_session_id: 'vs-orphan',
      cleared: true,
    },
  ],
  in_sync: false,
};

test('@token-os/contracts ReconcileReportSchema parses voice-drafts-reconcile.v1 and rejects a bad source', async () => {
  const contracts = await import('@token-os/contracts');
  assert.equal(contracts.VOICE_RECONCILE_CONTRACT_VERSION, 'voice-drafts-reconcile.v1');
  const parsed = contracts.ReconcileReportSchema.parse(RECONCILE_REPORT_FIXTURE);
  assert.equal(parsed.orphans[0].source, 'tmuxctld_session');
  assert.equal(parsed.in_sync, false);

  const bad = {
    ...RECONCILE_REPORT_FIXTURE,
    orphans: [{ ...RECONCILE_REPORT_FIXTURE.orphans[0], source: 'somewhere-else' }],
  };
  assert.throws(() => contracts.ReconcileReportSchema.parse(bad));
});
