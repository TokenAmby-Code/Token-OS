import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  LONG_HOLD_TIMEOUT_MS,
  DEFAULT_REQUEST_TIMEOUT_MS,
  createTmuxctldClient,
} from './tmuxctld-client.ts';

function installFetchAndTimerRecorder() {
  const calls = [];
  const timeouts = [];
  const priorFetch = globalThis.fetch;
  const priorSetTimeout = globalThis.setTimeout;
  const priorClearTimeout = globalThis.clearTimeout;
  globalThis.fetch = async (url, opts) => {
    calls.push({ url: String(url), opts });
    return { ok: true, async json() { return { ok: true, result: { ok: true } }; } };
  };
  globalThis.setTimeout = (fn, ms, ...args) => {
    timeouts.push(ms);
    return priorSetTimeout(fn, 60_000_000, ...args);
  };
  globalThis.clearTimeout = (id) => priorClearTimeout(id);
  return {
    calls,
    timeouts,
    restore() {
      globalThis.fetch = priorFetch;
      globalThis.setTimeout = priorSetTimeout;
      globalThis.clearTimeout = priorClearTimeout;
    },
  };
}

test('voice session start/clear use long-hold timeout above 60s ceiling', async () => {
  const rec = installFetchAndTimerRecorder();
  const priorUrl = process.env.TMUXCTLD_URL;
  const priorTimeout = process.env.TMUXCTLD_REQUEST_TIMEOUT_MS;
  process.env.TMUXCTLD_REQUEST_TIMEOUT_MS = '5000';
  process.env.TMUXCTLD_URL = 'http://tmuxctld.test';
  try {
    const client = createTmuxctldClient();
    await client.startVoiceSession({ botName: 'imperial_guard', userId: 'u1' });
    await client.clearVoiceSession({ voiceSessionId: 'vs-1' });
    await client.sendText({ target: 'palace:E', text: 'hello' });

    // A stale global 5s timeout must not shorten long-hold routes.
    assert.equal(LONG_HOLD_TIMEOUT_MS, 75_000);
    assert.ok(LONG_HOLD_TIMEOUT_MS > 60_000);
    assert.deepEqual(rec.timeouts, [LONG_HOLD_TIMEOUT_MS, LONG_HOLD_TIMEOUT_MS, LONG_HOLD_TIMEOUT_MS]);
    assert.deepEqual(rec.calls.map(c => new URL(c.url).pathname), [
      '/voice/session/start',
      '/voice/session/clear',
      '/send-text',
    ]);
  } finally {
    rec.restore();
    if (priorUrl === undefined) delete process.env.TMUXCTLD_URL; else process.env.TMUXCTLD_URL = priorUrl;
    if (priorTimeout === undefined) delete process.env.TMUXCTLD_REQUEST_TIMEOUT_MS; else process.env.TMUXCTLD_REQUEST_TIMEOUT_MS = priorTimeout;
  }
});

test('cheap tmuxctld lookups keep the short default timeout', async () => {
  const rec = installFetchAndTimerRecorder();
  const priorTimeout = process.env.TMUXCTLD_REQUEST_TIMEOUT_MS;
  delete process.env.TMUXCTLD_REQUEST_TIMEOUT_MS;
  try {
    const client = createTmuxctldClient();
    await client.voiceTarget('imperial_guard');
    assert.equal(DEFAULT_REQUEST_TIMEOUT_MS, 5_000);
    assert.deepEqual(rec.timeouts, [DEFAULT_REQUEST_TIMEOUT_MS]);
    assert.equal(new URL(rec.calls[0].url).pathname, '/voice/target');
  } finally {
    rec.restore();
    if (priorTimeout === undefined) delete process.env.TMUXCTLD_REQUEST_TIMEOUT_MS; else process.env.TMUXCTLD_REQUEST_TIMEOUT_MS = priorTimeout;
  }
});
