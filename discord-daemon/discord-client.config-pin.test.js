// discord-client.config-pin.test.js — pins the NAS config-path guard (PR C).
//
// Config resolution is pinned to <checkout>/config.json via import.meta.url.
// The guard makes the stale-NAS-path resurrection class fail LOUD at boot: a
// daemon whose checkout (and therefore config) resolves onto the NAS mount
// crashloops on stale config instead of silently running it
// (Mars/Tasks/discord-daemon-stale-nas-config-path-pin.md). Paths are
// canonicalized (realpath / `..` resolution) before the mount-root check.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, symlinkSync, rmSync, realpathSync } from 'fs';
import { join } from 'path';
import { tmpdir } from 'os';

import { assertConfigPathSafe } from './discord-client.ts';

test('local runtime checkout paths pass the guard', () => {
  for (const path of [
    '/Users/tokenclaw/runtimes/Token-OS/live/config.json',
    '/Users/tokenclaw/worktrees/Token-OS/wt-x/config.json',
    '/home/tokenclaw/runtimes/Token-OS/live/config.json',
  ]) {
    // Canonicalization may legitimately rewrite the spelling (live -> versioned
    // checkout symlinks), so pin only "returns a usable path, never throws".
    const canonical = assertConfigPathSafe(path);
    assert.equal(typeof canonical, 'string');
    assert.ok(canonical.length > 0, path);
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

test('.. traversal that resolves into a NAS mount is caught after canonicalization', () => {
  assert.throws(
    () => assertConfigPathSafe('/Users/tokenclaw/runtimes/../../../Volumes/Imperium/Token-OS/config.json'),
    /NAS/,
  );
  assert.throws(
    () => assertConfigPathSafe('/mnt/other/../imperium/Token-OS/config.json'),
    /NAS/,
  );
});

test('a local-looking symlink whose target lives under a mount root is caught', () => {
  // realpath the fixture dir up front: macOS tmpdir is itself a symlink
  // (/var -> /private/var), and the guard returns canonical paths.
  const dir = realpathSync(mkdtempSync(join(tmpdir(), 'config-pin-')));
  try {
    // Simulated mount root injected via the guard's mountRoots parameter —
    // real NAS roots cannot be fabricated in a test environment.
    const fakeMount = join(dir, 'fake-nas');
    mkdirSync(join(fakeMount, 'Token-OS'), { recursive: true });
    const target = join(fakeMount, 'Token-OS', 'config.json');
    writeFileSync(target, '{}');
    const link = join(dir, 'looks-local.json');
    symlinkSync(target, link);

    assert.throws(() => assertConfigPathSafe(link, [fakeMount]), /NAS/);
    // The same link is fine when the mount root list does not cover its target.
    assert.equal(assertConfigPathSafe(link, ['/definitely/elsewhere']), target);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('lookalike prefixes outside the mount roots are not blocked', () => {
  assert.doesNotThrow(() => assertConfigPathSafe('/Volumes/ImperiumBackup/Token-OS/config.json'));
});
