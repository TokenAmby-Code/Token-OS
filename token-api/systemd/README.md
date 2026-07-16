# Token-API systemd user units (k12 boxes)

Source of truth for the units running on the k12 boxes (target-structure §5:
unit files are git-tracked in the checkout; install is an explicit copy step,
the same relationship the launchd plists have to the repo on the Mac).

Captured verbatim from the live k12-personal install (rung 3, 2026-07-16).

| File | Purpose |
|---|---|
| `token-api.socket` | Socket activation on `127.0.0.1:7777` (box-local single-door shape — external traffic enters via edge_proxy) |
| `token-api@.service` | Template unit; `%i` selects the deploy checkout (`live` / `battlefield`) with its own env + DB dir |

## Install (per box, once)

```bash
cp token-api/systemd/token-api.socket token-api/systemd/token-api@.service ~/.config/systemd/user/
loginctl enable-linger "$USER"        # non-negotiable on a headless box (§5)
systemctl --user daemon-reload
systemctl --user enable --now token-api.socket   # template unit has no [Install]; the socket activates token-api@live on demand
```

Deploys never re-install units by default — `box-restart` restarts the units
whose files changed. If a unit file changes in a merge, re-copy + daemon-reload
explicitly.

There is no watchdog sidecar on the k12 boxes: `Restart=always` supervises,
and the durable hook→API outbox drains on token-api startup (the recovery
edge), replacing the Mac's tokenapi-watchdog drain leg.
