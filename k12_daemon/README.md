# k12_daemon

tmuxctld-successor daemon for k12 boxes: the authoritative, event-sourced tmux
control plane (Bun/TypeScript). Door step 1 skeleton — see the ruled spec
`Mars/Tasks/k12-daemon-spec.md` (§1–§12) for the full design.

## What it is

- **Event-sourced core.** One append-only SQLite stream is the single source of
  truth; the three day-one read models (`current_bindings`, `freelist`,
  `activity_board`) are pure projections rebuilt by replay — nobody writes them.
- **Canonical-id membrane.** Raw tmux `%id`s never cross upward. Every response,
  log line, and event is scrubbed (`assertNoTmuxId`); a breach fails loud.
- **Send chokepoint.** Enqueue-by-default; typed gate/refusal reasons; the tmux
  client-activity (typing) guard is read at the decision point at BOTH admission
  and drain — no keystroke hooks. Each receipt carries the send's own resolution.
- **Reconcile = replay.** Out-of-band pane death surfaces as a
  `contradiction_flagged` event (p0, fail-loud in bring-up mode), never a
  silently synthesized lifecycle.

## HTTP surface (spec §7 + rung-3 registration door)

Nine honest endpoints, bound to loopback only. Ingress is via the per-box
`edge_proxy` ONLY (see below) — the daemon never faces the tailnet directly. The
seed six are spec §7; `/close`, `/stop`, and `/subscribe` are the rung-3
registration-door trio: the generic close system, the stop-hook's door, and the
generic stop-hook subscription. Composing `/subscribe` (action `close`) with
`/stop` yields `final message → auto-close on next stop-hook` — no bespoke latch.

| Method | Path                     | Purpose                                   |
|--------|--------------------------|-------------------------------------------|
| GET    | `/health`                | Honest liveness + build + tmux reachability |
| POST   | `/launch`                | Atomic reg-audited seat bind / handover   |
| POST   | `/send`                  | Send chokepoint (enqueue-by-default)      |
| POST   | `/close`                 | Generic close: reap process, keep estate pane, seat → freelist |
| POST   | `/stop`                  | Stop-hook door: record / dedupe / refuse-ghost; fires auto-close |
| POST   | `/subscribe`             | Bound-keyed close-on-next-stop subscription (satiated-once) |
| POST   | `/reconcile`             | Replay-driven reconcile; p0 on contradiction |
| GET    | `/entities`              | `activity_board` projection (collection)  |
| GET    | `/entities/:id/events`   | Per-entity event stream                   |

Collection routes are registered before parameterized ones (the
`/api/instances/all` shadowing lesson); the ordering is data and is asserted by
a committed route-shadow test.

## Ingress — edge_proxy only

Per spec §12 (RULED): one edge proxy per box is the box's front door. The daemon
is reached through the box `edge_proxy` (`:7780`) under the `/k12` route prefix,
which strips the prefix and forwards to the daemon (`:7781`):

```text
/k12/health           → 127.0.0.1:7781 /health
/k12/launch           → 127.0.0.1:7781 /launch
/k12/entities         → 127.0.0.1:7781 /entities
/k12/entities/:id/events → 127.0.0.1:7781 /entities/:id/events
```

The daemon reads the `x-edge-proxy` header set by the proxy as its transport
receipt (woven into event provenance). See `edge_proxy/README.md` for the
per-route config shape and route-scoped auth.

## Config (spec §1, B1)

Configuration is env/config-driven — no hardcoded machine values. A JSON file
pointed at by `K12_DAEMON_CONFIG` wins; otherwise env vars; otherwise the
localhost-safe defaults. Keys (see `k12_daemon.config.example.json`):

| Key          | Env                        | Default                                    |
|--------------|----------------------------|--------------------------------------------|
| `bind`       | `K12_DAEMON_BIND`          | `127.0.0.1`                                |
| `port`       | `K12_DAEMON_PORT`          | `7781`                                     |
| `machine`    | `IMPERIUM_MACHINE`         | **none — fail loud** (never guess the box) |
| `dbPath`     | `K12_DAEMON_DB`            | `$HOME/runtimes/database/k12_daemon.events.sqlite` |
| `tmuxSocket` | `K12_DAEMON_TMUX_SOCKET`   | `k12`                                      |

`machine` has **no default**: a daemon that guesses its own box identity is a
bug, so config load fails loud when it is unset.

## Install / development

Bun-native — TypeScript source runs directly, no build step. The daemon depends
on `@token-os/contracts` via a `file:` link whose source `import 'zod'` must
resolve from the contracts source path. Reproduce a green install/test in **two
frozen steps** (this is the B1 reproduce path):

```bash
# 1. Install contracts (so its src/ can resolve `import 'zod'` for test + tsc)
cd token-api/web/contracts && bun install --frozen-lockfile

# 2. Install + verify the daemon
cd k12_daemon
bun install --frozen-lockfile
bun test
bunx tsc --noEmit
bun src/daemon.ts   # run
```

Note: the runtime (`bun src/daemon.ts`) resolves zod via the daemon's own
`node_modules` copy even without step 1; only `bun test` and `tsc --noEmit`
resolve zod from the contracts source path and therefore need step 1.

## Deployment — systemd `--user`

Mirrors the `edge_proxy` unit (reboot survival proven before acceptance):

```bash
# On the box, as the service user:
mkdir -p ~/.config/systemd/user
cp ~/runtimes/Token-OS/live/k12_daemon/systemd/k12-daemon.service ~/.config/systemd/user/
# Provision config (machine identity is mandatory):
install -Dm600 /dev/stdin ~/secrets/token-os/k12_daemon.json <<'JSON'
{ "bind": "127.0.0.1", "port": 7781, "machine": "k12-personal",
  "dbPath": "/home/<user>/runtimes/database/k12_daemon.events.sqlite",
  "tmuxSocket": "k12" }
JSON
systemctl --user daemon-reload
systemctl --user enable --now k12-daemon.service
loginctl enable-linger "$USER"   # survive logout / reboot
```
