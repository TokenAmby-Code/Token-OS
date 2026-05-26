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


### Live agent pane prompt delivery (`agent-cmd`)

`agent-cmd` is the canonical command for submitting text into an existing Claude/Codex prompt pane. It normalizes payloads and uses the hardened literal-send + delayed double-submit sequence through `tmuxctl`; `claude-cmd` remains as a compatibility wrapper. Use this for live prompt injection instead of raw `tmux send-keys ... Enter`. See `cli-tools/docs/pane-prompt-delivery.md`.

### Managed tmux stack dispatch (`tmuxctl stack`)

`tmuxctl` is the single pane-backed dispatch primitive for managed stack pages. Use `tmuxctl stack add legion` to allocate a typed worker pane, or `tmuxctl stack dispatch legion --command ...` to allocate and launch in one step. Entry points such as `dispatch`, Prefix+Space (`tmux-legion-prompt`), Golden Throne resume fallback, `work-loop`, and pane demotion route through this tmuxctl stack code instead of raw `tmux split-window`. See `cli-tools/docs/managed-stack-dispatch.md`.

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
