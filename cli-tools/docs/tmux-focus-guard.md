# tmux Focus Guard

## Invariant

Background/automation code must not leave the operator focused in a different
tmux window or pane. Only explicit human navigation may change focus.

Automation surfaces include hooks, Token-API workers, close-down assertions,
explicit/manual stack sweep/enforce, layout normalization, reconciler cleanup,
recolor/title updates, and dispatch that was requested as non-focusing. Stack
sweep must not be scheduled on a timer; stack repair is event-driven because
periodic layout repair can still produce a visible snap before focus restore.

## Automation contract

Use `tmuxctl.focus_guard.preserve_focus(...)` around any automation path that
can run focus-changing tmux commands as a side effect:

```python
from tmuxctl.focus_guard import preserve_focus

with preserve_focus(adapter, source="my-worker", attempted_target=target):
    adapter.run("select-layout", "-t", target, "main-vertical", allow_failure=True)
```

The helper captures `#{session_name}:#{window_index}` and `#{pane_id}`, runs the
body, then restores the original focus if it changed. Restores are logged to
`/tmp/tmux-focus-guard.log` and `/tmp/mechanicus-focus-guard.log`.

The tmux shim and `TmuxAdapter` also block automation focus commands when
`IMPERIUM_TMUX_AUTOMATION` or `TOKEN_API_INTERNAL_DISPATCH` is set. Restore
operations use `IMPERIUM_TMUX_FOCUS_RESTORE=1`.

## Human navigation contract

Mechanicus is guarded because automated stack work often touches that window.
Human UI paths must mark the current client as explicitly navigating before
selecting mechanicus:

```bash
tmuxctl allow-human-mechanicus-focus --client '#{client_tty}' --reason mouse-status
```

This is not a timer. It stores the human client in tmux global options:

- `@IMPERIUM_HUMAN_MECHANICUS_FOCUS_CLIENT`
- `@IMPERIUM_HUMAN_MECHANICUS_FOCUS_REASON`

`tmuxctl mechanicus-focus-guard` allows mechanicus focus for that client until
the client selects a non-mechanicus pane/window. Leaving mechanicus clears the
marker.

Current explicit human bindings in `tmux/tmux-base.conf`:

- mouse status-bar window click
- mouse pane click
- Prefix+4
- Prefix+h/j/k/l pane navigation
- audience/tombstone/goto/shuttle navigation paths use explicit override envs
  where appropriate

## Legacy temporary override

`tmuxctl allow-mechanicus-focus --seconds N` still exists for compatibility and
one-shot scripted explicit jumps, but do not use it for normal UI navigation.
Prefer `allow-human-mechanicus-focus` so behavior is client-scoped and not based
on magic timeout windows.

## Audit query

When adding tmux code, classify any match as either explicit human navigation or
automation with focus preservation:

```bash
rg -n "select-pane|select-window|switch-client|select-layout|join-pane|break-pane|move-pane|swap-pane|split-window" \
  cli-tools token-api ~/.claude/hooks
```

Dangerous primitives include `select-window`, `select-pane`, `switch-client`,
non-detached `split-window`, `join-pane`, `move-pane`, `swap-pane`, and layout
operations that may reselect panes. `select-pane -P` is also unsafe against
non-current panes in this environment; do not use it from background cleanup
unless the target is already current or the operation is focus-preserved.

