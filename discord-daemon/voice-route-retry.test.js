import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  isRetryableVoiceRouteFailure,
  routeVoiceTranscriptWithRetry,
} from './voice-route-retry.js';

test('retry predicate recognizes voice route failures', () => {
  assert.equal(isRetryableVoiceRouteFailure({ routed: false, reason: 'no_target' }), true);
  assert.equal(isRetryableVoiceRouteFailure({ routed: false, reason: 'voice_session_not_found' }), true);
  assert.equal(isRetryableVoiceRouteFailure(new Error('target not live')), true);
  assert.equal(isRetryableVoiceRouteFailure(new Error('voice session not found')), true);
  assert.equal(isRetryableVoiceRouteFailure({ routed: false, reason: 'no_draft' }), false);
});

test('route wrapper warns once and does not retry route failures', async () => {
  const calls = [];
  const tts = [];
  const router = {
    async route() {
      calls.push(Date.now());
      return { routed: false, reason: 'no_target' };
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
  assert.deepEqual(tts, ['voice route failed']);
  assert.equal(result.routed, false);
  assert.equal(result.reason, 'no_target');
  assert.equal(result.attempts, 1);
  assert.equal(result.warning_sent, true);
  assert.equal(result.retry_disabled, true);
});

test('route wrapper warns once and does not retry thrown route errors', async () => {
  let calls = 0;
  const tts = [];
  await assert.rejects(
    () => routeVoiceTranscriptWithRetry({
      router: {
        async route() {
          calls += 1;
          throw new Error('route timed out');
        },
      },
      voiceManager: { playTTS: async msg => tts.push(msg) },
      logger: { warn() {} },
      result: { botName: 'custodes', userId: 'u', text: 'hello' },
      maxAttempts: 3,
      retryDelayMs: 1,
    }),
    err => {
      assert.equal(err.attempts, 1);
      assert.equal(err.warning_sent, true);
      assert.equal(err.retry_disabled, true);
      return true;
    }
  );
  assert.equal(calls, 1);
  assert.deepEqual(tts, ['voice route failed']);
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
