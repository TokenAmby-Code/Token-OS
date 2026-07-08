// Bounty: the daemon grows a /notify endpoint — the Discord leg of the
// notification fabric (Terminus Stage 2 PR D). token-api's dispatch_notify
// POSTs {message, level} here; the daemon routes it to the notification
// channel. Fake-client fixture pattern from ../http-server-send.test.js.

import assert from 'node:assert/strict';
import { bounty } from './bounty.js';
import { createHttpServer } from '../http-server.ts';

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

bounty('POST /notify {message, level} sends to the notification channel', async () => {
  const sent = [];
  const server = createHttpServer(
    fakeDiscordClient(sent),
    messageStore(),
    {
      daemon_port: 0,
      channels: {
        scratch: '123456789012345678',
        notifications: '876543210987654321',
      },
    },
    logger(),
  );
  await server.start();
  try {
    const { port } = server.address();
    const resp = await fetch(`http://127.0.0.1:${port}/notify`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: 'deploy finished', level: 'info' }),
    });
    assert.equal(resp.status, 200); // 404 today == open bounty
    assert.equal(sent.length, 1);
    assert.ok(sent[0].content.includes('deploy finished'));
  } finally {
    await server.stop();
  }
});
