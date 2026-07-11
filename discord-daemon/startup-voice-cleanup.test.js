// startup-voice-cleanup.test.js — pins the boot sweep ordering and typed errors (PR A).
//
// The old boot path fired /voice/session/clear while tmuxctld was still coming
// up (token-restart restarts both together) and burned a 75s long-hold timeout
// on every daemon start. The sweep also never touched token-api's
// _discord_voice_drafts dict — the third copy of draft truth.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createStartupVoiceCleanup } from './startup-voice-cleanup.ts';

function makeLogger(logs = []) {
  return {
    logs,
    debug(msg, meta) { logs.push(['debug', String(msg), meta]); },
    info(msg, meta) { logs.push(['info', String(msg), meta]); },
    warn(msg, meta) { logs.push(['warn', String(msg), meta]); },
    error(msg, meta) { logs.push(['error', String(msg), meta]); },
  };
}

function connRefused() {
  const err = new Error('fetch failed');
  err.code = 'ECONNREFUSED';
  return err;
}

function timedOut() {
  const err = new Error('tmuxctld timeout /health after 1500ms');
  err.code = 'ETIMEDOUT';
  return err;
}

function okFetch(calls, payload = { cleared: 2 }) {
  return async (url, opts) => {
    calls.push({ url, opts });
    return { ok: true, status: 200, json: async () => payload };
  };
}

const CONFIG = {
  voice_channels: { mechanicus: 'vc1', imperial_guard: 'vc2' },
  token_api_port: 7777,
};

test('waits for tmuxctld health with backoff, then clears with short timeouts and sweeps token-api', async () => {
  const logger = makeLogger();
  const sleeps = [];
  const clears = [];
  const fetchCalls = [];
  let healthCalls = 0;

  const cleanup = createStartupVoiceCleanup({
    config: CONFIG,
    logger,
    voiceTranscriptRouter: {
      async clear(filter, opts) {
        clears.push({ filter, opts });
        return [];
      },
    },
    tmuxctld: {
      async health() {
        healthCalls += 1;
        if (healthCalls < 3) throw connRefused();
        return { status: 'ok' };
      },
    },
    fetchImpl: okFetch(fetchCalls),
    sleep: async (ms) => { sleeps.push(ms); },
  });

  const report = await cleanup.run();

  assert.equal(report.tmuxctld.healthy, true);
  assert.equal(report.tmuxctld.attempts, 3);
  assert.deepEqual(sleeps, [250, 500], 'exponential backoff between health attempts');
  assert.deepEqual(clears.map(c => c.filter.bot), ['mechanicus', 'imperial_guard']);
  assert.ok(clears.every(c => c.opts.timeoutMs === 5_000), 'clears use short per-call timeouts, not the 75s long-hold');
  assert.equal(fetchCalls.length, 1);
  assert.match(fetchCalls[0].url, /:7777\/api\/discord\/voice-drafts\/clear$/);
  assert.equal(report.tokenApiSweep.ok, true);
  assert.equal(report.tokenApiSweep.cleared, 2);
  assert.ok(!logger.logs.some(([lvl]) => lvl === 'error'), 'clean boot never logs at error');
});

test('health timeout means wedged endpoint: typed warn, no retry loop, clears still attempted', async () => {
  const logger = makeLogger();
  const clears = [];
  let healthCalls = 0;

  const cleanup = createStartupVoiceCleanup({
    config: CONFIG,
    logger,
    voiceTranscriptRouter: {
      async clear(filter, opts) { clears.push({ filter, opts }); return []; },
    },
    tmuxctld: { async health() { healthCalls += 1; throw timedOut(); } },
    fetchImpl: okFetch([]),
    sleep: async () => {},
  });

  const report = await cleanup.run();

  assert.equal(healthCalls, 1, 'a wedged endpoint is not re-polled');
  assert.equal(report.tmuxctld.healthy, false);
  assert.equal(report.tmuxctld.reason, 'ETIMEDOUT');
  assert.ok(logger.logs.some(([lvl, , meta]) => lvl === 'warn' && meta?.errorCode === 'tmuxctld_health_timeout'));
  assert.equal(clears.length, 2, 'short-timeout clears still attempted');
});

test('tmuxctld unreachable: bounded retries, clears skipped, token-api sweep still runs', async () => {
  const logger = makeLogger();
  const clears = [];
  const fetchCalls = [];
  let healthCalls = 0;

  const cleanup = createStartupVoiceCleanup({
    config: CONFIG,
    logger,
    voiceTranscriptRouter: {
      async clear(filter, opts) { clears.push({ filter, opts }); return []; },
    },
    tmuxctld: { async health() { healthCalls += 1; throw connRefused(); } },
    fetchImpl: okFetch(fetchCalls, { cleared: 0 }),
    sleep: async () => {},
    healthAttempts: 3,
  });

  const report = await cleanup.run();

  assert.equal(healthCalls, 3, 'retries are bounded');
  assert.equal(report.tmuxctld.reason, 'ECONNREFUSED');
  assert.ok(logger.logs.some(([lvl, , meta]) => lvl === 'warn' && meta?.errorCode === 'tmuxctld_unreachable_at_boot'));
  assert.equal(clears.length, 0, 'no pointless clears against a confirmed-down daemon');
  assert.equal(fetchCalls.length, 1, 'token-api sweep is independent of tmuxctld health');
});

test('a clear timeout is typed and does not block the remaining bots or the sweep', async () => {
  const logger = makeLogger();
  const fetchCalls = [];
  const clears = [];

  const cleanup = createStartupVoiceCleanup({
    config: CONFIG,
    logger,
    voiceTranscriptRouter: {
      async clear(filter, opts) {
        clears.push(filter.bot);
        if (filter.bot === 'mechanicus') throw timedOut();
        return [];
      },
    },
    tmuxctld: { async health() { return { status: 'ok' }; } },
    fetchImpl: okFetch(fetchCalls),
    sleep: async () => {},
  });

  const report = await cleanup.run();

  assert.deepEqual(clears, ['mechanicus', 'imperial_guard'], 'one bot timing out does not skip the next');
  assert.deepEqual(report.clears.map(c => c.ok), [false, true]);
  assert.equal(report.clears[0].errorCode, 'voice_boot_clear_timeout');
  assert.ok(logger.logs.some(([lvl, , meta]) => lvl === 'warn' && meta?.errorCode === 'voice_boot_clear_timeout'));
  assert.equal(fetchCalls.length, 1);
});

test('token-api sweep failure is a typed warn, never fatal to boot', async () => {
  const logger = makeLogger();

  const cleanup = createStartupVoiceCleanup({
    config: CONFIG,
    logger,
    voiceTranscriptRouter: { async clear() { return []; } },
    tmuxctld: { async health() { return { status: 'ok' }; } },
    fetchImpl: async () => { throw connRefused(); },
    sleep: async () => {},
  });

  const report = await cleanup.run();

  assert.equal(report.tokenApiSweep.ok, false);
  assert.equal(report.tokenApiSweep.error, 'ECONNREFUSED');
  assert.ok(logger.logs.some(([lvl, , meta]) => lvl === 'warn' && meta?.errorCode === 'token_api_draft_sweep_failed'));
});
