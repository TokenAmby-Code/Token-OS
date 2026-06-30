# tmuxctld Contracts Reference

## Security / Transport

- tmuxctld binds loopback only (`127.0.0.1:7778`).
- It is a local transport/proxy, not a remote API.
- Treat request payloads as privileged tmux operations; do not add unauthenticated network exposure.

## Boundary

- Token-API owns registry, session docs, policy, read models, and high-level operator intent.
- tmuxctld/tmuxctl own pane send, liveness probing, lifecycle operations, labels, and tmux focus-safe mechanics.
- Token-API should call the daemon for tmux actions instead of shelling out to raw tmux or killing panes directly.

## Launchd Authority

`tmuxctld-ctl install|start|restart|stop` mutates local launchd/runtime state. Use only for daemon maintenance or recovery, not routine validation.

## Send/Lifecycle Invariants

- Preserve focus guards and internal focus-preserving send paths.
- Use stable pane labels for human-facing target names.
- Do not allocate into numbered panes; allocation requests use the documented dispatch/tmuxctl path for new workers.
- Lifecycle operations must maintain registry truth through Token-API rather than leaving orphaned live panes or dead rows.
