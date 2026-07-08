// fleet-status publisher — feature-flagged poll of the ops read-model,
// edited in place into the fleet-status channel (Terminus Stage 2 PR D).

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createFleetStatusPublisher } from './fleet-status-publisher.ts';

const FLEET_CHANNEL = '111222333444555666';

function logger() {
  return { debug() {}, info() {}, warn() {}, error() {} };
}

function fakeClient(calls) {
  return {
    async sendMessage(channelId, content) {
      calls.push({ kind: 'send', channelId, content });
      return { message_id: `m${calls.length}`, channel_id: channelId };
    },
    async editMessage(channelId, messageId, content) {
      calls.push({ kind: 'edit', channelId, messageId, content });
      return { message_id: messageId, channel_id: channelId };
    },
  };
}

function opsState(instances) {
  return {
    surface: 'ops',
    contract_version: 'ops-state.v1',
    generated_at: '2026-07-08T12:00:00Z',
    instances: {
      active: instances,
      counts: { active: instances.length, stale: 0 },
    },
  };
}

function fetchReturning(states) {
  let i = 0;
  return async () => {
    const state = states[Math.min(i, states.length - 1)];
    i += 1;
    return { ok: true, async json() { return state; } };
  };
}

function makePublisher(calls, states, configOverrides = {}) {
  return createFleetStatusPublisher({
    client: fakeClient(calls),
    config: {
      token_api_port: 7777,
      fleet_status_enabled: true,
      channels: { 'fleet-status': FLEET_CHANNEL },
      ...configOverrides,
    },
    logger: logger(),
    fetchImpl: fetchReturning(states),
  });
}

test('first tick sends, changed state edits in place, unchanged state is a no-op', async () => {
  const calls = [];
  const publisher = makePublisher(calls, [
    opsState([{ id: 'custodes', display_name: 'Custodes', status: 'processing' }]),
    opsState([{ id: 'custodes', display_name: 'Custodes', status: 'stopped' }]),
    opsState([{ id: 'custodes', display_name: 'Custodes', status: 'stopped' }]),
  ]);

  const first = await publisher.tick();
  assert.deepEqual(first, { changed: true, edited: false });
  assert.equal(calls[0].kind, 'send');
  assert.equal(calls[0].channelId, FLEET_CHANNEL);
  assert.ok(calls[0].content.includes('Custodes'));

  const second = await publisher.tick();
  assert.deepEqual(second, { changed: true, edited: true });
  assert.equal(calls[1].kind, 'edit');
  assert.equal(calls[1].messageId, publisher.messageId);

  const third = await publisher.tick();
  assert.deepEqual(third, { changed: false });
  assert.equal(calls.length, 2);
});

test('a failed edit (aged-out message) falls back to a fresh send', async () => {
  const failingCalls = [];
  const failing = createFleetStatusPublisher({
    client: {
      async sendMessage(channelId, content) {
        failingCalls.push({ kind: 'send', channelId, content });
        return { message_id: `m${failingCalls.length}` };
      },
      async editMessage() {
        throw new Error('Unknown Message');
      },
    },
    config: {
      token_api_port: 7777,
      fleet_status_enabled: true,
      channels: { 'fleet-status': FLEET_CHANNEL },
    },
    logger: logger(),
    fetchImpl: fetchReturning([
      opsState([{ id: 'a', status: 'processing' }]),
      opsState([{ id: 'a', status: 'stopped' }]),
    ]),
  });
  await failing.tick();
  const result = await failing.tick();
  assert.deepEqual(result, { changed: true, edited: false });
  assert.equal(failingCalls.length, 2);
  assert.equal(failingCalls[1].kind, 'send');
});

test('publisher is off without the feature flag or channel', () => {
  const calls = [];
  const noFlag = makePublisher(calls, [], { fleet_status_enabled: false });
  assert.equal(noFlag.enabled, false);
  assert.equal(noFlag.start(), false);

  const noChannel = makePublisher(calls, [], { channels: {} });
  assert.equal(noChannel.enabled, false);
  assert.equal(noChannel.start(), false);
});

test('contract violations throw out of tick (caller logs, loop survives)', async () => {
  const calls = [];
  const publisher = makePublisher(calls, [{ not: 'an ops state' }]);
  await assert.rejects(() => publisher.tick());
  assert.equal(calls.length, 0);
});
