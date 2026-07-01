---
name: tmux-pane-death-revive
description: Temporary micro-SOP for dead tmux singleton persona panes. Use when council or mechanicus perpetual persona panes show `Pane is dead`, when manually reviving singleton seats, or when checking that tmux pane-died events reach tmuxctld `/event` and `/reconcile`.
---

# tmux-pane-death-revive

Temporary SOP until tmuxctld pane-death self-healing has proven stable.

## Rules

- Preserve the Token-API/tmuxctld boundary: do not patch registry rows and do not make Token-API kill tmux panes directly.
- Use stable labels in human-facing notes (`council:custodes`, `council:pax`, etc.), not raw tmux pane ids.
- Prefer tmuxctld `/reconcile` or `tmuxctl assert-instance` over bare `tmux respawn-pane`.
- Restart tmuxctld only for daemon maintenance/recovery, not routine validation.

## Triage

```bash
# List seat labels and dead/live state.
tmux list-panes -t main:council -F 'idx=#{pane_index} label=#{@PANE_ID} type=#{@PANE_TYPE} dead=#{pane_dead} status=#{pane_dead_status} cmd=#{pane_current_command}'
tmux list-panes -t main:mechanicus -F 'idx=#{pane_index} label=#{@PANE_ID} type=#{@PANE_TYPE} dead=#{pane_dead} status=#{pane_dead_status} cmd=#{pane_current_command}'

# Check daemon and event hook.
curl -sS --max-time 3 http://127.0.0.1:7778/health
tmux show-hooks -g | grep 'pane-died'
```

## Manual revive

1. Confirm the seat is a must-fill persona label: `council:custodes`, `council:malcador`, `council:administratum`, `council:pax`, `mechanicus:fabricator-general`, or `mechanicus:orchestrator`.
1. Trigger the daemon reconciler:

```bash
curl -sS --max-time 20 -X POST http://127.0.0.1:7778/reconcile \
  -H 'Content-Type: application/json' -d '{"session":"main"}'
```

1. If only one seat needs attention, assert it directly:

```bash
tmuxctl assert-instance --pane council:custodes
```

1. Re-list panes and verify dead persona seats are no longer dead. If respawn exits with status `127`, check that `tmuxctl.assertions.PERSONA_SEAT_SHIM` resolves to `cli-tools/scripts/persona-seat.sh`.

## Real fix verification

- `tmuxctld/lib/tmuxctl/daemon.py` must install global `pane-died[90]` to POST `/event event=pane-died`.
- `/health` should reassert the hook on its throttled heartbeat.
- `tmuxctld/lib/tmuxctl/service.py::handle_event` should route `pane-died` on must-fill persona labels to `assert_instance`.
- Focused tests:

```bash
python -m pytest cli-tools/tests/test_tmuxctld_reconcile.py cli-tools/tests/test_tmuxctld_daemon.py -q
python -m pytest cli-tools/tests/test_tmuxctl_persona_assert.py -q
```
