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

test('route wrapper warns loudly once then retries to success', async () => {
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

  assert.equal(calls.length, 2);
  assert.equal(tts.length, 1);
  assert.match(tts[0], /Retrying voice delivery/);
  assert.equal(result.routed, true);
  assert.equal(result.warning_sent, true);
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
