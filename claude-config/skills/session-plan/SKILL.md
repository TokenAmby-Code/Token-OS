---
name: session-plan
description: Operational session launcher for moving from vault/session context into a project worktree, choosing or creating the correct worktree, transplanting into plan mode, and then executing the approved plan. Use when starting implementation sessions or invoking /session-plan.
---

# Session Plan

Session-plan is the operational launcher: resolve context, pick/create a worktree, transplant into plan mode, and after approval execute in a clean implementation session.

Vault-first intake doctrine belongs to `Personas/Ranks/Aspirant.md`; use that rank file for Aspirant trials and broad vault-context exhaustion.

## Project Detection

| CWD | Default Project | Default Vault |
|-----|----------------|---------------|
| `$IMPERIUM_VAULT` | Token-OS | Imperium-ENV |
| `$CIVIC/Pax-ENV` | askCivic | Pax-ENV |
| Worktree path | From worktree config | From session doc |

Override with `--project <name>` on `worktree-setup`. Available configs: `ls ~/.config/worktrees/`.

## Resolve Session Doc

```bash
INSTANCE_PID=$(pid=$$; for _ in 1 2 3 4 5 6 7 8; do [ -z "$pid" ] || [ "$pid" = "1" ] && break; comm=$(basename "$(ps -o comm= -p "$pid" 2>/dev/null)" 2>/dev/null); case "$comm" in claude|codex) echo "$pid" && break ;; esac; pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' '); done)
token-ping instances/resolve pid=$INSTANCE_PID cwd=$(pwd)
```

If a session doc is linked, read it and update it with any launch-critical findings before transplant. If no doc is linked, create or assign one intentionally.

## Survey Worktrees

```bash
ls ~/.config/worktrees/
git -C /Volumes/Civic/askcivic.git worktree list 2>/dev/null || true
git -C ~/runtimes/Token-OS/token-os.git worktree list 2>/dev/null || true
worktree-sync status 2>/dev/null || true
```

Decision:

1. Use an existing local worktree when it already matches the branch/task.
2. If the worktree is on another machine, ask whether to transplant cross-device, import with `worktree-sync`, or create a fresh local checkout.
3. If staged on NAS, import it.
4. For a new branch:
   ```bash
   worktree-setup <branch-name> --no-transplant [--project <project>]
   ```

## Transplant

Always enter plan mode first:

```bash
transplant --plan [--primarch <name>] ~/worktrees/<project>/wt-<name>
```

After plan approval, do not code in the bloated planning session. Start the clean implementation session:

```bash
transplant --execute-plan
```

## Anti-Patterns

- Never create worktrees inside `.claude/` or another worktree.
- Never use Claude's built-in worktree tool; use `worktree-setup`.
- Never double-transplant from `worktree-setup`; use `--no-transplant` when this skill handles transplanting.
- Never transplant without plan mode for implementation work.
- Never exit plan mode and start coding in the same context; use `transplant --execute-plan`.
