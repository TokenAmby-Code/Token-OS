// message-store.stale.test.js — pins the pending-recovery staleness gate.
//
// Regression class (2026-07-20): a time-sensitive morning-supervisor briefing
// persisted at 05:19 sat in pending/ behind dead tokens; blind recovery would
// have replayed it to a human channel days late. Contract: recovery drops
// pending messages older than PENDING_MAX_AGE_MS (or of unknown age) instead
// of resending them.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { isStalePending, PENDING_MAX_AGE_MS } from './message-store.ts';

const NOW = Date.parse('2026-07-20T12:00:00.000Z');

test('a fresh pending message is replayed', () => {
  const msg = { persisted_at: new Date(NOW - 30_000).toISOString() };
  assert.equal(isStalePending(msg, NOW), false);
});

test('a pending message past the TTL is stale', () => {
  const msg = { persisted_at: new Date(NOW - PENDING_MAX_AGE_MS - 1_000).toISOString() };
  assert.equal(isStalePending(msg, NOW), true);
});

test('unknown age is stale — never replay a message of unknown vintage', () => {
  assert.equal(isStalePending({}, NOW), true);
  assert.equal(isStalePending({ persisted_at: 'not-a-date' }, NOW), true);
});

test('TTL stays tight enough that time-sensitive briefings cannot replay stale', () => {
  assert.ok(PENDING_MAX_AGE_MS <= 15 * 60_000);
});
