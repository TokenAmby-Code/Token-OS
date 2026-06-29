# Token-OS CI/CD Reference

Token-OS is a local hot runtime, not a remote production service.

## Invariants

- No remote prod deploy path exists for Token-OS.
- Live runtime updates go through the local webhook/sync path and service-specific restart tooling.
- Runtime desync is always an error: live code must match the intended commit/SHA after restart.
- Do not run broad tmux restarts or mutate live virtualenvs as a deploy shortcut.

## Safe Path

1. Land code in the worktree and merge through the normal PR path.
2. Sync/restart Token-API with the sanctioned local path:

   ```bash
   token-restart --sync
   ```

3. Verify the hot service:

   ```bash
   curl -sf "$TOKEN_API_URL/health"
   token-ping --raw /health 2>/dev/null || true
   ```

4. Verify the live SHA/path if the service exposes it. If live state does not match the intended commit, treat it as failed deployment/desync and investigate before reporting success.

## Do Not

- Do not call this a production deploy.
- Do not use `tx restart` for Token-API deployment; it can wipe live tmux state.
- Do not `uv sync` or rebuild virtualenvs in the live runtime.
- Do not hide sync failures behind a green local test run.
