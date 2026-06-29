# Centralized CLI Tools

This repository contains centralized Python CLI tools that are available across all ProcurementAgentAI projects.

## Features

- **Automatic venv management**: Tools automatically create and manage their own virtual environment
- **Non-interactive shell friendly**: Works in AI agent environments without requiring shell initialization
- **uv-based**: Uses `uv` for fast, reliable package management
- **Centralized commands**: All CLI tools accessible from any repository

## Available Commands

### Deploy (`deploy`)

Autonomous deployment with mutex coordination. Supports cloud and local deployments with target-based syntax.

**Quick Start:**
```bash
deploy                    # Dev async (default)
deploy prod               # Production async
deploy -b                 # Dev blocking
deploy local              # Local server with ngrok
deploy local -b           # Local with health monitoring
deploy debug              # Local with debugger
```

See `deploy --help` for full documentation.

### Test (`test`)

Smart testing utility for local development with automatic detection of input type and server state.

**Quick Start:**
```bash
test                      # Health check only
test "hello world"        # Send Google Chat message
test /api/status          # Test HTTP endpoint
test "hello" --one-shot   # Full test cycle
test "hello" --dry-run    # Show payload
```

See `test --help` for full documentation.



### NAS-safe search (`nas-grep`)

`nas-grep` is the agent-safe search wrapper for NAS/vault paths such as `/Volumes/Imperium` and `/mnt/imperium`. It prefers `rg` when available, otherwise uses a Python fallback, and always applies conservative excludes, bounded result/file limits, low-priority execution, and a shared NAS lease for known NAS mounts. Use it instead of raw broad `grep`, `rg`, or `ugrep` against the NAS.

```bash
nas-grep "session_id" /Volumes/Imperium/Terra
nas-grep -F "literal text" /mnt/imperium/Vault --max-results 50
nas-grep --dry-run "pattern" /Volumes/Imperium
```

### Shared agent skills (`skills-sync`)

`skills-sync --check` verifies that the NAS-backed canonical skills in `claude-config/skills` are visible to Claude and Codex and resolve to the same real paths. `skills-sync --install` repairs symlinks only: it keeps `~/.codex/skills/.system` intact and exposes shared skills under both `~/.agents/skills` and `~/.codex/skills`.

```bash
skills-sync --check
skills-sync --install
skills-sync --check --json
```

### Live agent pane prompt delivery (`agent-cmd`)

`agent-cmd` is the canonical command for submitting text into an existing Claude/Codex prompt pane. It normalizes payloads and uses the hardened literal-send + delayed double-submit sequence through `tmuxctl`; `claude-cmd` remains as a compatibility wrapper. Use this for live prompt injection instead of raw `tmux send-keys ... Enter`. See `cli-tools/docs/pane-prompt-delivery.md`.

### Managed tmux stack dispatch (`tmuxctl stack`)

`tmuxctl` is the single pane-backed dispatch primitive for managed stack pages. Use `tmuxctl stack add legion` to allocate a typed worker pane, or `tmuxctl stack dispatch legion --command ...` to allocate and launch in one step. Entry points such as `dispatch`, Prefix+Space (`tmux-legion-prompt`), Golden Throne resume fallback, `work-loop`, and pane demotion route through this tmuxctl stack code instead of raw `tmux split-window`. See `cli-tools/docs/managed-stack-dispatch.md`.

### tmux focus guard (`tmuxctl focus_guard`)

Automation must never steal the operator's tmux focus. Use `preserve_focus(...)` around tmux cleanup/layout paths that can change selection, and use `tmuxctl allow-human-mechanicus-focus --client '#{client_tty}'` for explicit UI navigation into mechanicus. Do not use timeout-based overrides for normal mouse/key navigation. See `cli-tools/docs/tmux-focus-guard.md`.

### Pane-bound assertion (`tmuxctl assert-instance`)

`tmuxctl assert-instance --pane <target>` is the public assertion/check-and-repair primitive for pane-backed agents. It is pane-type-bound: persona panes launch/reactivate when safe and report live identity mismatches without injecting `/persona`, stack workers are retired/pruned when dead, and palace/somnium panes only report truth while cleaning stale registry rows. Use it before automated injection; if it reports a persona mismatch/unregistered action, restart or let SessionStart re-register the protected pane before sending sensitive payloads. See `cli-tools/docs/managed-stack-dispatch.md`.

### Subagents (`subagent`)

Launch Codex sub-agents in dedicated terminal windows with repo-scoped logging. The command mirrors the `cli codex` implementation from ProcurementAgentAI and works from any repository.

**Key capabilities:**
- `subagent "<prompt>"` launches Codex with inline prompts
- `subagent --prompt-file docs/p_task.md` reads prompts from a file (source file stays untouched)
- `subagent @docs/p_task.md` shorthand for file prompts
- Automatic temp file creation for long or multi-line prompts (>8KB or containing newlines)
- Automatically detects packaged project virtual environments: respects `UV_PROJECT_ENVIRONMENT`, local `.venv/venv` folders, or an explicit `CLI_TOOLS_CODEX_VENV` override so Codex can launch even when the repo venv is not activated
- Logs captured in `logs/agents/` relative to the directory where you ran the command

**Examples:**
```bash
# Inline prompt
subagent "Review docs/p_auth_plan.md and implement the authentication API."

# File-based prompt (explicit flag)
subagent --prompt-file docs/p_feature_plan.md

# File-based prompt (shorthand)
subagent @docs/p_feature_plan.md
```

### Time Conversion (`time-convert`)

Convert a wall-clock time in any IANA timezone (e.g., `America/New_York`) into your system's local timezone.

**Features:**
- Accepts `HH:MM`, `HMM`, or `HHMM` 24-hour inputs (e.g., `8:30`, `830`, `1500`).
- Optional `--date YYYY-MM-DD` to anchor conversions on a specific calendar date.
- `--output-format` lets you customize the strftime pattern.
- `--verbose` prints both the source and local timestamps.
- Accepts common timezone shorthands like `UTC`, `PST`, or `CEST`.
- Defaults to 12-hour local output (`%I:%M %p`), still configurable via `--output-format`.

**Usage:**
```bash
time-convert 830 America/Los_Angeles
time-convert 1500 Europe/London --date 2025-02-14 --verbose
```

## Installation

The tools are automatically available via the wrapper script. No manual installation needed.

## Usage from Any Repository

Simply call the commands directly - they will automatically:
1. Detect if a venv exists, create one if needed
2. Install dependencies using `uv`
3. Execute the command

```bash
# From any repo directory
subagent "Your task description"
time-convert 1500 Europe/London
```

## Configuration

### Subagents

No configuration needed. The tool automatically detects the repository root (via Git when available), reads `UV_PROJECT_ENVIRONMENT` or local `.venv/venv` directories to find the packaged virtual environment, and writes agent logs to `logs/agents/`. You can force a specific environment by setting `CLI_TOOLS_CODEX_VENV=/path/to/.venv`.

### Time Conversion

No configuration required. The command runs entirely locally and reads timezone data from the standard library.

## Development

To modify or extend these tools:

1. Edit files in `~/cli-tools/src/cli_tools/`
2. The wrapper script will automatically rebuild the venv if dependencies change
3. Test from any repository directory

## Architecture

- `~/cli-tools/` - Main project directory
- `~/cli-tools/src/cli_tools/` - Source code
- `~/cli-tools/bin/` - Wrapper scripts
- `~/cli-tools/.venv/` - Virtual environment (auto-managed)
