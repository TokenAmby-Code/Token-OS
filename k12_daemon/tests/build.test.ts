// resolveGitSha: /health.git_sha is the box-restart deploy proof — it must be
// the real checkout SHA on a deployed build, env-stampable for overrides, and
// 'unknown' ONLY when there is genuinely no identity to report.

import { describe, expect, test } from 'bun:test';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { resolveGitSha } from '../src/build.ts';

const pkgDir = new URL('..', import.meta.url).pathname;

describe('resolveGitSha', () => {
  test('GIT_SHA env stamp wins over git resolution', () => {
    expect(resolveGitSha(pkgDir, { GIT_SHA: 'deadbeef' })).toBe('deadbeef');
  });

  test('blank GIT_SHA env falls through to git resolution', () => {
    const sha = resolveGitSha(pkgDir, { GIT_SHA: '   ' });
    expect(sha).not.toBe('unknown');
    expect(sha).toMatch(/^[0-9a-f]{40}$/);
  });

  test('resolves the actual checkout HEAD from the package dir', () => {
    const expected = Bun.spawnSync(['git', 'rev-parse', 'HEAD'], { cwd: pkgDir, stdout: 'pipe' })
      .stdout.toString()
      .trim();
    expect(resolveGitSha(pkgDir, {})).toBe(expected);
  });

  test('non-repo dir yields unknown', () => {
    const dir = mkdtempSync(join(tmpdir(), 'k12-build-test-'));
    try {
      expect(resolveGitSha(dir, {})).toBe('unknown');
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
