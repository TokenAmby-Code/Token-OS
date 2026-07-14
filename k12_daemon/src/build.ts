// Build identity resolution. Mirrors token-api's LAUNCHED_GIT_SHA doctrine:
// the SHA is captured ONCE at process boot so it reflects the code THIS process
// loaded, and cannot drift if the runtime checkout advances under a still-running
// daemon. /health.git_sha is the box-restart deploy proof, so 'unknown' after a
// deployed build is a defect, not a default.

/**
 * Resolve the git SHA of the checkout this process is running from.
 * Precedence: GIT_SHA env (explicit deploy stamp) → `git rev-parse HEAD` in
 * `repoDir` → 'unknown'. Only a full 40-hex SHA is trusted from git output.
 */
export function resolveGitSha(repoDir: string, env: Record<string, string | undefined> = process.env): string {
  const stamped = env.GIT_SHA?.trim();
  if (stamped) return stamped;
  try {
    const proc = Bun.spawnSync(['git', 'rev-parse', 'HEAD'], {
      cwd: repoDir,
      stdout: 'pipe',
      stderr: 'ignore',
      timeout: 5_000,
    });
    const sha = proc.stdout.toString().trim();
    if (proc.exitCode === 0 && /^[0-9a-f]{40}$/.test(sha)) return sha;
  } catch {
    // git missing or repoDir unreadable — fall through to 'unknown'.
  }
  return 'unknown';
}
