---
name: session-plan
description: "Vault-first launch protocol: gather context in Pax-ENV, create worktree, transplant into plan mode. The standard way to start implementation sessions."
---

# Session Plan — Vault Launch Protocol

The flow is: **vault → worktree → plan → execute.** No exceptions.

Start in Pax-ENV where context is cheap. Exhaust the vault. Create the worktree. Transplant with `--plan`. Design the approach. Get approval. Execute.

## When to Use

- Starting any implementation task
- Picking up a session doc that needs a worktree
- Any time the user says "session plan" or invokes `/session-plan`

## Assumptions

- You can reach **Pax-ENV** (the Obsidian vault) via `obsidian vault=Pax-ENV`
- The askCivic project config exists at `~/.config/worktrees/askCivic.conf`
- Worktrees live at `~/worktrees/askCivic/wt-<name>`

---

## Phase 1: Exhaust the Vault

You are in the vault. **Read everything relevant before leaving.** Vault context is free; post-transplant context is expensive.

1. **Load session doc** if one exists (invoke `/vault-mind`)
2. **Search broadly** — topic keywords, related terms, adjacent features
3. **Follow every `[[wikilink]]`** in discovered notes — read linked docs, architecture notes, prior session docs, specs
4. **Read, don't skim** — the vault docs you skip now are the ones you'll wish you had after transplant prunes context

```bash
# Search for the topic and related terms
obsidian vault=Pax-ENV search:context query="<topic>"
obsidian vault=Pax-ENV search:context query="<related-term>"

# Read every hit — and follow their wikilinks
obsidian vault=Pax-ENV read path="<note>.md"

# Check backlinks for notes that reference what you found
obsidian vault=Pax-ENV backlinks path="<key-note>.md"
```

**Done when:** You've read every note that touches the task. Not when you've found "enough."

## Phase 2: Session Doc

Every top-level session auto-creates a session doc on SessionStart. Check if you already have one:

```bash
# Resolve your own instance
CLAUDE_PID=$(pid=$$; for _ in 1 2 3 4 5; do [ -z "$pid" ] || [ "$pid" = "1" ] && break; comm=$(basename "$(ps -o comm= -p "$pid" 2>/dev/null)" 2>/dev/null); [ "$comm" = "claude" ] && echo "$pid" && break; pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' '); done)
token-ping instances/resolve pid=$CLAUDE_PID cwd=$(pwd)
```

**If `session_doc_id` is present** (expected — auto-created on SessionStart):
- Read it. Decide if it's the right doc for this work.
- If yes: merge your vault findings into it via `/session-update`
- If no: reassign to an existing doc or create a new one:
  ```bash
  token-ping instances/<id>/assign-doc doc_id=<N>   # reassign to existing
  instance-name "<name>" --session                   # create new
  ```

**If no session doc exists** (fallback — shouldn't happen with auto-creation):
```bash
instance-name --session "<task-description>"
```

Update the session doc with findings from Phase 1 — context links, identified files, preliminary decisions.

## Phase 3: Identify Primarch

Determine if the work maps to a primarch persona:

| Domain | Primarch |
|--------|----------|
| Infrastructure, vault systems, cli-tools | vulkan |
| Code architecture, refactoring, standards | guilliman |
| Procurement, civic work (askCivic) | fabricator-general |
| Defensive, security, validation | dorn |
| Stealth ops, covert work | corax / alpharius |

If the current instance is already a primarch, transplant carries it automatically.

## Phase 4: Pick or Create a Worktree

This is the landing zone decision. Survey **both machines**, then decide.

### Survey all worktrees

```bash
# Authoritative list — the bare repo knows about ALL worktrees across all machines
git -C /Volumes/Civic/askcivic.git worktree list

# Local worktrees only
ls ~/worktrees/askCivic/

# Staged worktrees (exported from another machine, ready to import)
worktree-sync status
```

The bare repo output shows paths from both machines:
- `/Users/tokenclaw/worktrees/askCivic/wt-*` → Mac worktrees
- `/home/token/worktrees/askCivic/wt-*` → WSL worktrees
- `prunable` flag → worktree directory is missing (stale reference, or on the other machine)

### Decision tree

1. **Worktree exists locally** (e.g., `~/worktrees/askCivic/wt-<name>`)
   - Target: `~/worktrees/askCivic/wt-<name>`
   - No setup needed, go to Phase 5

2. **Worktree exists on the other machine** (e.g., `/home/token/worktrees/askCivic/wt-<name>`)
   - **Ask the user:** "The `<name>` worktree is checked out on WSL/Mac. Should I:"
     - **a) Transplant cross-device** — work on it where it lives
       ```bash
       transplant --host wsl /home/token/worktrees/askCivic/wt-<name>
       ```
     - **b) Pull it to this machine** — export from there, import here
       ```bash
       # On the remote machine (via ssh):
       ssh-wsl "worktree-sync export <branch-name>"  # or ssh-mac
       # Then locally:
       worktree-sync import <branch-name>
       ```
     - **c) Create a fresh local worktree** on the same branch (if no uncommitted work there)
       ```bash
       worktree-setup <branch-name> --existing --no-transplant
       ```
   - Let the user decide — the right choice depends on whether there's uncommitted work, which machine they want to be on, etc.

3. **Worktree is staged on NAS** (exported from another machine, shown in `worktree-sync status`)
   ```bash
   worktree-sync import <branch-name>
   ```
   - Target: `~/worktrees/askCivic/wt-<name>`

4. **Task needs main** (hotfix, quick change, no branch needed)
   - Target: `~/worktrees/askCivic/wt-main`
   - No setup needed, go to Phase 5

5. **Task needs a new worktree** (new feature, new branch)
   ```bash
   worktree-setup <branch-name> --no-transplant
   ```
   - `--no-transplant` because we handle that ourselves in Phase 5
   - Target: `~/worktrees/askCivic/wt-<name>`

6. **Different project entirely**
   ```bash
   worktree-setup <branch-name> --project <project> --no-transplant
   ```

### Naming convention

- Branch names: descriptive kebab-case (`amendment-generator`, `fix-auth-flow`)
- Worktree dirs are auto-named `wt-<branch-name>` by `worktree-setup`
- Do NOT create worktrees inside `.claude/` or inside another worktree

## Phase 5: Transplant into Plan Mode

Transplant to the worktree **with `--plan`**. This is not optional — you always plan before executing.

```bash
transplant --plan [--primarch <name>] ~/worktrees/askCivic/wt-<name>
```

The `--plan` transplant:
1. Kills the current session in Pax-ENV and restarts in the worktree with `--resume`
2. The new session lands with a fresh context + the plan prompt
3. You explore the codebase and design the implementation approach in plan mode
4. Plan gatekeeper hook rejects the first plan with a directive to update the session doc
5. You update the session doc, resubmit — second plan is auto-approved

**After plan approval, you MUST use `transplant --execute-plan` to start the implementation session:**

```bash
transplant --execute-plan
```

This clears the plan-mode exploration context and starts a clean session with only the approved plan. Do NOT exit plan mode and start coding in the same session — the plan-mode session is bloated with codebase exploration that the implementation session doesn't need.

**What survives:**
- `transplant --plan` (vault → worktree): conversation history via `--resume`, session doc link, primarch
- `transplant --execute-plan` (context reset): the approved plan (written to `~/.claude/plans/`), session doc link, primarch

**There is no "skip planning" path.** The vault context you gathered in Phase 1 gets distilled into the plan. The plan is what survives into the implementation session. If you skip planning, the vault work was wasted.

**There is no "exit plan mode and start coding" path.** If you `ExitPlanMode` and start editing files, you've defeated the context pruning. Always use `transplant --execute-plan`.

---

## Quick Reference

```bash
# The flow: vault → worktree → plan → execute
# 1. Exhaust the vault (Phase 1)
obsidian vault=Pax-ENV search:context query="<topic>"
obsidian vault=Pax-ENV read path="<note>.md"

# 2. Create worktree (Phase 4)
worktree-setup <branch-name> --no-transplant

# 3. Transplant into plan mode (Phase 5)
transplant --plan --primarch fabricator-general ~/worktrees/askCivic/wt-<name>
# ... design approach ... user approves ...
transplant --execute-plan

# Survey worktrees
git -C /Volumes/Civic/askcivic.git worktree list
worktree-sync status

# Cleanup after merge
worktree-delete <branch-name> -b
```

## Anti-Patterns

- **Never** create worktrees inside `.claude/` or inside another worktree
- **Never** use Claude's built-in `EnterWorktree` tool — always use `worktree-setup`
- **Never** transplant without `--no-transplant` on worktree-setup (double transplant)
- **Never** hardcode paths — use `~/worktrees/<project>/wt-<name>` pattern
- **Never** `ExitPlanMode` then start coding — always `transplant --execute-plan` for a clean session
- **Never** modify this skill file during execution — consume it, don't edit it
