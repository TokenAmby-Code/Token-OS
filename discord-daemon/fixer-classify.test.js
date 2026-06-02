// fixer-classify.test.js — pins the benign-Realtime-lifecycle predicate contract.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { isBenignFixerError } from './fixer-classify.js';

test('session_expired error code is benign regardless of message', () => {
  assert.equal(isBenignFixerError('session_expired', 'anything'), true);
});

test('60-min max-duration message is benign even without the dedicated code', () => {
  assert.equal(
    isBenignFixerError('realtime_error', 'Your session hit the maximum duration of 60 minutes'),
    true
  );
});

test('genuine websocket error still pages the fixer', () => {
  assert.equal(isBenignFixerError('realtime_websocket_error', 'websocket closed'), false);
});

test('buffer-commit error still pages the fixer', () => {
  assert.equal(isBenignFixerError('realtime_input_audio_buffer_commit', 'buffer too small'), false);
});

test('empty/undefined inputs are not benign', () => {
  assert.equal(isBenignFixerError(undefined, ''), false);
});
