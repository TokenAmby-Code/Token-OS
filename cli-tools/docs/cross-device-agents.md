# Cross-Device Agent Mobility

How agents spawn on remote machines and move sessions between devices.

## Overview

Three cross-device primitives:

| Primitive | Purpose |
|-----------|---------|
| `subagent --host` | Spawn a NEW agent on another device |
| `transplant --host` | MOVE an existing session to another device |
| `ssh-connect` | Interactive human SSH (redirect-on-exit, not for agents) |

## Headless vs Interactive

```
┌──────────────────────────────────────────────────────────┐
│                    HEADLESS (no terminal)                 │
│                                                          │
│  subagent --host wsl --claude "fix bug"                  │
│  transplant --host wsl /path                             │
│                                                          │
│  - Agent runs via nohup, detached from any terminal      │
│  - Output goes to log files on the remote machine        │
│  - SSH connects, launches, disconnects                   │
│  - Ideal for: CI-like tasks, background work, overnight  │
│  - Token API tracks the remote instance                  │
│                                                          │
├──────────────────────────────────────────────────────────┤
│                  INTERACTIVE (terminal-attached)          │
│                                                          │
│  subagent --host wsl --persona                           │
│  transplant --host wsl --relay /path                     │
│                                                          │
│  - Agent runs in a tmux pane (local or remote)           │
│  - Local terminal is SSH'd into remote machine           │
│  - You can see output, interact, approve tool calls      │
│  - Ideal for: debugging, supervised work, exploration    │
│  - tmux keeps the pane alive if SSH drops                │
│                                                          │
└──────────────────────────────────────────────────────────┘
```

## Why tmux Matters

Claude Code sessions run inside tmux panes. This makes transplant possible — the pane persists even when the Claude process inside it is killed and restarted.

- **Local transplant**: Kill Claude, restart in same pane with `--resume --fork-session`
- **Cross-device relay**: Kill local Claude, same pane runs `ssh -t` into remote Claude
- **Headless**: tmux is irrelevant (no terminal at all)

## Tool Comparison

| Action | Tool | Mode | Terminal? |
|--------|------|------|-----------|
| New agent, same machine | `subagent "prompt"` | async/blocking | No |
| New agent, remote machine | `subagent --host wsl "prompt"` | async/blocking | No |
| Interactive agent, remote | `subagent --host wsl --persona` | persona | Yes (SSH) |
| Move session, same machine | `transplant /new/dir` | — | Same pane |
| Move session, remote (headless) | `transplant --host wsl /dir` | headless | No |
| Move session, remote (relay) | `transplant --host wsl --relay /dir` | relay | Same pane via SSH |
| Human SSH between devices | `ssh-connect mac` | — | Yes |

## SSH Plumbing

### Host Mapping

Both tools use `host_to_ssh()` to map friendly names to SSH config aliases:

```
mac   → mini    (~/.ssh/config host alias for Mac Mini)
wsl   → wsl     (~/.ssh/config host alias for WSL via Tailscale)
phone → phone   (~/.ssh/config host alias for Termux via Tailscale)
```

### Why Raw SSH Instead of ssh-connect

`ssh-connect` adds redirect-on-exit behavior (useful for humans switching between devices). Agents don't want that — they want plain `ssh` for:
- One-shot command execution (`ssh target "subagent ..."`)
- Interactive passthrough (`ssh -t target "claude ..."`)
- File transfer (`scp transcript.jsonl target:path/`)

### Prerequisites

- SSH keys configured in `~/.ssh/config` for all targets
- `/Volumes/Imperium/Token-OS/cli-tools/bin/` in PATH on all devices (`scripts-sync` handles this)
- Claude Code installed at `~/.local/bin/claude` on target devices

## Session Transcript Transfer

When transplanting across devices, the session JSONL must be copied:

1. **Find source JSONL**: `~/.claude/projects/{encoded-source-dir}/{session-id}.jsonl`
2. **Encode target path**: Claude uses `tr '/.' '-'` to encode directory paths (e.g., `/home/token/project` becomes `-home-token-project`)
3. **SCP to remote**: `scp $source_jsonl $target:~/.claude/projects/{encoded-target-dir}/`
4. **Resume on remote**: `claude --resume $session_id --fork-session`

The `--fork-session` flag creates a new session branching from the transcript, so the original session file is preserved.

## Device Capabilities

| Device | Claude | Codex | OpenClaw | Notes |
|--------|--------|-------|----------|-------|
| Mac | Yes | Yes | Yes | Primary, all tools available |
| WSL | Yes | Yes | Yes | `scripts-sync` keeps in sync |
| Phone | Yes | No | Partial | Termux, limited PATH |

### Per-Device Notes

- **Mac** (mini): macOS, full toolchain, Token API primary server, LaunchAgent services
- **WSL** (wsl): Linux, Token API satellite, systemd services, shared Scripts via git
- **Phone** (phone): Termux/Android, basic Claude only, no codex, limited disk

## Examples

### Spawn a background agent on WSL

```bash
subagent --host wsl --claude "run the test suite and report failures"
# Output:
# Host: wsl (wsl)
# Job: claude-20260220-143022-12345
# Log: /home/token/.subagent/logs/claude-20260220-143022-12345.log
# Backend: claude
# PID: 67890
```

### Move current session to WSL (headless)

```bash
transplant --host wsl /home/token/project
# Output:
# ═══════════════════════════════════════════════════════════════
#  Session Transplant
# ═══════════════════════════════════════════════════════════════
#
#   From: /Users/tokenclaw/project
#   To:   wsl:/home/token/project
#
# ✓ SSH to wsl (wsl) OK
# → SCP transcript to wsl...
# ✓ Transcript copied to wsl
# → Spawning headless Claude on wsl...
# ✓ Remote agent spawned
#
#   Host: wsl (wsl)
#   Dir:  /home/token/project
#   Log:  ~/.subagent/logs/transplant-abc123.log
#   Session: abc123def456...
```

### Move current session to Mac (relay — interactive)

```bash
transplant --host mac --relay ~/other-project
# Local pane kills Claude, then SSH's into Mac running Claude with the same session
# You're now interacting with Claude on the Mac through your local terminal
```

### Drop into an interactive Claude on WSL

```bash
subagent --host wsl --persona
# exec ssh -t wsl "subagent --persona"
# You're now in Claude's TUI on WSL
```
