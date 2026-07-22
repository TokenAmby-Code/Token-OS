# Handoff — Cached CI/CD propagation for potentially-OFF nodes (ruling #9)

**Branch:** `cd-offline-node-propagation`  ·  **Repo:** `gh --repo TokenAmby-Code/Token-OS`
**Last update:** 2026-07-14 (context-checkpoint mid-pr-step)

## State: ALL CODE DONE. PR in flight, live-verify pending.

### Completed (committed to worktree, verified clean)
1. **Design spec** — `docs/cd-offline-node-propagation.md` (new).
2. **Hub cache** — `token-api/main.py`: `_CD_DEPLOY_RECORD_PATH`,
   `_cd_save_deploy_record`, `_cd_load_deploy_record` (~19510); persist inside
   `cd_restart` right after `services = body.get("services")`; tokenless
   `GET /api/cd/current` (record→LAUNCHED_GIT_SHA fallback). `py_compile` clean.
3. **WSL convergence** — `token-api/token-satellite.py`: CD block before
   `/runtime/refresh` (`_CD_BARE`, `_cd_hub_base` config-driven, `_cd_git`,
   `_cd_bare_main_tip`, `_cd_resolve_target` hub→GitHub, `_cd_changed_paths`,
   shared `_spawn_runtime_refresh`, `_cd_converge`, `_cd_converge_on_boot`,
   `POST /cd/converge`); `runtime_refresh` refactored onto shared spawn; boot
   thread in `startup_event`. All deps resolve; `py_compile` clean.
4. **Task 4 (THIS session)** — `cli-tools/bin/token-restart`: removed
   deploy-triggered WSL push:
   - `full_restart` SYNC_DID_ADVANCE branch: deleted unconditional
     `RESTART_WSL=true`+`REFRESH_WSL=true` (kept `RESTART_TOKENAPI=true`).
   - `map_changed_to_services`: deleted the
     `token-satellite.py|...|config/deskflow/*` arm entirely (falls to `*)`);
     dropped `RESTART_WSL/REFRESH_WSL` from the `uv.lock|pyproject.toml` arm
     (kept `RESTART_TOKENAPI=true`).
   - KEPT operator paths: `wsl-only`→`restart_wsl` (~2205); manual else-branch
     `RESTART_WSL=true` (~1948, REFRESH_WSL stays false).
   - Updated header/help/notes (WSL now converges on own boot, ruling #9).
   - `bash -n` clean; no orphaned RESTART_WSL/REFRESH_WSL refs.

### Decisions locked (do not relitigate)
- GitHub-main fallback so a node converges even when hub is ALSO off.
- Convergence = one idempotent check (no poll/timeout/debounce); self-heals to
  fixpoint on next boot (`already_current`). Verify externally via
  `/health.git_sha` parity.
- `/cd/converge` unauthenticated like `/restart`; target SHA only from trusted
  sources (hub record / GitHub main), never request body.
- Excise not shim; deploy-prod.yml unchanged (push still works when node ON).

## NEXT STEPS
1. **pr-step** running in bg (id was `bem2ykwer`). Wait for PR#/CodeRabbit/checks.
   If wedged after green check-run: `pr-step --force merge -y`.
2. **Post-merge:** hub `/health` git_sha == merged SHA.
3. **Live-verify WSL convergence** (bootstrap wrinkle — new pull mechanism can't
   deploy itself first time):
   a. Bootstrap WSL to merged SHA `M1` ONCE via existing push
      (`token-restart` Mac leg, or `curl -X POST .../runtime/refresh` with
      `TOKEN_SATELLITE_REFRESH_SECRET`). Confirm
      `curl http://100.66.10.74:7777/health` → git_sha==M1.
   b. Land trivial 2nd merge `M2`; WSL no longer pushed → stays M1.
   c. `curl -X POST http://100.66.10.74:7777/cd/converge` (or restart satellite
      to fire boot trigger). Watch WSL /health climb M1→M2 = PROOF.
   d. If WSL unreachable, report as BLOCKER with curl evidence — no done claim
      without proof.

## WSL facts
tailnet `100.66.10.74`, SSH alias `wsl`, user unit `token-satellite`, runtime
`/home/token/runtimes/token-os/live` (detached HEAD), refresh helper
`~/.local/bin/token-satellite-refresh`. Was UP at tip `524c77c6`.

## Report shape owed
Design-spec path+summary, files changed, PR#/SHAs, checks+CodeRabbit verdict,
live-verify evidence, mobile follow-on scope (design-only per spec), blockers,
session-doc update.
