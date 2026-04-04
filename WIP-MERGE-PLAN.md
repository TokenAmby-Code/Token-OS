# WIP Branch Merge Plan

**Source branch:** `wip/accumulated-2026-03-31`
**Target branch:** `master`
**Strategy:** Cherry-pick as grouped commits (not full merge — changes are logically separated)
**Created:** 2026-03-31

## Context

Unstaged changes were bulk-committed to `wip/accumulated-2026-03-31` as a single commit (`50d8241`). That reset the working tree to `master`, which deleted files that only existed in the accumulated work (notably `cli-tools/tmux/`). The NAS recycle bin caught the deletions.

This plan breaks the single wip commit into logical groups, cherry-picked onto master as clean individual commits.

## Groups

### 1. tmux — Config, binaries, workspace tools
**Status:** done (b569c28) — 17 files, tmux-status deferred (CIFS ghost)
**Priority:** CRITICAL (blocks monitor/tmux startup on WSL)

New files:
- `cli-tools/tmux/tmux-base.conf` — shared tmux config
- `cli-tools/tmux/tmux-portable-status.conf` — portable monitor status bar
- `cli-tools/tmux/mobile-shell-init.zsh` — mobile pane shell init

New binaries:
- `cli-tools/bin/tmux-claude-exit`
- `cli-tools/bin/tmux-context`
- `cli-tools/bin/tmux-dictate`
- `cli-tools/bin/tmux-grid-expand`
- `cli-tools/bin/tmux-mobile-keyboard`
- `cli-tools/bin/tmux-mode-toggle`
- `cli-tools/bin/tmux-pane-status`
- `cli-tools/bin/tmux-refresh-layout`
- `cli-tools/bin/tmux-reset`
- `cli-tools/bin/tmux-resume`
- `cli-tools/bin/tui-pane-guard`
- `cli-tools/bin/tx`
- `cli-tools/bin/wt-focus`
- `cli-tools/bin/portable-monitor`

Modified:
- `cli-tools/bin/tmux-status` (minor)
- `cli-tools/bin/tmux-workspace` (if changed)

---

### 2. lib — Machine identity & shared libraries
**Status:** done (64a7343)

New files:
- `cli-tools/lib/nas-path.sh` — shell machine identity ($IMPERIUM, $IMPERIUM_MACHINE)
- `cli-tools/lib/imperium_config.py` — Python machine identity
- `cli-tools/lib/git-remote.sh` — git remote helpers

---

### 3. claude-config — Shared Claude Code configuration
**Status:** done (a1ce2b6)

New directory tree:
- `claude-config/CLAUDE.md`
- `claude-config/setup.sh`
- `claude-config/settings.template.json`
- `claude-config/hooks/` (btw-capture, generic-hook, plan-gatekeeper, stop-validator)
- `claude-config/commands/` (openclaw, openclaw-cron)
- `claude-config/skills/` (deploy, enforce, fleet-pause, fleet-unpause, pr, session-plan, session-update, vault-canon, vault-mind)

---

### 4. token-api — Server, TUI, cron, stop-hook overhaul
**Status:** done (b84502f) — 3 files CIFS ghost-deleted on disk (pyproject.toml, stop_hook.py, uv.lock)

Modified:
- `token-api/main.py` — major expansion
- `token-api/token-api-tui.py` — major refactor
- `token-api/cron_engine.py`
- `token-api/stop_hook.py` — major refactor
- `token-api/corax_watchtower.py`
- `token-api/custodes_checkin.py`
- `token-api/fleet_dispatch_poc.py`
- `token-api/post_run_graph.py`
- `token-api/test_cron_engine.py`
- `token-api/token-api.service`
- `token-api/tts-studio.py`
- `token-api/tui-screenshot.py`
- `token-api/pyproject.toml`
- `token-api/uv.lock`

New:
- `token-api/init_db.py`
- `token-api/morning_launcher.py`
- `token-api/morning_session.py`
- `token-api/nas_mount.py`
- `token-api/tests/test_legion_synced.py`
- `token-api/AGENTS.md`

---

### 5. cli-tools/bin — Misc binary updates
**Status:** done (63c251a) — tools file via index update (CIFS ghost)

New:
- `cli-tools/bin/claude-cmd`
- `cli-tools/bin/enforce`
- `cli-tools/bin/mewgenics-capture`
- `cli-tools/bin/mewgenics-dedup`
- `cli-tools/bin/mewgenics-process`
- `cli-tools/bin/nas-env`
- `cli-tools/bin/return-trip-watcher`
- `cli-tools/bin/tailscale-check`
- `cli-tools/bin/tui-screenshot`
- `cli-tools/bin/victory`

Modified:
- `cli-tools/bin/deploy`, `deploy-mac`
- `cli-tools/bin/instance-name`, `instance-stop`, `instances-clear`
- `cli-tools/bin/macrodroid-*`
- `cli-tools/bin/pr-create`, `pr-merge`, `pr-review-loop`
- `cli-tools/bin/primarch`
- `cli-tools/bin/push-mobile`
- `cli-tools/bin/ssh-connect`
- `cli-tools/bin/stash`
- `cli-tools/bin/token-restart`
- `cli-tools/bin/tools`
- `cli-tools/bin/transplant` (major)
- `cli-tools/bin/tts`, `tts-skip`
- `cli-tools/bin/voice-chat`
- `cli-tools/bin/work-mode`
- `cli-tools/bin/worktree-setup`
- Various minor path rewrites

Removed:
- `cli-tools/bin/scripts-sync` (deleted)

---

### 6. Shell — Legacy scripts relocated
**Status:** done (55c8536)

Moved from `Shell/` → `cli-tools/Shell/`:
- cleanup-logs.sh, cron-dashboard.sh, deploy-executor-fleet.sh
- heartbeat-watchdog.sh, inbox-status.sh, system-dashboard.sh, vault-progress.sh

---

### 7. ahk — AutoHotkey scripts
**Status:** done (9c5b7a8)

New:
- `ahk/dial-scroll.ahk`
- `ahk/ring-remap-launcher.bat`
- `ahk/script-compiler.ahk`
- `ahk/voice-send-keys.ahk`

Modified:
- `ahk/audio-monitor.ahk`, `helper.ahk`, `monitor-launcher.ahk`, `quicknote.ahk`, `runjs.ahk`

---

### 8. mobile — Termux, MacroDroid, morning macros
**Status:** done (77c45bb)

New:
- `mobile/macros/constraint-probes.*`
- `mobile/macros/debug-logging-blocks.*`
- `mobile/macros/morning-setup.*`

Modified:
- `mobile/termux-bashrc-template` (major)
- `mobile/termux-properties-template`
- `mobile/AGENTS.md`, `mobile/macros/MACRODROID.md`

---

### 9. misc — Root-level & minor changes
**Status:** done (dca1b51) — skipped discord-daemon/node_modules (Mac symlink)

- `.gitignore`
- `AGENTS.md`, `STARTUP.md`, `VENV_ROLL_CALL.md`
- `Powershell/Setup-HeadlessTask.ps1`
- `cli-tools/directory-tags.yaml`
- `cli-tools/src/cli_tools/followup/cli.py`
- `cli-tools/src/cli_tools/subagents/prompts/tool_creator.md`
- `cli-tools/src/deploy/deploy-wrapper.sh`
- `discord-daemon/http-server.js`
- `discord-daemon/node_modules` (symlink?)

---

## Process

For each group:
1. Checkout master
2. `git checkout wip/accumulated-2026-03-31 -- <file-list>`
3. `git add` the files
4. `git commit` with a descriptive message
5. Update this doc: mark group status as `done`

## Residual Items

These were not merged and may need manual attention:

- **`cli-tools/bin/tmux-status`** — CIFS ghost prevented disk write. Minor change (adds `$TOKEN_API_URL` env var). Apply manually when CIFS clears.
- **`discord-daemon/node_modules`** — Mac-only symlink to `/Users/tokenclaw/discord-daemon-modules/node_modules`. Skipped intentionally.
- **`Shell/remove-date-prefix.sh`** — Existed on master's `Shell/` but wasn't in the wip's `cli-tools/Shell/` relocation. Likely dead code.
- **`heartbeat-watchdog.sh`** — Top-level copy deleted on wip (moved to `cli-tools/Shell/`). Already handled by group 6.

## CIFS Ghost Files

Several files hit a Synology NAS CIFS caching bug where the directory listing shows a file but all operations (read, write, rm, chmod, stat) fail with "No such file or directory". Workaround: rename the parent directory, or wait for CIFS cache to expire. Affected:
- `cli-tools/bin/tools` (worked around via `git update-index`)
- `cli-tools/bin/tmux-status` (deferred)
- `token-api/pyproject.toml`, `stop_hook.py`, `uv.lock` (committed in git, ghost on disk)
- `claude-config/hooks/generic-hook.sh` (worked around via parent rename)

## Notes

- The wip branch is a single commit (`50d8241`), so cherry-pick won't isolate groups — we used `git checkout <branch> -- <files>` instead.
- Some files have interdependencies (e.g., tmux bins depend on `lib/nas-path.sh`). Merged `lib` early (group 2).
- All 9 groups merged as of 2026-03-31. Branch `wip/accumulated-2026-03-31` can be deleted once residuals are resolved.
