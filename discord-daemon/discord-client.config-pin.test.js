// discord-client.config-pin.test.js — pins the NAS config-path guard (PR C).
//
// Config resolution is pinned to <checkout>/config.json via import.meta.url.
// The guard makes the stale-NAS-path resurrection class fail LOUD at boot: a
// daemon whose checkout (and therefore config) resolves onto the NAS mount
// crashloops on stale config instead of silently running it
// (Mars/Tasks/discord-daemon-stale-nas-config-path-pin.md).

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { assertConfigPathSafe } from './discord-client.ts';

test('local runtime checkout paths pass the guard unchanged', () => {
  for (const path of [
    '/Users/tokenclaw/runtimes/Token-OS/live/config.json',
    '/Users/tokenclaw/worktrees/Token-OS/wt-x/config.json',
    '/home/tokenclaw/runtimes/Token-OS/live/config.json',
  ]) {
    assert.equal(assertConfigPathSafe(path), path);
  }
});

test('NAS-resident config paths fail loud', () => {
  for (const path of [
    '/Volumes/Imperium/Imperium-ENV/Token-OS/config.json',
    '/Volumes/Imperium/config.json',
    '/mnt/imperium/Imperium-ENV/Token-OS/config.json',
  ]) {
    assert.throws(() => assertConfigPathSafe(path), /NAS/, path);
  }
});

test('lookalike prefixes outside the mount roots are not blocked', () => {
  assert.equal(
    assertConfigPathSafe('/Volumes/ImperiumBackup/Token-OS/config.json'),
    '/Volumes/ImperiumBackup/Token-OS/config.json',
  );
});
