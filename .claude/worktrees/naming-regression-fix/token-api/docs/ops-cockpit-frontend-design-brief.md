# Ops Cockpit Frontend Design Brief

Audience: frontend-design agent taking over the `/ui/ops` Terminus cockpit.
Status: pilot route is live and verified as of 2026-05-25.

## Current surface

- Route: `GET /ui/ops`
- Source: `token-api/web/ops` — Vite + React + TypeScript.
- Build output: `token-api/ui/ops` — committed static assets served by FastAPI; LaunchAgent does not need Node.
- Data boundary: `GET /api/ui/ops/state`, polled every 2 seconds.
- Live behavior verified: UI updates when desktop/phone attention state changes, including opening a distraction.

Do not add a parallel live dashboard mechanism for this cockpit; extend `/ui/ops` and its typed/API boundary instead.

## Product goal

Turn the placeholder cockpit into a dense but readable operations surface for:

1. Operator timer/break posture.
2. Attention/distraction state across desktop and phone.
3. Active agent fleet state.
4. Recent operational events.
5. Cron / Golden Throne / enforcement visibility.
6. Eventually: arbitrary graph views for agent/session/task relationships.

This is a dashboard first, control surface second. Mutations should remain Token-API/CLI/tmux-mediated until explicitly designed.

## Design principles

- **Glanceable first:** the operator should know “am I working, in break debt, distracted, or blocked?” in <2 seconds.
- **Dense but calm:** this is an operations cockpit, not a marketing app. Use hierarchy, spacing, and muted color rather than animation noise.
- **Mobile readable:** Android over Tailscale is a supported viewing frame. Tables need responsive alternatives/cards.
- **State provenance visible:** show why Token-API thinks a state is true, not just the derived conclusion.
- **Avoid semantic inference in frontend:** consume `/api/ui/ops/state`; request backend fields if needed.
- **Graph components should accept normalized typed data:** nodes/edges/time-series arrays, not raw backend rows.

## Current state contract highlights

`/api/ui/ops/state` top-level keys:

- `timer`
- `attention`
- `work_state`
- `instances`
- `events`
- `cron`
- `tts`
- `enforcement`

See `docs/ops-cockpit.md` for the full contract summary.

## Recommended layout v2

### Top strip

Persistent, single-row when desktop width allows:

- Timer mode badge: WORKING / MULTITASKING / DISTRACTED / BREAK / IDLE / SLEEPING / QUIET.
- Break balance: signed duration, color-coded positive/neutral/debt.
- Desktop attention: mode + app/source.
- Phone attention: current app + distracted/clear.
- Active fleet count + stale count.
- Enforcement pending count.

### Main panels

1. **Timer graph** — primary visual anchor.
2. **Active instances** — table/card hybrid with stale, pane, session-doc, zealotry, GT next fire/victory.
3. **Attention evidence** — compact desktop/phone/work-state facts.
4. **Events timeline** — recent event stream.
5. **Cron/GT/enforcement summary** — compact status cards.
6. **Graph workspace** — node/edge graph view once backend provides graph read models.

## Timer graph concept

Desired visualization: two-axis line chart with segmented background shading.

### Purpose

Show timer posture over time, making it obvious when the operator earned break, spent break, entered debt, or was distracted.

### Data needed

Current `/api/ui/ops/state` is a snapshot. For a real graph, add a backend history endpoint/read-model, for example:

```http
GET /api/ui/ops/timer/history?window=6h&bucket=60s
```

Recommended response shape:

```ts
type TimerHistory = {
  generated_at: string;
  window_seconds: number;
  bucket_seconds: number;
  points: Array<{
    t: string;                    // ISO timestamp
    break_balance_ms: number;     // left axis
    total_work_time_ms?: number;  // optional cumulative/reference
    productivity_active: boolean;
    desktop_mode?: string | null;
    phone_app?: string | null;
    mode: string;                 // derived timer mode
  }>;
  segments: Array<{
    start: string;
    end: string;
    mode: string;
    activity: string;
    source?: string | null;
  }>;
};
```

### Axes

- X axis: time.
- Left Y axis: signed break balance in minutes/hours; zero line must be prominent.
- Right Y axis: optional intensity/activity score if needed later, e.g. distraction intensity, processing instance count, or focus count. Do not add a right axis until it carries a real signal.

### Lines

Minimum viable:

- Break balance line: signed, continuous, green above zero, red below zero or single line with threshold coloring.
- Optional second line: active agent count or distraction intensity on right axis.

Avoid plotting too many lines. The background segments should carry categorical mode; the line should carry numeric balance.

### Background segmented shading

Use vertical bands behind the line:

- WORKING: subtle green/blue.
- MULTITASKING: amber/blue blend.
- DISTRACTED: red translucent.
- BREAK: amber/red.
- IDLE: gray.
- SLEEPING/QUIET: dark purple/gray.

Segments should come from backend mode-transition history, not frontend reconstruction from sampled points when possible.

### Interaction

- Hover/crosshair: timestamp, mode, balance, desktop mode, phone app, productivity active.
- Click/drag range: later, allow zoom/select; v1 can be static.
- Mobile: horizontal scroll or simplified sparkline with tap tooltip.

### Library recommendation

For v2, prefer one of:

- `visx` / `d3-scale` for custom cockpit-quality charts.
- `uPlot` for fast time-series with custom background bands.
- `Recharts` only if speed-to-acceptable matters more than bespoke control.

Given the desired segmented shading and dual-axis behavior, `uPlot` or `visx` is likely better than a high-level chart library.

## Arbitrary node/edge graph concept

Desired visualization: arbitrary directed graphs with optional undirected edges, typed nodes, directional edges, and scalable layout.

### Use cases

- Agent ↔ session doc bindings.
- Parent/subagent relations.
- Cron job → run → instance → session doc.
- Golden Throne follow-up chains.
- Event causality and enforcement cascades.
- Vault/task dependency graphs later.

### Backend read-model shape

Add graph-specific aggregate endpoints rather than making frontend infer relationships from instance/event tables.

```http
GET /api/ui/ops/graph/{graph_name}
```

Recommended normalized shape:

```ts
type OpsGraph = {
  graph: string;
  generated_at: string;
  layout_hint?: 'force' | 'dagre' | 'elk' | 'radial';
  nodes: Array<{
    id: string;
    type: string;          // instance, session_doc, cron_job, event, device, etc.
    label: string;
    subtitle?: string;
    status?: string;
    group?: string;
    weight?: number;
    href?: string;
    data?: Record<string, unknown>;
  }>;
  edges: Array<{
    id: string;
    source: string;
    target: string;
    type: string;          // bound_to, spawned, resumed_by, caused, blocks, etc.
    directed: boolean;
    label?: string;
    status?: string;
    weight?: number;
    data?: Record<string, unknown>;
  }>;
};
```

### Directional edge rendering

- Directed edges need arrowheads at target.
- Edge label should sit on the path midpoint when zoomed enough.
- Edge type should determine stroke style:
  - solid: strong/current relation.
  - dashed: historical/soft relation.
  - dotted: inferred/weak relation.
- Edge status should determine color or opacity:
  - active/current: bright.
  - stale: muted/dashed.
  - blocked/error: red.
  - completed/victory: green/gold.

### Layout strategy

Support multiple layout modes; no single graph layout fits every operational graph.

- **DAG / lineage graphs:** use ELK or Dagre, left-to-right or top-to-bottom. Best for cron → instance → session doc → event chains.
- **Force graph:** useful for exploratory relationship maps, but can be unstable. Pin positions after layout to avoid constant jitter.
- **Radial:** useful for one selected node and its neighborhood.
- **Manual/remembered positions:** later, persist operator-arranged layouts by graph name.

### Library recommendation

Best candidates:

- **React Flow**: best practical fit for directed operational graphs, custom nodes, arrow edges, minimap, controls, selection, and future interaction.
- **Cytoscape.js**: better for large graph analysis and graph algorithms, less React-native UI feel.
- **Sigma.js/Graphology**: good for very large network exploration, less suited to custom cockpit node cards.

Recommendation: start with React Flow for cockpit graph panels. Use ELK/Dagre layout for directed graphs. Revisit Cytoscape/Sigma only if graph size or graph algorithms become dominant.

### Interaction model

- Click node: side inspector with full data and linked actions/read-only details.
- Click edge: side inspector with relation type, timestamps, provenance, source fields.
- Filter by node type, edge type, status, stale/current.
- Search node labels.
- Toggle direction labels/arrowheads for dense views.
- Neighborhood mode: focus selected node, dim unrelated graph.

### Performance guardrails

- Keep initial graphs small: 100–300 nodes max for React Flow.
- Backend should support graph scopes: `active`, `recent`, `instance/{id}`, `cron/{id}`, `session-doc/{id}`.
- Frontend should virtualize side lists/tables, but graph canvas itself should be bounded.
- Do not poll large graph endpoints every 2 seconds. Poll summary state often; refresh graph on demand or at a slower cadence.

## Immediate design-agent tasks

1. Replace placeholder metrics with a polished responsive cockpit layout.
2. Improve active instance table/card design for desktop and mobile.
3. Add empty/loading/error states that are operationally useful.
4. Define visual language for timer modes, stale states, GT state, enforcement state.
5. Produce a timer graph component API with mocked history data while backend history endpoint is built.
6. Produce a graph component API with mocked `OpsGraph` data and directional edges.
7. Keep all data fetching centralized and typed; avoid ad-hoc endpoint calls from deeply nested components.

## Implementation status (2026-05-25)

All seven immediate tasks delivered. The cockpit is live at `/ui/ops`.

- **Aesthetic:** "cogitator console" — warm graphite + oxidized brass, phosphor telemetry, hazard debt striping. Self-hosted Chakra Petch / IBM Plex Mono / IBM Plex Sans (no external font requests).
- **Layout (1):** responsive shell — persistent glanceable top strip, feature timer graph, fleet panel, attention/events split, subsystem status cards, relationship graph.
- **Instances (2):** `InstancesPanel` renders a table on desktop and a card stack under ~760px from one data source; zealotry pips, GT victory/armed states, stale tagging.
- **States (3):** boot/loading bar, no-state error, per-feed empties; connection indicator distinguishes nominal / retrying / signal-lost.
- **Visual language (4):** centralized in `modes.ts` + CSS `--m-*` tokens (see `docs/ops-cockpit.md` → Design language).
- **Timer graph (5):** bespoke SVG (`TimerGraph.tsx`) consuming the live `TimerHistory` contract. Segmented mode bands, threshold-colored balance line (green/hazard split at zero), quarter-hour-quantized Y axis, tape-measure X axis from day-start, hover crosshair. Band text and legend were removed to avoid overlap during rapid mode swaps; mode is conveyed by color bands + hover tooltip.
- **Graph (6):** bespoke SVG layered directed graph (`OpsGraph.tsx`) consuming the `OpsGraph` contract via mocked data. Arrowheads, status-styled edges (solid/dashed/dotted + color), click-to-inspect, type filters, neighborhood focus.
- **Data layer (7):** `api.ts` typed polling hooks (`useOpsState`/`useTimerHistory`/`useOpsGraph`); `types.ts` holds all three contracts. Timer history is live with no mock fallback. Graph still falls back transparently and shows a `· mock` tag until the graph endpoint exists.

### Library deviation

The brief recommends React Flow (graph) and uPlot/visx (timer). Both were implemented as **bespoke SVG** instead, to keep the committed Node-less build lean and own the design language end-to-end. Revisit React Flow if operational graphs exceed ~300 nodes or demand pan/zoom/minimap interaction beyond the current renderer.

### Deferred / blocked

- `GET /api/ui/ops/timer/history` backend is built and live. `GET /api/ui/ops/graph/{name}` still needs a backend; the component contract is final and mock-swappable.
- **Day-start** is hardcoded to `DAY_START_HOUR = 7 / DAY_START_MINUTE = 20` in `api.ts`. It should be read from the state payload once morning-session + Hatch alarm-clock integration publishes an authoritative day-start. It currently lines up with the 7 AM timer daily reset; the graph display now starts at 07:20.
