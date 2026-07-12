# Centralized CLI Tools - Summary

## Overview

This project centralizes Python CLI tools from multiple repositories (`ProcurementAgentAI/cli` and `invoices/cli`) into a single location (`~/cli-tools/`) that any developer—or AI agent—can use from any repository.

## Key Features

✅ **Automatic venv management** - Tools automatically create and manage their own virtual environment
✅ **Non-interactive shell friendly** - Works in AI agent environments without requiring shell initialization
✅ **uv-based** - Uses `uv` for fast, reliable package management instead of pip
✅ **Centralized commands** - All CLI tools accessible from any repository
✅ **Zero configuration** - Works out of the box, no manual venv activation needed

## Structure

```
~/cli-tools/
├── bin/                    # Executable wrapper scripts
│   ├── cli-wrapper         # Main wrapper with venv management
│   ├── subagent            # Convenience wrapper for Codex subagents
│   └── time-convert        # Convenience wrapper for timezone conversion helper
├── src/
│   └── cli_tools/
│       ├── subagents/      # Codex subagent orchestration helpers
│       └── timezone/       # Time conversion CLI (ported from ProcurementAgentAI)
├── .venv/                  # Auto-managed virtual environment
├── pyproject.toml          # Project dependencies (uv format)
├── README.md               # User documentation
├── INSTALL.md              # Installation instructions
└── SUMMARY.md              # This file
```

## Available Commands

### `subagent`
Launch Codex sub-agents in dedicated terminals with the same capabilities as `cli codex` (prompt file support, `@filename` shorthand, automatic temp files, repo-scoped logging) plus packaged-venv detection so Codex can start without activating environments manually.

```bash
# Inline prompt
dev@repo:~$ subagent "Review docs/p_backend_plan.md and implement the API."

# File-based prompts
subagent --prompt-file docs/p_feature_plan.md
subagent @docs/p_feature_plan.md  # shorthand
```

### `time-convert`
Convert a wall-clock time in any IANA timezone into your local timezone. Useful for quick stand-up planning and scheduling across teams.

```bash
# Convert 3pm London time into local timezone
time-convert 1500 Europe/London --date 2025-02-14 --verbose
```

## How It Works

1. **Wrapper Script** (`bin/cli-wrapper`):
   - Checks if venv exists, creates one if needed
   - Uses `uv` to install/update dependencies
   - Executes Python modules inside the managed environment
   - Works in non-interactive shells (no `.bashrc` sourcing needed)

2. **Convenience Scripts** (`bin/subagent`, `bin/time-convert`):
   - Lightweight shims that call `cli-wrapper`
   - Safe to symlink or add to PATH for system-wide access

3. **Python Modules** (`src/cli_tools/`):
   - Standard Python package layout with module-per-command
   - Entry points defined via `pyproject.toml`
   - Automatically installed by `uv`

## Usage Examples

```bash
# Launch a Codex agent from any repo
dev@repo:~/project$ subagent "Investigate authentication flow"

dev@repo:~/project$ subagent @docs/p_refactor_plan.md

# Convert a meeting time
$ time-convert 930 America/Los_Angeles --date 2025-12-13
```

## Installation

### Quick Setup (Add to PATH)

```bash
export PATH="$HOME/cli-tools/bin:$PATH"
```

### Verification

```bash
subagent --help
time-convert --help
```

## Requirements

- Python 3.11+
- `uv` package manager
  - Install: `curl -LsSf https://astral.sh/uv/install.sh | sh`

## Migration Notes

**Subagents:**
- Old: `./codex-simple "prompt"` inside a repo
- New: `subagent "prompt"` from any repo (auto-detects packaged env)

**Time Conversion:**
- Old: `python -m scripts.timezone.cli 1500 Europe/London`
- New: `time-convert 1500 Europe/London`

## Configuration

- **Subagents:** Automatically create log files in `logs/agents/` relative to the working repo and respect `UV_PROJECT_ENVIRONMENT`, `.venv/venv`, or `CLI_TOOLS_CODEX_VENV` to find Codex binaries. No manual setup required unless overriding via env vars.
- **Time Conversion:** No configuration—runs entirely locally using the Python standard library.

## Development

1. Edit files in `~/cli-tools/src/cli_tools/`
2. Update `pyproject.toml` if dependencies change
3. `cli-wrapper` rebuilds/updates the venv automatically on next run

## Documentation

- **User Guide:** `~/cli-tools/README.md`
- **Installation:** `~/cli-tools/INSTALL.md`
- **Agent Documentation:** Add relevant pointers to downstream repos as needed
