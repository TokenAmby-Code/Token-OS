# Terminus Ops Cockpit

Status: pilot live as of 2026-05-25.

The ops cockpit is a separate Vite React TypeScript frontend served by Token-API at:

- Local Mac: `http://localhost:7777/ui/ops`
- Remote/Tailscale clients: `$TOKEN_API_URL/ui/ops` or the Mac Mini MagicDNS equivalent
- Aggregate read model: `GET /api/ui/ops/state`

It is the live dashboard surface. Do not add parallel live dashboard mechanisms; extend this cockpit and its typed/API boundary instead.

## Runtime model

Token-API serves the committed Vite build from `token-api/ui/ops`; no Node runtime is required under the LaunchAgent. The source app lives in `token-api/web/ops`.

```
token-api/web/ops/     # Vite + React + TypeScript source
token-api/ui/ops/      # committed production build served by FastAPI
```

Fonts are **self-hosted** (latin woff2 subsets in `web/ops/src/fonts/`, bundled into the build) so the cockpit makes no external font requests at runtime — important for Tailscale/offline framing. Display: Chakra Petch · data/labels: IBM Plex Mono · body: IBM Plex Sans.

FastAPI routes:

- `GET /ui/ops` returns `ui/ops/index.html`.
- `GET /ui/ops/{asset_path}` returns built assets, guarded against path traversal and arbitrary file exposure.
- `GET /api/ui/ops/state` returns the aggregate cockpit read model.

The frontend polls `/api/ui/ops/state` every 2 seconds. Live acceptance confirmed that the browser surface updates when desktop/phone attention state changes, including opening a distraction.

## Frontend architecture

Data access is centralized and typed; deeply-nested components never call endpoints directly.

```
web/ops/src/
  main.tsx                  # root render
  App.tsx                   # cockpit shell, panel composition, conn/loading/error states
  styles.css                # "cogitator console" design system (tokens + chrome)
  types.ts                  # OpsState, TimerHistory, OpsGraph contracts
  api.ts                    # typed polling hooks: useOpsState / useTimerHistory / useOpsGraph
  format.ts                 # display-only formatting helpers
  modes.ts                  # visual language: mode/status/edge/node -> color + label
  mock.ts                   # mocked OpsGraph only; timer history is live
  fonts/                    # self-hosted woff2 + generated fonts.css
  components/
    TopStrip.tsx            # glanceable persistent strip
    TimerGraph.tsx          # bespoke SVG balance chart (segmented bands, tape X axis)
    InstancesPanel.tsx      # fleet table (desktop) + card stack (mobile)
    SidePanels.tsx          # attention evidence, event stream, subsystem status cards
    OpsGraph.tsx            # bespoke SVG layered directed graph
```

The graph components are bespoke SVG (no chart/graph library) to keep the committed, Node-less build lean and give full control over the design language. React Flow / uPlot remain the eventual recommendation if graph size or interaction demands outgrow the hand-rolled renderers (see the design brief).

### Polling cadence

- `useOpsState` — `/api/ui/ops/state` every **2s** (live posture).
- `useTimerHistory` — `/api/ui/ops/timer/history` every **30s** (slow; live, no mock fallback).
- `useOpsGraph` — `/api/ui/ops/graph/{name}` every **60s** (on-demand cadence; falls back to mock only on endpoint failure).

### Design language (`modes.ts` + `styles.css`)

`modes.ts` is the single source of truth for state → color. Timer modes map to `--m-working` (phosphor green), `--m-multi` (cyan), `--m-distracted` (hazard red), `--m-break` (amber), `--m-idle` (gray), `--m-sleep` (violet). Break balance reads green above the zero line, hazard-red below. Stale instances, blocked edges, and down subsystems use the hazard tone; victory/completed use brass/gold. Components must read colors from these helpers, not hardcode them.

### State assertions

The cockpit exposes state assertions near the top because the operator should never infer what the system believes from raw fields. Each assertion has `id`, `label`, `value`, `status`, `confidence`, `evidence[]`, `freshness_seconds`, `correction_hint`, and `details`. The first set covers timer mode, break balance, productivity, desktop attention, phone attention, fleet, enforcement, and TTS.

### Timer graph specifics

- **X axis**: spans from the day-start (currently hardcoded `DAY_START_HOUR = 7 / DAY_START_MINUTE = 20` in `api.ts`) to now, so it compresses as the day fills rather than scrolling a fixed window. Tape-measure ticks — labeled `HH:00` on the hour, medium mark at `:30`, minor marks at `:15`/`:45`.
- **Y axis**: signed break balance, quantized to **quarter-hour** steps chosen from a ladder of 15-min multiples (never a blind divide); the domain snaps to that step so top/bottom/zero land on clean values. Zero line is drawn prominently but unlabeled.
- Hover crosshair shows timestamp, mode, balance, productivity, desktop mode, phone app.

> **Day-start is hardcoded to 07:20 for MVP.** It should eventually be read from the Token-API state payload, but that depends on finishing the morning-session and Hatch alarm-clock integration so the server publishes an authoritative day-start. Until then the constant in `api.ts` is the single place to change it; it lines up with the 7 AM timer daily reset; the graph display now starts at 07:20.

### Live and mocked read-models

`GET /api/ui/ops/timer/history` is live. It reconstructs a bucketed line from `timer_shifts` plus the current `TimerEngine` snapshot and returns exact mode segments/annotations where persisted shifts exist. `useTimerHistory` has no mock fallback; fake timer data is worse than an explicit degraded state.

`GET /api/ui/ops/graph/active-fleet` is live, with `/api/ui/ops/graph/active` as an alias. It returns the current active fleet topology: host devices, active instances, session-doc bindings, live pane bindings when tmux metadata is available, and canonical chapter-command edges.

`GET /api/ui/ops/graph/golden-throne` is live, with `/api/ui/ops/graph/gt` as an alias. It returns read-only Golden Throne topology: GT-bound instances, their session docs, marker/timer state, recent `golden_throne_*` events, and Golden-Throne-sourced expected acknowledgements. `useOpsGraph` stays generic and falls back to `mock.ts` only on endpoint failure. Proposed future graph shapes and the full graph spec live in `docs/ops-cockpit-frontend-design-brief.md`.

## Aggregate state contract

`/api/ui/ops/state` is the cockpit boundary. The frontend must not read SQLite or infer cockpit semantics by stitching many legacy endpoints together.

Current top-level keys:

- `surface`, `generated_at`
- `timer` — effective mode, activity layer, productivity signal, manual/focus flags, break balance/backlog, total work/break counters.
- `assertions` — plain-language state assertions: what Token-API believes is true, status/tone, confidence, evidence, source freshness, and correction hint.
- `attention` — desktop mode/work mode/AHK/geofence/Steam fields and phone app/distraction/heartbeat fields.
- `work_state` — cached work-state evidence from Token-API (`get_cached_work_state`).
- `instances` — active instance list, status/engine/legion counts, age/stale indicators, pane/session-doc metadata, workflow fields, zealotry, and Golden Throne next-fire/resume/victory fields.
- `events` — recent event log entries with parsed JSON details when possible.
- `cron` — cron availability, job counts, running count, last-24h runs, and a small job sample.
- `tts` — current item, queue lengths, backend, satellite availability, global mode.
- `enforcement` — pending acknowledgement count/sample and Pavlok summary.

## Development workflow

From `token-api/web/ops`:

```bash
npm install
npm run typecheck
npm run build
```

Build output lands in `token-api/ui/ops`.

Backend verification:

```bash
cd /Volumes/Imperium/runtimes/token-os/live/token-api
.venv/bin/python -m py_compile main.py
.venv/bin/pytest -q tests/test_ops_ui.py
curl -sf http://localhost:7777/api/ui/ops/state | jq .surface
curl -sf 'http://localhost:7777/api/ui/ops/timer/history?window=15m&bucket=60s' | jq '{points: (.points|length), segments: (.segments|length)}'
```

Restart the live service after changing backend or committed frontend build assets:

```bash
token-restart --from /Volumes/Imperium/runtimes/token-os/live/token-api
open http://localhost:7777/ui/ops
```

## Current limitations

- **Relationship graph scope is narrow.** The graph panel has live `active-fleet` and read-only `golden-throne` backends; broader enforcement causality and lineage graphs are still deferred.
- **Day-start hardcoded to 07:20.** Pending morning-session / Hatch alarm-clock integration that lets the server publish an authoritative day-start to read from state.
- Read-only surface; operational mutations should remain Token-API/CLI mutations invoked from tmux keybindings until deliberately designed.
- Built frontend assets are committed for this pilot to keep LaunchAgent runtime Python-only. No CSS framework — design system is hand-rolled CSS variables in `styles.css`.
