import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createHttpServer } from './http-server.ts';
import { DISCORD_MESSAGE_CONTENT_LIMIT } from './outbound-message.ts';

function logger(logs = []) {
  return {
    debug(msg) { logs.push(['debug', msg]); },
    info(msg) { logs.push(['info', msg]); },
    warn(msg) { logs.push(['warn', msg]); },
    error(msg) { logs.push(['error', msg]); },
  };
}

function messageStore() {
  const persisted = [];
  const removed = [];
  return {
    persisted,
    removed,
    persist(id, data) { persisted.push({ id, data }); },
    remove(id) { removed.push(id); },
  };
}

function fakeDiscordClient(sent) {
  return {
    onMessage() {},
    onReaction() {},
    getStatus() { return { connected: true }; },
    async sendMessage(channelId, content, options = {}) {
      sent.push({ channelId, content, options });
      if (typeof content === 'string' && content.length > DISCORD_MESSAGE_CONTENT_LIMIT) {
        throw new Error('Invalid Form Body content[BASE_TYPE_MAX_LENGTH]: Must be 2000 or fewer in length.');
      }
      return {
        message_id: `m${sent.length}`,
        channel_id: channelId,
        timestamp: `2026-07-03T00:00:0${sent.length}.000Z`,
      };
    },
  };
}

async function withServer(client, store, logs, fn) {
  const server = createHttpServer(
    client,
    store,
    {
      daemon_port: 0,
      channels: { scratch: '123456789012345678' },
    },
    logger(logs),
  );
  await server.start();
  try {
    const { port } = server.address();
    await fn(`http://127.0.0.1:${port}`);
  } finally {
    await server.stop();
  }
}

test('/send preserves short-message behavior and pending cleanup', async () => {
  const sent = [];
  const store = messageStore();
  const logs = [];
  await withServer(fakeDiscordClient(sent), store, logs, async (url) => {
    const resp = await fetch(`${url}/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channel: 'scratch', content: 'hello' }),
    });
    assert.equal(resp.status, 200);
    assert.deepEqual(await resp.json(), {
      message_id: 'm1',
      channel_id: '123456789012345678',
      timestamp: '2026-07-03T00:00:01.000Z',
    });
  });

  assert.deepEqual(sent.map(s => s.content), ['hello']);
  assert.equal(store.persisted.length, 1);
  assert.deepEqual(store.removed, [store.persisted[0].id]);
});

test('/send preserves embed-only sends', async () => {
  const sent = [];
  const store = messageStore();
  const logs = [];
  const embeds = [{ title: 'embed title' }];

  await withServer(fakeDiscordClient(sent), store, logs, async (url) => {
    const resp = await fetch(`${url}/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channel: 'scratch', embeds }),
    });
    assert.equal(resp.status, 200);
    assert.equal((await resp.json()).message_id, 'm1');
  });

  assert.equal(sent.length, 1);
  assert.equal(sent[0].content, undefined);
  assert.deepEqual(sent[0].options.embeds, embeds);
  assert.equal(store.persisted.length, 1);
  assert.deepEqual(store.removed, [store.persisted[0].id]);
});

test('/send chunks long content in order and removes pending after handled delivery', async () => {
  const content = [
    'alpha',
    `${'bravo '.repeat(240)}tail`,
    'charlie',
    `${'delta '.repeat(240)}tail`,
    'omega',
  ].join('\n');
  assert.ok(content.length > DISCORD_MESSAGE_CONTENT_LIMIT);

  const sent = [];
  const store = messageStore();
  const logs = [];
  let body;
  await withServer(fakeDiscordClient(sent), store, logs, async (url) => {
    const resp = await fetch(`${url}/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channel: 'scratch', content, reply_to: 'reply-1' }),
    });
    assert.equal(resp.status, 200);
    body = await resp.json();
  });

  assert.ok(sent.length > 1);
  assert.equal(sent.map(s => s.content).join(''), content);
  assert.ok(sent.every(s => s.content.length <= DISCORD_MESSAGE_CONTENT_LIMIT));
  assert.deepEqual(sent.map(s => s.channelId), sent.map(() => '123456789012345678'));
  assert.equal(sent[0].options.reply_to, 'reply-1');
  assert.ok(sent.slice(1).every(s => !s.options.reply_to));
  assert.equal(store.persisted.length, 1);
  assert.deepEqual(store.removed, [store.persisted[0].id]);
  assert.equal(body.chunked, true);
  assert.equal(body.chunk_count, sent.length);
  assert.equal(body.max_chunk_length, Math.max(...sent.map(s => s.content.length)));
  assert.deepEqual(body.message_ids, sent.map((_, i) => `m${i + 1}`));
  assert.ok(logs.some(([, msg]) => String(msg).includes('discord_outbound_send')));
});

test('/send removes pending on terminal Discord content validation failure', async () => {
  const store = messageStore();
  const logs = [];
  const client = {
    onMessage() {},
    onReaction() {},
    getStatus() { return { connected: true }; },
    async sendMessage() {
      throw new Error('Invalid Form Body content[BASE_TYPE_MAX_LENGTH]: Must be 2000 or fewer in length.');
    },
  };

  await withServer(client, store, logs, async (url) => {
    const resp = await fetch(`${url}/send`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ channel: 'scratch', content: 'provider rejects this' }),
    });
    assert.equal(resp.status, 500);
    const body = await resp.json();
    assert.match(body.error, /Must be 2000 or fewer/);
  });

  assert.equal(store.persisted.length, 1);
  assert.deepEqual(store.removed, [store.persisted[0].id]);
});
