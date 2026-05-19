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

- `dispatch --target legion:new|mechanicus:new` allocates stack panes via `tmuxctl stack add`.
- Prefix+Space (`tmux-legion-prompt`) launches via `tmuxctl stack dispatch legion`.
- Claude print-mode redirection (`claude-wrapper.sh`) launches via `tmuxctl stack dispatch`.
- Golden Throne resume fallback allocates managed legion workers via `tmuxctl stack add legion`; legacy side-window naming has been retired.
- `work-loop dispatch` allocates managed legion workers via `tmuxctl stack add legion` and marks them with `@WORK_LOOP=true`.
- Pane demotion (`tmux-shuttle`) moves panes into legion as `legion:worker` / `stack-worker`, then calls `tmuxctl stack enforce --focus`.
- Aspirant full-session launch (`dispatch --aspirant --aspirant-kind dispatch`) creates the aspirant note/session doc, then re-enters `dispatch --target legion:new` with an aspirant system prompt, generated launch prompt, linked session doc, and Golden Throne metadata. Use `--intake-only` for the old note/session-only behavior.

If a new tool needs a legion/mechanicus pane, wire it to `tmuxctl stack add` or `tmuxctl stack dispatch`. Do not duplicate layout, split, tagging, or focus behavior in shell.

See also: [`aspirant-dispatch.md`](aspirant-dispatch.md) for the full aspirant launch contract and real validation walk.
