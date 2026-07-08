// The daemon's /notify endpoint — the Discord leg of the notification
// fabric. token-api's dispatch_notify POSTs {message, level}; the daemon
// routes it to the notification channel. Fake-client fixture pattern from
// http-server-send.test.js. Graduated from the bounty lane in Terminus
// Stage 2 PR D.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHttpServer } from './http-server.ts';

function logger() {
  return { debug() {}, info() {}, warn() {}, error() {} };
}

function messageStore() {
  return { persist() {}, remove() {} };
}

function fakeDiscordClient(sent) {
  return {
    onMessage() {},
    onReaction() {},
    getStatus() { return { connected: true }; },
    async sendMessage(channelId, content, options = {}) {
      sent.push({ channelId, content, options });
      return {
        message_id: `m${sent.length}`,
        channel_id: channelId,
        timestamp: `2026-07-08T00:00:0${sent.length}.000Z`,
      };
    },
  };
}

function makeServer(sent, channels) {
  return createHttpServer(
    fakeDiscordClient(sent),
    messageStore(),
    {
      daemon_port: 0,
      channels,
    },
    logger(),
  );
}

const CHANNELS = {
  scratch: '123456789012345678',
  notifications: '876543210987654321',
};

test('POST /notify {message, level} sends to the notification channel', async () => {
  const sent = [];
  const server = makeServer(sent, CHANNELS);
  await server.start();
  try {
    const { port } = server.address();
    const resp = await fetch(`http://127.0.0.1:${port}/notify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'deploy finished', level: 'info' }),
    });
    assert.equal(resp.status, 200);
    assert.equal(sent.length, 1);
    assert.equal(sent[0].channelId, CHANNELS.notifications);
    assert.ok(sent[0].content.includes('deploy finished'));
  } finally {
    await server.stop();
  }
});

test('POST /notify without a message is a 400, nothing sent', async () => {
  const sent = [];
  const server = makeServer(sent, CHANNELS);
  await server.start();
  try {
    const { port } = server.address();
    const resp = await fetch(`http://127.0.0.1:${port}/notify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ level: 'info' }),
    });
    assert.equal(resp.status, 400);
    assert.equal(sent.length, 0);
  } finally {
    await server.stop();
  }
});

test('POST /notify with no notifications channel configured is a 500, nothing sent', async () => {
  const sent = [];
  const server = makeServer(sent, { scratch: '123456789012345678' });
  await server.start();
  try {
    const { port } = server.address();
    const resp = await fetch(`http://127.0.0.1:${port}/notify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'x' }),
    });
    assert.equal(resp.status, 500);
    assert.equal(sent.length, 0);
  } finally {
    await server.stop();
  }
});
