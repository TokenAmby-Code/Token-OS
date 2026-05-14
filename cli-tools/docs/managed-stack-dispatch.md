# Managed Stack Dispatch

`tmuxctl` owns pane-backed dispatch for managed stack pages.

## Invariant

- `legion` has exactly one orchestrator pane: `@PANE_ID=legion:custodes`, `@PANE_TYPE=legion`.
- `mechanicus` has exactly one orchestrator pane: `@PANE_ID=mechanicus:fabricator-general`, `@PANE_TYPE=mechanicus`.
- Every additional pane in either page is a right-column worker: `@PANE_ID=<base>:worker`, `@PANE_TYPE=stack-worker`.
- Dispatch entry points must not call raw `tmux split-window` into `legion`/`mechanicus`.

## Commands

Create a managed worker pane only:

```bash
tmuxctl stack add legion --session main --cwd "$PWD"
```

Create a managed worker pane and launch a command:

```bash
tmuxctl stack dispatch legion --session main --cwd "$PWD" --command 'echo hello world'
```

Reassert invariants around an existing pane/window:

```bash
tmuxctl stack enforce --pane %123
tmuxctl stack enforce --focus --pane %123
tmuxctl stack enforce --window main:legion
```

## Current entry points

- `dispatch --target legion:new|mechanicus:new` allocates stack panes via `tmuxctl stack dispatch`.
- `dispatch --id <session_id> --pane <tmux-pane-id>` is the canonical human-visible resume command. Shell history and staged resume commands should use `dispatch` only.
- Prefix+Space (`tmux-legion-prompt`) launches via `tmuxctl stack dispatch legion`.
- Claude print-mode redirection (`claude-wrapper.sh`) launches via `tmuxctl stack dispatch`.
- Golden Throne resume fallback allocates managed legion workers via `tmuxctl stack add legion`; legacy side-window naming has been retired.
- `work-loop dispatch` allocates managed legion workers via `tmuxctl stack add legion` and marks them with `@WORK_LOOP=true`.
- Pane demotion (`tmux-shuttle`) moves panes into legion as `legion:worker` / `stack-worker`, then calls `tmuxctl stack enforce --focus`.

If a new tool needs a legion/mechanicus pane, wire it to `tmuxctl stack add` or `tmuxctl stack dispatch`. Do not duplicate layout, split, tagging, or focus behavior in shell.

## Retired launcher names

Human launch and resume paths should use `dispatch` directly; `claude-launcher` remains only as a compatibility route to `dispatch --interactive`. The retired agent-facing launcher names hard-fail while the cutover is reviewed: `claude-dispatch`, `codex-dispatch`, `vault-dispatch`, `primarch`, `inquisitor`, and `subagent`. Internal dispatch may still invoke `codex-dispatch` and `primarch` with `TOKEN_API_INTERNAL_DISPATCH=1`.
