// discord-client.token-priority.test.js — pins keychain-first token resolution.
//
// Regression class (2026-07-17..20 outage recovery): the .env launchd cache
// had priority over the keychain, so a token rotated in the keychain could
// NEVER take effect while a stale cache line existed. Contract: keychain is
// canonical when token_source === 'keychain'; .env is only a fallback for
// locked/headless keychain; the JSON fallback file stays last.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { resolveBotToken } from './discord-client.ts';

const CONFIG = { token_source: 'keychain' };
const BOT = { keychain_service: 'discord-bot-token-inquisition' };
const ENV_KEY = 'DISCORD_BOT_TOKEN_INQUISITION';

test('a live keychain read beats a stale .env cache line', () => {
  const resolved = resolveBotToken(CONFIG, BOT, {
    readKeychainToken: () => 'fresh-keychain-token',
    envTokens: { [ENV_KEY]: 'stale-cached-token' },
  });
  assert.equal(resolved.token, 'fresh-keychain-token');
  assert.equal(resolved.source, 'keychain');
});

test('locked/unavailable keychain falls back to the .env cache', () => {
  const resolved = resolveBotToken(CONFIG, BOT, {
    readKeychainToken: () => { throw new Error('keychain locked'); },
    envTokens: { [ENV_KEY]: 'cached-token' },
  });
  assert.equal(resolved.token, 'cached-token');
  assert.equal(resolved.source, 'env');
});

test('empty keychain result falls back to the .env cache', () => {
  const resolved = resolveBotToken(CONFIG, BOT, {
    readKeychainToken: () => '',
    envTokens: { [ENV_KEY]: 'cached-token' },
  });
  assert.equal(resolved.token, 'cached-token');
  assert.equal(resolved.source, 'env');
});

test('a bot with no keychain entry anywhere still resolves from .env', () => {
  // imperial_guard's token lives only in .env on the current fleet.
  const resolved = resolveBotToken(CONFIG, { keychain_service: 'discord-bot-token-imperial-guard' }, {
    readKeychainToken: () => { throw new Error('not found'); },
    envTokens: { DISCORD_BOT_TOKEN_IMPERIAL_GUARD: 'env-only-token' },
  });
  assert.equal(resolved.token, 'env-only-token');
  assert.equal(resolved.source, 'env');
});

test('no source at all fails loud', () => {
  assert.throws(
    () => resolveBotToken(CONFIG, BOT, {
      readKeychainToken: () => { throw new Error('not found'); },
      envTokens: {},
    }),
    /No Discord bot token found/,
  );
});
