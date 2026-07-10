# Ops Cockpit Data Roadmap

Status: active planning note, 2026-07-09.

The ops cockpit is the live operator/debug rendering surface for Token-OS operational state. It is currently served by Token-API, but its long-term home is the TypeScript daemon surface that is expected to encompass the Discord daemon, ops cockpit, and eventually a TypeScript tmuxctld port.

## Ownership doctrine

The cockpit is a live render over lower-level authorities. It must not become a third state store.

- **Token-API (Python)** owns agent identity, lifecycle, session docs, timer/work state, enforcement, TTS policy/control, and other application-level facts.
- **tmuxctld (Python today; TypeScript eventually)** owns tmux mechanics, pane occupancy, pane delivery, wrapper ledger, typing guard, and tmux-facing projection facts.
- **Ops cockpit (TypeScript)** is permitted to display Token-API and tmuxctld data in the same place. It is closer to tmuxctld operationally than Token-API, but it has privileged read access to both.
- **Future TypeScript daemon** should host or own the cockpit shell and shared client-side contracts, but it should remain a passthrough/rendering system over Token-API and tmuxctld data surfaces.

The cockpit does not cloak lower-level errors. Accurate degraded/debug state is a product requirement, not noise.

## Correctness principles

1. **No independent persistence.** The cockpit may keep transient UI state needed for rendering/interaction, but it must not persist operational facts, repair data, or invent durable truth.
2. **No frontend endpoint stitching.** Components consume typed read models through centralized data access. They do not directly stitch arbitrary legacy endpoints.
3. **Typed passthrough, not sanitizing concealment.** TypeScript contracts enforce shape correctness while preserving lower-layer error fields, source names, stale/missing status, and debug details.
4. **Degraded real state beats fake state.** Missing tmuxctld, stale Token-API data, null TTS satellite health, ledger drift, and parse/schema mismatches must be visible.
5. **Authority boundaries stay explicit.** Token-API and tmuxctld each expose dedicated data surfaces. The cockpit can join them visually, but not silently choose a new authority.
6. **Debug-level propagation.** Read models should carry enough details for an operator to distinguish unavailable, stale, malformed, divergent, and policy-blocked states.

## Current authoritative surfaces

- Browser cockpit: `GET /ui/ops`
- Aggregate cockpit state: `GET /api/ui/ops/state`
- Agent/script status summary: `GET /api/ops/status`
- Timer history: `GET /api/ui/ops/timer/history`
- Graph read models: `GET /api/ui/ops/graph/{active-fleet,golden-throne}`
- tmuxctld health/ledger surfaces: tmuxctld loopback `/health`, `/ledger/rows`, `/ledger/resolve`, and related occupancy/resolver APIs

Current frontend source remains `token-api/web/ops/`; committed build remains `token-api/ui/ops/` until the TypeScript daemon host exists.

## Roadmap phases

### Phase 1 — TTS queue as a first-class live surface

Goal: make the existing live TTS data more accurate and operator-actionable without adding a new state source.

Planned work:

- Preserve and expose current/speaking, hot queue, pause queue, backend, global mode, and satellite health.
- Add sender metadata where cheaply available from Token-API authority, especially persona/commander fields needed by the UI.
- Ensure TTS queue rendering does not infer missing facts from active instances when the backend can expose them directly.
- Carry backend errors and unknown satellite state through to dials/tooltips/drawers.
- Keep existing Token-API-routed controls: skip, promote, play-pane, and global mode.

Validation:

- `npm run typecheck`
- `npm run build`
- TTS-focused tests where shape/control behavior changes
- `tests/test_ops_ui.py` if aggregate contract changes

### Phase 2 — State dials from existing real facts

Goal: replace placeholder/unwired state dials with real read-model projections where `/api/ui/ops/state` already has facts.

Initial dial targets:

- TTS: speaking/queued/idle/degraded from `tts` + `sources.tts`.
- Enforcement: pending count, Pavlok summary, source/error state from `enforcement` + `sources.enforcement`.
- Golden Throne: due/resume/victory signals derived from active instance GT fields and recent GT events.
- Device/source health: Mac/WSL/mesh should remain explicit unknown/unwired unless a real Token-API or tmuxctld fact exists.

Rules:

- Dials should be hoverable/clickable by type contract.
- Default click opens/focuses details; specific dials may override with Token-API controls.
- Placeholder dials are acceptable only when explicitly labeled as not wired or data unavailable.
- Do not convert errors into neutral UI copy.

### Phase 3 — tmuxctld occupancy read model for compass (partially implemented)

Goal: expose pane occupancy through Token-API/tmuxctld data surfaces so the cockpit compass can render real pane state.

Planned backend shape:

- Token-API collector reads tmuxctld occupancy/ledger surfaces with short timeouts.
- `/api/ui/ops/state` now exposes a typed `tmux.occupancy` summary with counts, cells, errors, and degraded status.
- `/api/ops/status` exposes concise counts for agent/script reads.
- The read model distinguishes:
  - reachable/unreachable tmuxctld
  - total panes
  - occupied/free/protected/dead panes
  - ledger-bound instance ids
  - ledger/sniff/projection drift
  - source errors and stale/missing status

Boundary rule: tmuxctld remains the occupancy authority; Token-API only brokers/interprets for consumers.

### Phase 4 — Compass UI wiring

Goal: render pane occupancy as a cockpit/debug surface.

Planned work:

- Added a typed frontend adapter from `OpsState.tmux.occupancy` to compass stars; remaining visual refinement can map exact pane geometry later.
- Show free/occupied/protected/dead/drift states directly.
- Surface lower-level errors in the compass, not only global health.
- Never fetch tmuxctld directly from the browser.

### Phase 5 — Worker/idle lifecycle queues

Goal: evolve from registration chips to operational lifecycle lanes.

This is intentionally later because it requires stricter semantics than simple registration.

Candidate lanes:

- registered
- working/processing
- idle
- stale
- blocked/waiting
- speaking/queued as an overlay, not the lifecycle owner

Required before implementation:

- Define which authority owns each lifecycle claim.
- Ensure idle is not confused with pane occupancy or liveness.
- Preserve the existing worker rail signal: chip presence means successful registration.

## Error propagation contract

Every new data surface should carry enough detail for the cockpit to render precise failure state:

```ts
type DataSurfaceStatus = {
  status: 'ok' | 'warn' | 'bad' | 'unknown';
  available: boolean | null;
  stale: boolean | null;
  source: 'token-api' | 'tmuxctld' | 'discord-daemon' | string;
  message: string | null;
  errors?: Array<{
    code?: string;
    message: string;
    detail?: unknown;
  }>;
  generated_at?: string | null;
};
```

This is not necessarily the exact final shared type. It records the invariant: errors are data, not rendering exceptions to hide.

## Near-term implementation order

1. Phase 1 TTS queue polish.
2. Phase 2 state dials from existing facts.
3. Phase 3 tmuxctld occupancy read model.
4. Phase 4 compass UI.
5. Phase 5 worker/idle lifecycle queues.

Do not begin Phase 5 until the cockpit can already display TTS, state-dial, and pane-occupancy truth without inventing state.
