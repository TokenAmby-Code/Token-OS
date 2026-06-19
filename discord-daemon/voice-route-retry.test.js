import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  isRetryableVoiceRouteFailure,
  routeVoiceTranscriptWithRetry,
} from './voice-route-retry.js';

test('retry predicate recognizes tmux route failures', () => {
  assert.equal(isRetryableVoiceRouteFailure({ routed: false, reason: 'no_target' }), true);
  assert.equal(isRetryableVoiceRouteFailure(new Error('target not live: %9')), true);
  assert.equal(isRetryableVoiceRouteFailure(new Error('Command failed: tmux-dictate')), true);
  assert.equal(isRetryableVoiceRouteFailure({ routed: false, reason: 'no_draft' }), false);
});

test('route wrapper warns loudly once and does not retry tmux lag failures', async () => {
  const calls = [];
  const tts = [];
  const router = {
    async route() {
      calls.push(Date.now());
      if (calls.length === 1) return { routed: false, reason: 'no_target' };
      return { routed: true, target: '3:0', pane: '%9' };
    },
  };
  const result = await routeVoiceTranscriptWithRetry({
    router,
    voiceManager: { playTTS: async msg => tts.push(msg) },
    logger: { warn() {} },
    result: { botName: 'custodes', userId: 'u', text: 'hello' },
    maxAttempts: 3,
    retryDelayMs: 1,
  });

  assert.equal(calls.length, 1);
  assert.equal(tts.length, 1);
  assert.equal(tts[0], 'tmux lagging');
  assert.equal(result.routed, false);
  assert.equal(result.retry_disabled, true);
  assert.equal(result.tmux_lag, true);
  assert.equal(result.warning_sent, true);
});

test('route wrapper converts retryable tmux errors to a non-retried lag result', async () => {
  const tts = [];
  const result = await routeVoiceTranscriptWithRetry({
    router: {
      async route() {
        throw new Error('Command failed: tmux-dictate timed out');
      },
    },
    voiceManager: { playTTS: async msg => tts.push(msg) },
    logger: { warn() {} },
    result: { botName: 'custodes', userId: 'u', text: 'hello' },
    maxAttempts: 3,
    retryDelayMs: 1,
  });

  assert.equal(tts.length, 1);
  assert.equal(tts[0], 'tmux lagging');
  assert.equal(result.routed, false);
  assert.equal(result.retry_disabled, true);
  assert.equal(result.tmux_lag, true);
  assert.equal(result.attempts, 1);
});

test('route wrapper does not retry non-retryable command states', async () => {
  let calls = 0;
  const result = await routeVoiceTranscriptWithRetry({
    router: {
      async route() {
        calls += 1;
        return { routed: false, command: 'ship', reason: 'no_draft' };
      },
    },
    voiceManager: { playTTS: async () => { throw new Error('should not call'); } },
    logger: { warn() {} },
    result: { botName: 'custodes', userId: 'u', text: 'ship it' },
    maxAttempts: 3,
    retryDelayMs: 1,
  });

  assert.equal(calls, 1);
  assert.equal(result.routed, false);
  assert.equal(result.reason, 'no_draft');
  assert.equal(result.warning_sent, false);
});
