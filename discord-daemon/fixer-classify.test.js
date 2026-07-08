// fixer-classify.test.js — pins the benign-Realtime-lifecycle predicate contract.
import { test } from 'node:test';
import assert from 'node:assert/strict';

import { isBenignFixerError } from './fixer-classify.ts';

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

test('"maximum duration" without the 60-min phrase is NOT suppressed (narrow match)', () => {
  assert.equal(
    isBenignFixerError('realtime_error', 'exceeded maximum duration for audio buffer'),
    false
  );
});

test('buffer-commit error without a duration still pages the fixer', () => {
  assert.equal(isBenignFixerError('realtime_input_audio_buffer_commit', 'buffer too small'), false);
});

test('empty-buffer (0ms) commit response after local cleanup race is benign', () => {
  assert.equal(
    isBenignFixerError(
      'realtime_input_audio_buffer_commit',
      'Error committing input audio buffer: buffer too small. Expected at least 100ms of audio, but buffer only has 0.00ms of audio.'
    ),
    true
  );
  assert.equal(
    isBenignFixerError('realtime_input_audio_buffer_commit', 'buffer too small: 0ms of audio'),
    true
  );
});

test('buffer too small with a non-zero duration still pages the fixer', () => {
  assert.equal(
    isBenignFixerError(
      'realtime_input_audio_buffer_commit',
      'Error committing input audio buffer: buffer too small. Expected at least 100ms of audio, but buffer only has 250ms of audio.'
    ),
    false
  );
  // Fractional zeros inside a non-zero duration must not read as empty.
  assert.equal(
    isBenignFixerError('realtime_input_audio_buffer_commit', 'buffer too small: 250.0ms of audio'),
    false
  );
});

test('a 0ms mention without the buffer-too-small signature still pages the fixer', () => {
  assert.equal(isBenignFixerError('realtime_error', 'latency was 0ms somehow'), false);
});

test('empty/undefined inputs are not benign', () => {
  assert.equal(isBenignFixerError(undefined, ''), false);
});
