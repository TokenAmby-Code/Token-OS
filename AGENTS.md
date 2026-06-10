# Scripts - Generic Utility Scripts and Tools

**Location**: `/Volumes/Imperium/runtimes/token-os/live/` (deploy-owned runtime checkout — read-only for agents)
**Purpose**: Generic utility scripts, tools, and services
**Last Updated**: 2026-06-10
**Status**: Protected-main/local-CD topology (2026-06-10). Main syncs ff-only via the bare skeleton `/Volumes/Imperium/token-os.git`; services run from the detached runtime checkout above. **All agent work happens in branch worktrees under `~/worktrees/Token-OS/wt-<branch>`** (use `worktree-setup <branch> --project Token-OS` or `dispatch --worktree <branch>`). Never edit the runtime checkout — a dirty runtime aborts deploys. The old working checkout (the pre-cutover `Token-OS` tree) is archived as `Token-OS.legacy-20260610`; do not resurrect or push from it.

## Structure

```
Scripts/
├── token-api/              # Token API service (Mac + WSL satellite, port 7777)
├── Shell/                  # Shell scripts (system, safety, deployment)
├── cli-tools/              # CLI utilities
├── git/                    # Git utilities (gcom-enhanced.sh)
├── mobile/                 # Termux/mobile scripts
└── CLAUDE.md               # This file
```

## Key Services

### Token API (port 7777)

**Location**: `/Volumes/Imperium/runtimes/token-os/live/token-api/`
**Mac Service**: LaunchAgent `ai.openclaw.tokenapi`
**WSL Satellite**: systemd `token-satellite.service` (enforcement, `/restart`)
**Logs**: `~/.claude/token-api-stdout.log`, `~/.claude/token-api-stderr.log`

**Features**:
- TTS: macOS `say` command (voices: Daniel, Karen, Moira, Rishi)
- Sound: macOS `afplay` with system sounds (Glass, Ping, Tink, Hero)
- HTTP server on port 7777 (Mac + WSL satellite)
- Multi-device restart orchestration via `token-restart`
- Obsidian command execution
- Vault queries (stats, graph, search)

**Commands**:
- `token-restart`: Multi-device restart (Mac → WSL satellite → Ops browser refresh)
- `token-restart --status`: All-device health check
- `curl http://localhost:7777/health`: Health check

**See**: `/Volumes/Imperium/runtimes/token-os/live/token-api/CLAUDE.md` for details

### Discord Daemon (port 7779)

**Location**: `/Volumes/Imperium/runtimes/token-os/live/discord-daemon/` (code), `~/.discord-cli/` (config, logs, pending)
**Service**: LaunchAgent `ai.tokenclaw.discord` (KeepAlive)
**Logs**: `~/.discord-cli/logs/` (also `launchd-stdout.log` for console output)

Standalone Discord WebSocket daemon (discord.js v14) replacing OpenClaw's Discord gateway. Subscribes to ALL messages in 9 TokenClaw guild channels + operator DMs — no ping/mention required.

**CLI**: `discord send|read|ask|declare|dm|subscribe|status|channels`
**Management**: `discord-daemon start|stop|restart|status|logs`

**Integration**:
- Forwards incoming messages to Token API: `POST localhost:7777/api/discord/message`
- Token API logs them to `events` table as `discord_message`
- Persists outgoing messages to `~/.discord-cli/pending/` for crash recovery
- Bot token in macOS Keychain (`discord-bot-token`)

**Config**: `~/.discord-cli/config.json` (channel map, guild ID, ports)

### Deployment Scripts

**Executor Fleet**:
- `Shell/deploy-executor-fleet.sh`: Deploy 6 concurrent executors at 9 PM

**Safety**:
- `Shell/safety-snapshot.sh`: Create git snapshots
- `Shell/safety-rollback.sh`: Rollback to snapshot
- `Shell/safety-dashboard.sh`: Safety status viewer

**System**:
- `heartbeat-watchdog.sh`: Monitors task-worker cron, escalates if stale
- `Shell/cleanup-logs.sh`: Log rotation
- `Shell/system-dashboard.sh`: System status
- `Shell/vault-progress.sh`: Vault metrics

## Verification After Move

**Completed 2026-02-15**:
- [x] Scripts exist at /Volumes/Imperium/runtimes/token-os/live
- [x] Executable permissions preserved
- [x] Token API running from new location (port 7777)
- [x] Cron jobs updated with new paths
- [x] Watchdog script updated
- [x] Shell aliases updated (`token-restart`, `monitor`, `gcom`)
- [x] LaunchAgents updated (tokenapi, watchdog)

## Agent Access

Agents can:
- **READ**: All scripts for reference
- **EXECUTE**: Via Bash tool (with caution)
- **MODIFY**: With human approval (scripts are code, not data)
- **CREATE**: New scripts in appropriate subdirs

**Safety Rules**:
- NEVER delete existing scripts without backup
- NEVER modify LaunchAgent plists without stopping service first
- TEST new scripts before deploying to cron
- LOG script execution to appropriate log files

## Integration with Agent System

### Executor Fleet Deployment

```bash
/Volumes/Imperium/runtimes/token-os/live/Shell/deploy-executor-fleet.sh
```

Deploys 6 executors:
1. code-writer-01 (30p, every 5min)
2. file-operator-01 (20p, every 5min)
3. validator-01 (20p, every 10min)
4. researcher-01 (40p, every 15min)
5. obsidian-improver-01 (20p, every 6hr)
6. discord-improver-01 (20p, daily)

### Safety Operations

```bash
# Create snapshot before risky operation
/Volumes/Imperium/runtimes/token-os/live/Shell/safety-snapshot.sh pre-deployment

# Rollback if something breaks
/Volumes/Imperium/runtimes/token-os/live/Shell/safety-rollback.sh snapshot-pre-deployment-20260215

# Check safety status
/Volumes/Imperium/runtimes/token-os/live/Shell/safety-dashboard.sh
```

---

**Remember**: These are generic scripts, not OpenClaw-specific. Can be used with any agent system or manual workflows.
