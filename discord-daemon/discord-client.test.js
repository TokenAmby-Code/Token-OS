import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  MAX_DISCORD_CONTENT_LENGTH,
  splitDiscordContent,
} from './discord-client.js';

test('splitDiscordContent leaves short messages unchanged', () => {
  assert.deepEqual(splitDiscordContent('hello'), ['hello']);
});

test('splitDiscordContent caps every chunk at the Discord content limit', () => {
  const chunks = splitDiscordContent('x'.repeat(MAX_DISCORD_CONTENT_LENGTH * 2 + 17));

  assert.equal(chunks.length, 3);
  assert.ok(chunks.every(chunk => chunk.length <= MAX_DISCORD_CONTENT_LENGTH));
  assert.equal(chunks.join(''), 'x'.repeat(MAX_DISCORD_CONTENT_LENGTH * 2 + 17));
});

test('splitDiscordContent prefers newline boundaries near the limit', () => {
  const first = 'a'.repeat(MAX_DISCORD_CONTENT_LENGTH - 25);
  const second = 'b'.repeat(100);
  const chunks = splitDiscordContent(`${first}\n${second}`);

  assert.equal(chunks.length, 2);
  assert.equal(chunks[0], first);
  assert.equal(chunks[1], second);
});
