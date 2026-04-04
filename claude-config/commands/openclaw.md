# OpenClaw Manager

Manage the OpenClaw agent OS: gateway, agents, cron, Discord, sessions.

## Architecture

- **Gateway**: LaunchAgent `ai.openclaw.gateway` on port 18789
- **Config**: `~/.openclaw/openclaw.json`
- **Workspace**: `~/.openclaw/workspace/`
- **Agent**: `main` (default, MiniMax M2.5 model)
- **Channel**: Discord (@TokenClaw)
- **Cron jobs**: `~/.openclaw/cron/jobs.json`
- **Sessions**: `~/.openclaw/agents/main/sessions/sessions.json`
- **Logs**: `~/.openclaw/logs/gateway.log`, `gateway.err.log`

## Quick Commands

### Health & Status

```bash
openclaw health                     # Gateway + Discord + agent health
openclaw doctor                     # Diagnose issues (--fix to repair)
openclaw status                     # Channel health + recent sessions
openclaw sessions                   # List all sessions
openclaw sessions --active 60       # Sessions active in last 60 min
openclaw logs                       # Gateway logs (full dump)
```

### Gateway Control

```bash
openclaw gateway install            # Install LaunchAgent (auto-start)
openclaw gateway start              # Start gateway
openclaw gateway stop               # Stop gateway
openclaw gateway restart            # Restart LaunchAgent
openclaw gateway uninstall          # Remove LaunchAgent
```

### Talk to Agent

```bash
openclaw agent --agent main --message "your message"
openclaw agent --agent main --session-id "custom-id" --message "..."
openclaw agent --agent main --message "..." --thinking medium
openclaw agent --agent main --message "..." --deliver  # Reply via Discord
```

### Cron Jobs

See [[openclaw-cron]] for full cron job management.

Quick: `openclaw cron list`, `openclaw cron run <name>`, `openclaw cron status`

### Messaging (Discord)

```bash
# Send message to Discord channel
openclaw message send --channel discord --target <channel-id> --message "text"

# Read recent messages
openclaw message read --channel discord --target <channel-id>

# React to message
openclaw message react --channel discord --target <channel-id> --message-id <id> --emoji "emoji"
```

### Config

```bash
openclaw config get <dot.path>      # Read config value
openclaw config set <dot.path> <value>  # Set config value
openclaw config unset <dot.path>    # Remove config value
openclaw configure                  # Interactive wizard
```

### Agents

```bash
openclaw agents list                # List configured agents
openclaw agents add                 # Add isolated agent (own workspace)
openclaw agents delete              # Remove agent
openclaw agents set-identity        # Update agent name/emoji/avatar
```

## Workspace Files (bootstrapped into every session)

| File | Purpose |
|------|---------|
| `AGENTS.md` | Operating instructions, environment info, CLI tools |
| `SOUL.md` | Persona, boundaries, tone |
| `TOOLS.md` | Tool notes (camera names, SSH, voices) |
| `IDENTITY.md` | Self-identity |
| `USER.md` | About the human |
| `HEARTBEAT.md` | Heartbeat task checklist |
| `BOOTSTRAP.md` | First-run guide (deleted after use) |
| `CLAUDE.md` | Workspace rules (NOT auto-bootstrapped) |

Edit workspace files: `~/.openclaw/workspace/<file>`

## Context Files (standby, for cron agents)

Located at `~/.openclaw/workspace/context/`:

| Path | Source |
|------|--------|
| `context/root/AGENTS.md` | `~/AGENTS.md` (copy) |
| `context/scripts/AGENTS.md` | `/Volumes/Imperium/Token-OS/AGENTS.md` (symlink) |
| `context/token-api/AGENTS.md` | `/Volumes/Imperium/Token-OS/token-api/AGENTS.md` (symlink) |
| `context/mobile/AGENTS.md` | `/Volumes/Imperium/Token-OS/mobile/AGENTS.md` (symlink) |
| `context/claw-env/AGENTS.md` | `~/Claw-ENV/AGENTS.md` (symlink) |
| `context/token-env/AGENTS.md` | `~/Token-ENV/AGENTS.md` (symlink) |

**Note**: `context/root/AGENTS.md` is a COPY (not symlink) because OpenClaw's realpath security rejects symlinks pointing outside the workspace. Update manually when `~/AGENTS.md` changes.

## Known Limitations

1. **bootstrap-extra-files hook**: Configured but doesn't inject files (possible bug in v2026.2.13). Environment info was instead appended to workspace `AGENTS.md` directly.
2. **No ancestor chain**: Unlike Claude Code, OpenClaw only reads from workspace root. No parent-directory traversal.
3. **CLAUDE.md not bootstrapped**: Only the 7 recognized basenames are auto-injected (AGENTS, SOUL, TOOLS, IDENTITY, USER, HEARTBEAT, BOOTSTRAP).
4. **Cron jobs share workspace**: All cron agents use the same workspace and AGENTS.md. Use explicit `cat context/<project>/AGENTS.md` in cron prompts for project-specific context.

## Common Tasks

### Restart after config change
```bash
openclaw gateway restart
```

### Check why agent isn't responding
```bash
openclaw health                     # Check gateway + Discord
openclaw doctor --fix               # Auto-repair common issues
openclaw logs | tail -50            # Recent gateway logs
```

### Update workspace context
```bash
# Edit directly
vim ~/.openclaw/workspace/AGENTS.md

# Or sync root config (if ~/AGENTS.md changed)
\cp ~/AGENTS.md ~/.openclaw/workspace/context/root/AGENTS.md
```

### Test agent behavior
```bash
openclaw agent --agent main --session-id "test-$(date +%s)" --message "your test prompt"
```
