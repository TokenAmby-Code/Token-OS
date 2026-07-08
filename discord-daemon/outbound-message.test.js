import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  DISCORD_MESSAGE_CONTENT_LIMIT,
  sendChunkedDiscordContent,
  splitDiscordMessageContent,
} from './outbound-message.ts';

test('short content is not chunked or annotated', async () => {
  const calls = [];
  const result = await sendChunkedDiscordContent('short message', async (chunk) => {
    calls.push(chunk);
    return { message_id: 'm1', channel_id: 'c1', timestamp: '2026-07-03T00:00:00.000Z' };
  });

  assert.deepEqual(calls, ['short message']);
  assert.deepEqual(result, { message_id: 'm1', channel_id: 'c1', timestamp: '2026-07-03T00:00:00.000Z' });
});

test('content over Discord limit chunks in order without truncation', async () => {
  const content = [
    'intro line',
    'A'.repeat(1200),
    'middle line',
    'B'.repeat(1200),
    'final line',
  ].join('\n');

  const sent = [];
  const result = await sendChunkedDiscordContent(content, async (chunk, meta) => {
    sent.push({ chunk, meta });
    return {
      message_id: `m${meta.index + 1}`,
      channel_id: 'c1',
      timestamp: `2026-07-03T00:00:0${meta.index}.000Z`,
    };
  });

  assert.ok(sent.length > 1);
  assert.equal(sent.map(s => s.chunk).join(''), content);
  assert.ok(sent.every(s => s.chunk.length <= DISCORD_MESSAGE_CONTENT_LIMIT));
  assert.deepEqual(sent.map(s => s.meta.index), sent.map((_, i) => i));
  assert.deepEqual(sent.map(s => s.meta.count), sent.map(() => sent.length));
  assert.equal(result.chunked, true);
  assert.equal(result.chunk_count, sent.length);
  assert.equal(result.total_length, content.length);
  assert.deepEqual(result.message_ids, ['m1', 'm2']);
});

test('splitter prefers newline boundary before word boundary before hard limit', () => {
  const newlineContent = `${'a'.repeat(90)}\n${'b'.repeat(90)}`;
  assert.deepEqual(splitDiscordMessageContent(newlineContent, 100), [
    `${'a'.repeat(90)}\nb`,
    'b'.repeat(89),
  ]);

  const wordContent = `${'a'.repeat(60)} ${'b'.repeat(30)} ${'c'.repeat(30)}`;
  const wordChunks = splitDiscordMessageContent(wordContent, 100);
  assert.deepEqual(wordChunks, [
    `${'a'.repeat(60)} ${'b'.repeat(30)} c`,
    'c'.repeat(29),
  ]);

  const hardContent = 'x'.repeat(205);
  const hardChunks = splitDiscordMessageContent(hardContent, 100);
  assert.deepEqual(hardChunks.map(c => c.length), [100, 100, 5]);
  assert.equal(hardChunks.join(''), hardContent);
});

test('splitter avoids splitting inside code fences when an outside boundary exists', () => {
  const content = [
    'before',
    '```text',
    'inside code',
    '```',
    'after words that can split cleanly',
  ].join('\n');

  const chunks = splitDiscordMessageContent(content, 35);

  assert.equal(chunks.join(''), content);
  assert.ok(chunks.every(c => c.length <= 35));
  assert.equal(chunks[0], 'before\n```text\ninside code\n```\na');
});

test('splitter avoids trim-risk leading/trailing whitespace at chunk boundaries', () => {
  const content = Array.from(
    { length: 20 },
    (_, i) => `line-${String(i).padStart(2, '0')} ${'word '.repeat(20)}tail`
  ).join('\n');
  const chunks = splitDiscordMessageContent(content, 200);

  assert.equal(chunks.join(''), content);
  assert.ok(chunks.length > 1);
  assert.ok(chunks.every(c => c.length <= 200));
  assert.ok(chunks.every(c => !/^\s|\s$/.test(c)));
});

test('send metadata never allows a chunk above provider limit', async () => {
  const content = Array.from({ length: 5 }, (_, i) => `line-${i}-${'z'.repeat(700)}`).join('\n');
  const sent = [];
  await sendChunkedDiscordContent(content, async (chunk) => {
    sent.push(chunk);
    if (chunk.length > DISCORD_MESSAGE_CONTENT_LIMIT) {
      throw new Error('provider would reject this chunk');
    }
    return { message_id: `m${sent.length}`, channel_id: 'c1', timestamp: 'now' };
  });

  assert.equal(sent.join(''), content);
  assert.ok(sent.length > 1);
});
