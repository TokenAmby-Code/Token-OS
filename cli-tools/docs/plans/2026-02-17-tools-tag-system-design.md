# Tools Tag Browsing System — Design

**Date:** 2026-02-17
**Status:** Approved

## Summary

Replace the `tool` bash function with a `tools` command in `cli-tools/bin/` that adds tag-based browsing, audience-aware behavior, and directory-scoped tool injection for Claude sessions.

## Caller Modes

The script defaults to **agent mode** (non-interactive). The `.bash_aliases` wrapper passes `--human` for interactive use.

```bash
# .bash_aliases
tools() { command tools --human "$@"; }
```

| Mode | Interactive prompts | AI fallback | Tool creation offer |
|------|---------------------|-------------|---------------------|
| Agent (default) | No | Yes (prints suggestion, exits) | No (agent uses tool-creator subagent) |
| Human (`--human`) | Yes | Yes | Yes (interactive prompt) |

## Inline Metadata Format

Each script gets a structured header:

```bash
#!/usr/bin/env bash
# pr-create - Create GitHub pull request with review
# TAGS: git, pr, workflow
# AUDIENCE: human, agent
```

- **Name + description:** `# <name> - <one-sentence description>`
- **TAGS:** comma-separated, lowercase
- **AUDIENCE:** `human`, `agent`, or both. Defaults to both if omitted.

## Tag Taxonomy

| Tag | Tools |
|-----|-------|
| `git` | pr-create, pr-merge, pr-review-loop, worktree-setup, worktree-delete |
| `pr` | pr-create, pr-merge, pr-review-loop |
| `worktree` | worktree-setup, worktree-delete |
| `token-api` | token-restart, token-status, token-ping, tts-skip, timer-mode, timer-status, timer-test |
| `deploy` | deploy, cloud-logs |
| `db` | db-query, db-migrate |
| `mobile` | ssh-phone, macrodroid-gen, macrodroid-pull, macrodroid-push, macrodroid-read, macrodroid-state, tasker-push |
| `macrodroid` | macrodroid-gen, macrodroid-pull, macrodroid-push, macrodroid-read, macrodroid-state |
| `instance` | instance-name, instance-stop, instances-clear, subagent, agents-db |
| `system` | mem-watchdog, time-convert, screenshot, browser-console, sandbox-server |
| `workflow` | stash, followup, test |

### Audience Split

- **Human-primary:** ssh-phone, time-convert, stash, followup, screenshot
- **Agent-primary:** subagent, agents-db, instance-name, instance-stop, instances-clear, mem-watchdog, sandbox-server
- **Both:** everything else

## Command Interface

```
tools                        # list all tools, grouped by primary tag
tools <tag>                  # filter by tag
tools <query>                # fuzzy name search, then AI fallback
tools --tags                 # list all available tags with counts
tools --agent --dir <path>   # output tools for a directory's tag set (for Claude injection)
tools --human                # human mode (enables interactive prompts)
tools --help                 # usage info
```

## Output Format

### Grouped (default and tag filter)

```
$ tools git

  pr:
    pr-create        Create GitHub pull request with review
    pr-merge         Merge approved pull request
    pr-review-loop   Review loop for PRs

  worktree:
    worktree-setup   Set up git worktree
    worktree-delete  Delete git worktree
```

When filtering by tag, tools are sub-grouped by their other shared tags.

### Agent directory mode

```
$ tools --agent --dir ~/ProcAgentDir

Available CLI tools:
  pr-create        - Create GitHub pull request with review
  pr-merge         - Merge approved pull request
  deploy           - Deploy to Cloud Run
  db-query         - Query Cloud SQL database
  ...
```

## Directory-to-Tag Mapping

A config file maps directory patterns to relevant tag sets:

```yaml
# cli-tools/directory-tags.yaml
~/ProcAgentDir:
  tags: [deploy, db, pr, git, instance, workflow]
/Volumes/Imperium/runtimes/token-os/live/token-api:
  tags: [token-api, deploy]
/Volumes/Imperium/runtimes/token-os/live/mobile:
  tags: [mobile, macrodroid]
/Volumes/Imperium/runtimes/token-os/live/cli-tools:
  tags: [all]
```

## Claude Session Integration

A Claude Code startup hook runs `tools --agent --dir $PWD` and injects the output as session context. This means:

- CLAUDE.md files no longer need to list/describe available tools
- CLAUDE.md focuses on prescriptive guidance (how to use tools, not what exists)
- Tool descriptions are maintained in one place (script headers)
- Adding a tool + tagging it automatically makes it visible in the right sessions

## Architecture

- **`cli-tools/bin/tools`** — main bash script, all logic
- **Inline headers** — source of truth for metadata
- **`cli-tools/directory-tags.yaml`** — directory-to-tag mapping
- **`.bash_aliases`** — thin wrapper passing `--human`
- **Claude hook** — runs `tools --agent --dir $PWD` at session start

## AI Fallback Flow

1. Query matches no tags and no tool names
2. Claude Haiku identifies which tool(s) the user likely meant
3. If no match found:
   - **Human mode:** offers interactive tool creation prompt, spawns Claude session
   - **Agent mode:** prints suggestion and exits (agent invokes tool-creator subagent if needed)
