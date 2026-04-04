# Venv Roll Call

## The Problem

Scripts and projects live on NAS volumes (Imperium, Civic) shared between Mac and WSL.
NAS filesystems (SMB) **cannot host Python venvs** because:
- No symlink support (uv/venv uses symlinks to the Python binary)
- Compiled `.so`/`.dylib` files are platform-specific — a macOS venv won't run on Linux

Venvs must live on **local filesystems**, one per project per machine.

## Machines

| Machine | OS | Tailscale IP | NAS Mounts |
|---|---|---|---|
| tokens-mac-mini | macOS | 100.95.109.23 | `/Volumes/Imperium`, `/Volumes/Civic` |
| tokenpc-1 (WSL) | Linux | 100.66.10.74 | `/mnt/imperium`, `/mnt/civic` |

## Projects Requiring Venvs

### 1. token-api (Imperium)
- **NAS path (Mac):** `/Volumes/Imperium/Token-OS/token-api/`
- **NAS path (WSL):** `/mnt/imperium/Token-OS/token-api/`
- **Python:** >=3.11, managed by `uv`
- **Key deps:** fastapi, uvicorn, langgraph, apscheduler, rich
- **WSL local venv:** `~/.local/venvs/token-api`
- **Mac local venv:** TBD (Mac may use worktrees — see below)
- **How to sync:** `UV_PROJECT_ENVIRONMENT=~/.local/venvs/token-api uv sync` from the project dir
- **Who runs the server:** Mac only (launchd service `ai.openclaw.tokenapi`, port 7777)
- **WSL usage:** TUI client only (`monitor` alias), connects to Mac via Tailscale (`TOKEN_API_URL=http://100.95.109.23:7777` in `.env`)

### 2. askcivic / ProcurementAgentAI (Civic)
- **NAS bare repo:** `/mnt/civic/askcivic.git`
- **Worktrees:** `/mnt/civic/askcivic.worktrees/` (WSL), Mac equivalent TBD
- **Python:** >=3.11, managed by `uv`
- **Key deps:** fastapi, google-cloud-*, gunicorn, httpx, streamlit, langchain
- **Venv strategy:** Already uses `UV_PROJECT_ENVIRONMENT=.venv` in its Makefile with `UV_LINK_MODE=copy` — this creates a `.venv` inside each worktree using copies instead of symlinks, which **works on NAS**
- **Mac also uses worktrees:** `/Users/tokenclaw/ProcAgentDir/ProcurementAgentAI`

## How uv Manages Non-Default Venv Locations

The key environment variable is `UV_PROJECT_ENVIRONMENT`:
```bash
# Tell uv to use a venv at a specific path instead of ./.venv
UV_PROJECT_ENVIRONMENT=~/.local/venvs/token-api uv sync
UV_PROJECT_ENVIRONMENT=~/.local/venvs/token-api uv run python main.py
```

Other useful vars:
- `UV_LINK_MODE=copy` — use file copies instead of symlinks (needed for NAS)
- `UV_NO_SYNC=1` — skip auto-sync on `uv run` (faster when deps haven't changed)

## Current State (2026-03-13)

### WSL
- [x] `~/.local/venvs/token-api` — created, deps installed, working
- [x] `monitor` alias updated to set `UV_PROJECT_ENVIRONMENT`
- [x] `.env` updated with `TOKEN_API_URL=http://100.95.109.23:7777` (Mac via Tailscale)
- [x] `token-restart` — updated for NAS era. Git sync / `scripts-sync` / `--no-push` removed (all devices read from NAS). `--from <dir>` still works for plist updates. Core flow: Mac launchctl restart → WSL satellite restart → phone TUI signal.
- [ ] systemd service (`token-api.service`) — currently disabled, uses `/usr/bin/python3` directly (not uv), WorkingDirectory points to `/mnt/imperium/Token-OS/token-api` (old symlink path). **Mac perspective:** Same pattern as Mac's LaunchAgent fix — needs ExecStart pointed to `~/.local/venvs/token-api/bin/python` and WorkingDirectory to `/mnt/imperium/Token-OS/token-api`.
- [ ] **`token-satellite.service` — crash-looping (exit 203/EXEC)**. Service is `enabled` but ExecStart points to `/mnt/imperium/Token-OS/token-api/.venv/bin/python` which doesn't exist. The NAS `.venv` at `/mnt/imperium/Token-OS/token-api/.venv/` is empty (just CACHEDIR.TAG, empty `bin/`). Needs the same fix as token-api.service: point at local venv `~/.local/venvs/token-api` and update WorkingDirectory to NAS path. Satellite runs on port 7777 and is the Windows execution arm (AHK scripts, TTS, app enforcement). **Currently blocking: pedal-enter feature, all satellite-dependent functionality.**
- [x] askcivic worktrees — `worktree-setup` creates worktrees on local disk (`~/worktrees/askCivic/wt-<branch>`), `uv sync` runs there and creates `.venv` on local FS. No NAS venv issues.

### Mac
- [x] token-api — Mac now uses worktree model too: `~/worktrees/Scripts/wt-master/token-api`. `uv sync --project token-api` runs during `worktree-setup` via `SYNC_SUBDIR=token-api` in `Scripts.conf`. Venv is at `~/worktrees/Scripts/wt-master/token-api/.venv` (local disk). LaunchAgent pointed at worktree via `token-restart --from`.
- [x] askcivic — Mac worktrees at `~/worktrees/askCivic/wt-main`. Created via `worktree-setup main --existing`. Venv on local disk inside worktree.
- [x] `deploy-mac web` — Mac-native frontend deploy working. No WSL dependency. Uses Homebrew gcloud/gsutil (`/opt/homebrew/bin/`). See "Mac Frontend Deploy Pipeline" section below.
- [x] gcloud SDK — installed via `brew install --cask google-cloud-sdk`. Authenticated with dev SA key (`deploy/dev-service-account.json`). `CLOUDSDK_PYTHON` set to `/opt/homebrew/opt/python@3.13/bin/python3.13`.

## Worktree Model & Venvs

The worktree system sidesteps the NAS venv problem entirely:

- **Bare repo** lives on NAS (`/mnt/civic/askcivic.git`, `/Volumes/Civic/scripts.git`)
- **Worktrees** live on **local disk** (`~/worktrees/<project>/wt-<branch>/`)
- `uv sync` runs inside the worktree → `.venv` is on local disk → no symlink or `.so` issues
- Each machine has its own native `.venv` with platform-correct binaries
- `worktree-sync export/import` transfers branches between machines (dirty state preserved via sync commit)

For monorepos like Scripts/token-api, `SYNC_SUBDIR=token-api` in the `.conf` file tells `worktree-setup` to run `uv sync --project token-api` instead of at the repo root.

## Mac Frontend Deploy Pipeline (`deploy-mac`)

**Status:** Working (2026-03-13). Tested through hello world 1 → 2 → 3 on dev.askcivic.com.

**What it is:** Self-contained bash script (`cli-tools/bin/deploy-mac`) that bypasses the Makefile entirely. The Makefile's `deploy-web` target has a split-shell variable bug where `$BUCKET` set in one recipe line is empty in the next — unfixable without rewriting the Makefile. `deploy-mac` inlines all logic in a single bash process.

**Known limitations (non-blocking):**
- `gsutil web set` fails (dev SA lacks `storage.buckets.update`) — SPA routing already configured, skipped with warning
- CDN invalidation skipped (dev SA lacks `compute.urlMaps.list`) — content propagates anyway (GCS generation match verified at 0s in all tests)
- gsutil `sys.maxint` bug: gsutil 5.36 + Python 3.13+ triggers `sys.maxint` (removed in Py3) in multiprocessing error paths. Worked around with `parallel_process_count=1` for rsync and `find`+`cp` loop for non-asset/non-HTML files

**Agent friction notes:**
- Claude Code's Bash tool runs in zsh with custom `cd` function that breaks in non-interactive context. Must use `bash -c 'cd /path && command'` to invoke deploy-mac from the correct directory
- `env -C` not available on macOS — cannot use it as an alternative

## Stale Artifacts to Clean Up
- `/mnt/imperium/Token-OS/token-api/.venv.old` — renamed macOS venv with darwin `.so` files that can't be deleted due to NAS stale file handles. Try again later or delete from Mac side.
- `/Users/tokenclaw/ProcAgentDir/ProcurementAgentAI` — old Mac checkout (pre-worktree). Can be removed once Mac is fully on worktree model.

### 3. cli-tools (Imperium)
- **NAS path (Mac):** `/Volumes/Imperium/Token-OS/cli-tools/`
- **Python:** >=3.11, managed by `uv`
- **Key deps:** click, rich, python-dotenv, requests, asyncpg, pyyaml
- **Venv strategy:** `uv run --directory` creates `.venv` inside the cli-tools dir on first use. No worktree needed — this is a standalone tool project.
- **Mac venv:** `/Volumes/Imperium/Token-OS/cli-tools/.venv` (created by `uv run` on NAS — works because Mac mounts support symlinks via SMB)
- **Bug fixed (2026-03-16):** Entry point scripts (`bin/cloud-logs`, `bin/db-query`, `bin/db-migrate`, `bin/time-convert`) used `uv run --project "$HOME/Scripts/cli-tools"` which only tells uv where to find `pyproject.toml` but does NOT change the working directory. uv then discovers `.venv` by walking up from cwd. When invoked from a worktree with its own `.venv`, uv used the worktree's venv (wrong deps, wrong packages). Fix: changed to `uv run --directory "$CLI_TOOLS_DIR"` where `CLI_TOOLS_DIR` is resolved from `$(dirname "$0")/..`. The `--directory` flag changes cwd before running, so uv finds the correct `.venv`.

## Commands Quick Reference

```bash
# WSL: Run monitor TUI (already aliased)
monitor

# WSL: Manually sync token-api venv
cd /mnt/imperium/Token-OS/token-api && UV_PROJECT_ENVIRONMENT=~/.local/venvs/token-api uv sync

# WSL: Run arbitrary token-api script
cd /mnt/imperium/Token-OS/token-api && UV_PROJECT_ENVIRONMENT=~/.local/venvs/token-api uv run python <script.py>

# AskCivic: Sync from within a worktree (copy mode, works on NAS)
cd /mnt/civic/askcivic.worktrees/<branch> && UV_PROJECT_ENVIRONMENT=.venv UV_LINK_MODE=copy uv sync --frozen

# Mac: Deploy frontend (from within askCivic worktree)
cd ~/worktrees/askCivic/wt-main && deploy-mac web        # dev
cd ~/worktrees/askCivic/wt-main && deploy-mac web --prod  # prod
```

## Cross-Machine Friction Log (2026-03-13)

Hard-won lessons from getting Mac deploy pipeline working:

1. **Shared bare repo, divergent working trees.** Mac and WSL share git refs via the Civic NAS bare repo. A Mac commit updates `HEAD` immediately, and WSL's `git log` sees it. But WSL's **working tree files are NOT updated** — `git merge main` says "Already up to date" because the ref matches. Must `git reset --hard HEAD` to force file refresh. This caused a deploy to push stale content (old heading instead of "hello world").

2. **Worktree gitdir cross-references are machine-specific.** The bare repo's `worktrees/<name>/gitdir` file and the worktree's `.git` file must reference each other. When a worktree is created on WSL, both paths are Linux paths. To use the same worktree entry from Mac, both files need Mac paths. There's no portable way to do this — it's a manual fixup when transferring worktrees between machines.

3. **macOS bash is 3.2 (GPLv2).** `set -u` + empty arrays = `unbound variable`. `sed -i` requires explicit empty extension (`sed -i ''`). `env -C` doesn't exist. Scripts targeting both platforms need defensive patterns.

4. **gsutil 5.36 is broken on Python 3.13+.** The `sys.maxint` attribute was removed in Python 3. gsutil's multiprocessing code references it in error paths. `parallel_process_count=1` reduces but doesn't eliminate the surface area. For reliability, avoid `gsutil -m rsync` on Mac and use `find` + `gsutil cp` loops instead.

5. **GCP service account permissions vary.** The dev SA (`pax-chat-dev-sa`) has object read/write but not bucket admin (`storage.buckets.update`) or compute admin (`compute.urlMaps.list`). CDN invalidation and SPA routing config must be non-fatal on Mac deploys. WSL deploy may use a broader-scoped auth context.
