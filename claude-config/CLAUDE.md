# Global Claude Code Configuration

This file provides global guidance for AI agents working across projects in this environment.

## NAS Volumes — Single Source of Truth

All source code, vaults, and project data live on the NAS (`Token-NAS`, `192.168.0.12`). Local `~/` paths are legacy — always use NAS mount paths.

| Volume | Mac Mount | WSL Mount | Encryption | Contents |
|--------|-----------|-----------|-----------|----------|
| **Imperium** | `/Volumes/Imperium` | `/mnt/imperium` | None | Imperium-ENV vault, Scripts/, CLI tools, Plans |
| **Civic** | `/Volumes/Civic` | `/mnt/civic` | AES-256 | Pax-ENV vault, ProcurementAgentAI, Civic work projects |

Use `$IMPERIUM` and `$CIVIC` env vars — never hardcode mount paths. See `Scripts/cli-tools/lib/nas-path.sh`.

**Naming:** Civic Initiatives (company) > askCivic (AI service) > Pax (AI agent). Volumes are named after the org.

**Agent account:** TokenClaw (non-admin, read/write on both shares). Security boundary is the mount — Civic is invisible when locked.

**Before NAS-dependent work**, verify mounts are live:
```bash
ls "$IMPERIUM/Token-OS/" > /dev/null 2>&1 && echo "ok" || echo "NOT MOUNTED"
ls "$CIVIC/" > /dev/null 2>&1 && echo "ok" || echo "NOT MOUNTED or LOCKED"
```
If a mount is down, inform the user — do not attempt to remount (requires sudo).

**Full directives:** `$IMPERIUM/Plans/nas-agent-directives.md`

## Session Setup (First Response)

On your **first response** in a new session, run these setup steps silently before addressing the user's request:

```bash
# 1. Name this session (pick something descriptive based on cwd or user's task)
instance-name "<brief-descriptive-name>"

# 2. Check git context (exit 128 = not a git repo, expected)
git branch --show-current 2>/dev/null || [ $? -eq 128 ]
```

**Naming guidelines:**
- Use the project/directory name if generic task: `instance-name "token-api"`
- Use the task if specific: `instance-name "fix-auth-bug"` or `instance-name "add-payments-feature"`
- Keep it short (2-4 words, kebab-case)

**Session doc** (after naming, for non-primarch sessions):
- Primarch sessions auto-link via `TOKEN_API_PRIMARCH` — no action needed
- For non-trivial work (3+ steps, multi-file changes), create or link a session doc:
  1. Check for existing active docs: `token-ping "session-docs?status=active"`
  2. If a relevant doc exists, link to it: `instance-name "<name>" --session-id <ID>`
  3. Otherwise create new: `instance-name "<name>" --session`
- For trivial/ad-hoc tasks, skip (no doc needed)

**Optional prompts** (ask only if relevant):
- If on `main`/`master` and task involves changes: "Should I create a worktree/branch for this?"
- If multiple active instances in same repo: Note this to avoid conflicts

## Behavioral Guidelines

**Fix broken tools with the tool-creator subagent — never with ad-hoc bash.** When a CLI tool isn't working, launch `Task` with `subagent_type=tool-creator` to diagnose and fix it. Do NOT start cobbling together raw SSH commands or bash workarounds to replicate what the broken tool should do. The detour through the subagent is always faster than the bypass.

**Surface confusion early.** If a request is ambiguous, present the interpretations
rather than picking one silently. State assumptions explicitly. Push back if a simpler
approach exists. Stop and ask when confused rather than guessing.

**Surgical changes.** Every changed line should trace directly to the request. Don't
improve adjacent code, comments, or formatting. Match existing style even if you'd do
it differently. Clean up orphans YOUR changes created (unused imports, dead functions),
but don't remove pre-existing dead code unless asked - mention it instead.

**Follow through — don't suggest.** Never end a response by offering to run a command
or telling the user to do something you could do yourself. If you can do it, just do it.
If you need permission, use AskUserQuestion. Ending with "Want me to run X?" or
"Here's the command:" is a failure mode — run the command and report the result instead.

**Verify as you go.** For multi-step work, state what you'll check after each step.
When fixing bugs, confirm the fix actually resolves the issue. When refactoring,
confirm behavior is preserved. Don't mark a task complete without verification.

## Task Lists (IMPORTANT)

**Always use TaskCreate/TaskUpdate for non-trivial work.** The Token-API TUI displays task progress for each instance. Using task lists ensures:
- The user can monitor progress across all running instances
- Work is visible in the dashboard even when the instance isn't focused
- Complex tasks are broken into trackable steps

Use tasks for anything with 3+ steps or that involves multiple files.

## CLI Tools (`$IMPERIUM/Token-OS/cli-tools/bin/`)

| Command | Purpose |
|---------|---------|
| `instance-name` | Session naming |
| `transplant` | Move session to different directory/device with history preserved |
| `worktree-setup` | Create git worktrees from NAS bare repos |
| `vault-dispatch` | Spawn Claude in vault, brief it, transplant to work |
| `work-loop` | Full cycle: vault → worktree → implement → PR → merge → cleanup |
| `tx br` | Backrooms dispatch/status/cleanup (`tx backrooms`) |
| `cloud-logs` | Cloud Run logs |
| `db-query` | Cloud SQL access |
| `db-migrate` | SQL migrations |
| `deploy` | Deployments |
| `pr-create` | PR + review |
| `test` | Local testing |
| `ssh-connect` | Standardized SSH with redirect-on-exit (`ssh-mac`, `ssh-wsl`, `ssh-phone`) |
| `time-convert` | Timezone conversion |

Full docs: `$CIVIC/ProcurementAgentAI/CLAUDE.md` or use slash commands (`/pr`, `/test`, `/logs`, `/deploy`, `/db-query`).

## Worktree Pipeline — Vault to Implementation

The standard flow for implementation work is: **vault → worktree → plan → execute.**

Use the `/session-plan` skill to run this flow. It handles project detection, vault research, worktree creation, and transplant orchestration.

```
Vault (Imperium-ENV or Pax-ENV)
  ↓ /session-plan — exhaust vault context
  ↓ worktree-setup <branch> --no-transplant
  ↓ transplant --plan ~/worktrees/<project>/wt-<branch>
Worktree (plan mode)
  ↓ explore codebase, design approach
  ↓ transplant --execute-plan
Worktree (implementation)
  ↓ implement, test, /pr
```

**For autonomous dispatch** (no human in the loop):
```bash
vault-dispatch <session-doc> <working-dir> [--primarch <name>]   # one-shot
work-loop dispatch <session-doc> [--branch <name>]               # full cycle to PR merge
tx br "prompt"                                                     # quick backrooms task
```

**Worktree configs:** `~/.config/worktrees/<project>.conf` — project auto-detected from CWD.

## Vault Mind — Obsidian as Extended Cognition

The Obsidian vault is your extended mind. Each vault (-ENV) is a domain you can exist within:

| Vault | NAS Volume | Path | Domain |
|-------|-----------|------|--------|
| **Imperium-ENV** | Imperium | `$IMPERIUM/Imperium-ENV/` | Personal + agent workspace (Terra/ + Mars/) |
| **Pax-ENV** | Civic | `$CIVIC/Pax-ENV/` | Work (Civic Initiatives, professional projects) |

### Identity Model

**Session doc = self.** When reading your session document, you are reading your own memory. "I decided X. I was working on Y." Continue from where you left off.

**Vault notes = institutional.** When reading vault notes, treat them as knowledge written before you. Respect the context that produced the decision, but bring your own judgment. Don't blindly accept.

### On Startup

If you have a linked session document, invoke the **vault-mind** skill. It reads your session doc, follows `[[wikilinks]]` into the vault, and orients you. Startup traversal is 1-level deep; runtime traversal is unlimited — use `obsidian vault=<name> read/search/backlinks` freely during your session.

### Writing to the Vault

| Tier | Agent Class | Vault Access |
|------|------------|--------------|
| Imperial Guard | MiniMax | Session doc only |
| Astartes+ | Claude | Session doc + **earned** vault writes via **vault-canon** skill |
| Administratum | Sonnet+ cron | Processes completed session docs > vault, then archives |

Direct vault writes are rare and significant. Use **vault-canon** when you have a hard decision, a correction, a pattern, or an architectural truth. Everything else goes in the session doc.

### Obsidian CLI Quick Reference

```bash
obsidian vault=<name> read path="<note>.md"        # Read a note
obsidian vault=<name> search query="<term>"         # Search titles
obsidian vault=<name> search:context query="<term>" # Search content
obsidian vault=<name> backlinks path="<note>.md"    # What links here?
obsidian vault=<name> create path="<note>.md" content="..."  # Create
obsidian vault=<name> append path="<note>.md" content="..."  # Append
obsidian vault=<name> property:set path="<note>.md" property="key" value="val"
```

## Context Reset Protocol

When a session approaches context limits or completes a major phase, follow this handoff procedure:

1. **Agent exhaustively updates session document** — all decisions, activity, and current state written to the session doc via vault-mind/session-update
2. **Agent enters plan mode** — writes a continuation plan capturing exactly what comes next, with file paths and specific changes
3. **User approves plan** — reviews the continuation plan for correctness
4. **User resets context** (or starts new session) — pastes the approved plan as the opening prompt
5. **New session invokes vault-mind** — reads the session doc, traverses wikilinks, orients from where the previous agent left off
6. **New session executes the plan** — picks up exactly where the previous session stopped

**Key rules:**
- The session doc is the source of truth, not the plan. The plan is a pointer.
- Include the transcript path in the plan if the new session might need verbatim content (code snippets, error messages).
- The plan should reference specific files, line numbers, and vault paths — not vague descriptions.

## Project-Specific Configuration

Projects have their own CLAUDE.md files with project-specific guidance. Claude loads **all ancestor CLAUDE.md files** in the directory chain, so child projects inherit this global config automatically.

Key projects:
- `$CIVIC/ProcurementAgentAI/CLAUDE.md` - Main project (architecture + CLI tool docs)
- `$IMPERIUM/Token-OS/token-api/CLAUDE.md` - Token-API server
- `$IMPERIUM/Token-OS/mobile/CLAUDE.md` - Mobile/MacroDroid tools
- `$IMPERIUM/Imperium-ENV/CLAUDE.md` - Primary Obsidian vault (Imperium)
- `$CIVIC/Pax-ENV/CLAUDE.md` - Work Obsidian vault (obsidian-cli docs live here)
