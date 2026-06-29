---
name: tmuxctld
description: tmuxctld daemon shorthand. Use when inspecting or managing the loopback tmuxctl daemon, pane-send/lifecycle proxy, launchd service, daemon health, or tmuxctl boundary behavior.
---

# tmuxctld

`tmuxctld` is the loopback HTTP daemon face of `tmuxctl`. It owns tmux-side operations such as pane send/liveness/lifecycle proxying while Token-API owns registry/session state.

## Surfaces

- CLI daemon: `tmuxctld --host 127.0.0.1 --port 7778`.
- Control: `tmuxctld-ctl status|health|logs|start|restart|stop|install`.
- Implementation: `${TOKEN_OS:-$HOME/runtimes/Token-OS/live}/cli-tools/lib/tmuxctl/daemon.py`.
- LaunchAgent: `cli-tools/launchd/ai.tokenclaw.tmuxctld.plist`.
- Tests: `cli-tools/tests/test_tmuxctld_*.py`.

## Safe checks

```bash
tmuxctld --help
tmuxctld-ctl status
tmuxctld-ctl health
tmuxctl pane-live --help
```

## Do Not

- Do not start/restart/stop/install the daemon for dogfood; those mutate launchd/runtime state.
- Do not make Token-API kill tmux panes directly; preserve the Token-API/tmuxctld boundary.
- Do not leak raw `%pane` IDs in human-facing reports; translate to stable pane labels.
- Do not bypass tmux focus guards except through documented tmuxctl automation paths.
