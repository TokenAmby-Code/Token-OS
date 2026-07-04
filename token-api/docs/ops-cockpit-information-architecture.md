# Ops Cockpit Information Architecture

Status: working design notes, 2026-05-26.

The `/ui/ops` cockpit should answer operational questions, not mirror database tables. Token-API should provide a small set of read models that map directly to visual components.

## Primary operator questions

1. **What mode am I in right now?**
   - Timer effective mode, break balance/debt, manual/focus state.
2. **Why does the system think that?**
   - Desktop mode, phone app, productivity evidence, live agent evidence.
3. **What changed recently?**
   - Timer mode transitions, attention changes, instance lifecycle, enforcement/GT events.
4. **Which agents need attention?**
   - Processing/idle/stale/blocked, session-doc linkage, next required action, GT next fire.
5. **What system loops are active?**
   - Cron, Golden Throne, enforcement/Pavlok, TTS queue.
6. **How are things related?**
   - Instance/session-doc/cron/event graphs with directional edges and provenance.


## Current contract status — 2026-07-04

- `GET /api/ui/ops/state` is the aggregate cockpit boundary and currently exposes `contract_version`, `health`, `sources`, top-level `recommended_actions`, top-level `source_freshness`, top-level `voice_drafts`, and direct `tmux` health.
- `GET /api/ops/status` exists for concise agent/script reads and shares the same fact/assertion/recommended-action builders as the browser state.
- `GET /api/ui/ops/graph/active-fleet` and `GET /api/ui/ops/graph/golden-throne` are live read models, with `/active` and `/gt` aliases. Enforcement causality and broader lineage graphs remain deferred.
- Frontend data fetching should stay centralized in `api.ts`; a `layoutModel.ts` selector/presentation bridge should be added before production UI components wire predicates over raw `OpsState` health/freshness fields.

## Display hierarchy

### 1. Command strip — always visible

A compact sticky top strip for current posture:

- Timer mode badge.
- Signed break balance.
- Desktop attention: `mode`, Steam/app detail if present, AHK reachability.
- Phone attention: app + distracted/clear + heartbeat age.
- Fleet: active count, processing count, stale count.
- Enforcement: pending acknowledgement count.

Use strong color only for state that requires attention: debt, distraction, stale, pending enforcement, broken heartbeat.

### 1.5. State assertions — truth table

Immediately below the command strip, show explicit assertions in plain language. This is the cockpit's answer to: "what does the system think is true?" Each assertion should include:

- asserted value, e.g. `Timer mode: WORKING`;
- status/tone: good/warn/bad/neutral;
- confidence: high/medium/low;
- evidence lines, e.g. `activity=working`, `productivity_active=true`;
- source freshness, where applicable;
- correction hint, so the operator knows exactly what to fix.

Assertions are deliberately redundant with raw panels. They are the primary display for precise correction; raw panels are supporting evidence.

### 2. Timer history — primary graph

This should be the main high-fidelity visual that the TUI could not provide.

#### Visual model

- X axis: local time.
- Left Y axis: signed break balance.
- Right Y axis: secondary signal, defaulting to active agent count or distraction intensity.
- Background shading: segmented timer mode bands.
- Zero line: prominent horizontal break-even line.
- Event annotations: small markers for mode changes, phone app open/close, desktop detection changes, enforcement, GT resume/fire, manual timer actions.

#### Recommended default overlays

- Main line: `break_balance_ms` converted to minutes/hours.
- Secondary line: `active_instance_count` or `processing_recent_count`.
- Background bands: effective timer mode segments.
- Marker lanes below chart:
  - phone distraction events.
  - desktop detection changes.
  - enforcement / expected-ack events.
  - GT resume/fire/victory events.

A lane of event markers is often more readable than forcing everything onto a numeric right axis. Keep the right axis for one real continuous/count signal.

#### Live backend endpoint

```http
GET /api/ui/ops/timer/history?window=6h&bucket=60s
```

Proposed response:

```ts
type TimerHistory = {
  generated_at: string;
  window_seconds: number;
  bucket_seconds: number;
  points: Array<{
    t: string;
    break_balance_ms: number;
    mode: string;
    activity: string;
    productivity_active: boolean;
    active_instance_count: number;
    processing_recent_count: number;
    desktop_mode?: string | null;
    phone_app?: string | null;
  }>;
  segments: Array<{
    start: string;
    end: string;
    mode: string;
    activity?: string;
    productivity_active?: boolean;
    reason?: string | null;
  }>;
  annotations: Array<{
    id: string;
    t: string;
    lane: 'timer' | 'desktop' | 'phone' | 'enforcement' | 'gt' | 'instance';
    type: string;
    label: string;
    severity?: 'info' | 'warn' | 'bad' | 'good';
    details?: Record<string, unknown>;
  }>;
};
```

#### Aggregation rules

- Bucket numeric samples by last-known or average depending on field.
- Preserve mode transitions exactly as `segments`; do not infer bands from bucketed points in the frontend.
- Preserve annotations as event points; do not flatten into line samples.
- Include gap markers if the server was down or no samples exist.

### 3. Fleet attention table/cards

The active instance list should be sorted by operational urgency, not only recency.

Suggested sort priority:

1. Processing and stale.
2. Processing recent.
3. Enforcement/GT due soon.
4. Blocked workflow / next required action present.
5. Idle stale.
6. Normal idle.

Each row/card should show:

- Human surface: display name, pane label, engine, device.
- State: processing/idle, age, stale reason.
- Continuity: session doc title/path/status/policy/binding source.
- Work state: workflow state, next required action, stop allowed.
- GT: zealotry, next fire, resume count, victory state.
- Provenance affordance: link/expand for recent events and mutations for that instance.

### 4. Evidence panel

A compact explanation panel for derived system state:

- Timer mode derivation: activity layer, productivity layer, manual layer.
- Work-state evidence: recent work action, recent processing/pane activity within 3 minutes, observed panes, processing recent count.
- Attention evidence: desktop detector, phone foreground app, heartbeat age, geofence.
- Data freshness: generated-at and age of the most important source signals.

This is the antidote to “dashboard says X but why?”.

### 5. Event stream / timeline

Recent events should be grouped and filtered, not raw-only.

Default groups:

- Attention: desktop/phone/timer events.
- Fleet: instance register/rename/stop/activity/stale.
- GT: scheduled/fire/resume/victory/enforcement.
- Cron: job start/finish/victory/failure.
- Enforcement: expected ack created/escalated/resolved, Pavlok, phone block.
- TTS/notification.

Display as a timeline with lane icons and severity. Raw JSON details should be expandable.

### 6. System loop cards

Separate compact cards for always-running loops:

- Cron: enabled/running/runs last 24h, recent failures, next due jobs.
- Golden Throne: pending timers, due soon, recent resumes, second-resume enforcement.
- Enforcement: pending acks, Pavlok state, cooldown/cap, recent escalations.
- TTS: backend, satellite availability, current item, queue length.
- Device health: Mac, WSL, phone heartbeat/reachability when available.

## Graph views

Graph views should be separate read models. The frontend should not derive arbitrary graph topology from the snapshot endpoint.

### Candidate graphs

1. **Active fleet graph**
   - Devices → tmux panes → instances → session docs.
2. **Continuity graph**
   - Session docs → linked instances → workflow events → next action owners.
3. **Golden Throne graph**
   - Instance/session doc → GT timer → resume attempts → enforcement/victory.
4. **Cron execution graph**
   - Cron job → run → spawned instance → output/victory/session doc.
5. **Enforcement causality graph**
   - Distraction event → expected acknowledgement → notification/enforce/Pavlok → resolution.
6. **Event causality graph**
   - Selected event and its local neighborhood by correlation IDs, instance IDs, and timestamps.

### Graph read-model endpoint

```http
GET /api/ui/ops/graph/{graph_name}?scope=active&root_id=...
```

Proposed response:

```ts
type OpsGraph = {
  graph: string;
  generated_at: string;
  scope: string;
  layout_hint?: 'dag' | 'force' | 'radial' | 'manual';
  nodes: Array<{
    id: string;
    type: string;
    label: string;
    subtitle?: string;
    status?: string;
    group?: string;
    weight?: number;
    data?: Record<string, unknown>;
  }>;
  edges: Array<{
    id: string;
    source: string;
    target: string;
    type: string;
    directed: boolean;
    label?: string;
    status?: string;
    weight?: number;
    data?: Record<string, unknown>;
  }>;
};
```

### Directional edge semantics

- `spawned`: parent instance → child instance.
- `bound_to`: instance → session doc.
- `runs`: cron job → cron run.
- `launched`: cron run/dispatch → instance.
- `scheduled`: instance/session doc → GT timer.
- `resumed`: GT timer → instance/resume pane.
- `caused`: event → event.
- `blocked_by`: work item → blocker.
- `ack_required`: enforcement source → expected ack.
- `resolved_by`: expected ack → resolution event.

Use arrowheads for all directed edges. Use edge color/status for active, stale, blocked, failed, completed/victory.

## Backend/read-model priorities

1. Done: `GET /api/ui/ops/timer/history` for high-fidelity timer graph. Next improvement: persist richer source freshness/gap metadata.
2. Done: top-level `source_freshness` on `/api/ui/ops/state` so stale sensors are visually obvious.
3. Done: per-instance `attention_rank` / `attention_reasons` to simplify urgency sorting.
4. Done: live graph endpoint for `active-fleet` (`/api/ui/ops/graph/active-fleet`, alias `/active`).
5. Done: live graph endpoint for `golden-throne` (`/api/ui/ops/graph/golden-throne`, alias `/gt`). Enforcement causality remains deferred.

## Frontend display priorities

1. Timer graph with mode bands and annotations.
2. Better active-instance urgency display.
3. Evidence panel for derived state.
4. Event stream with filters/lanes.
5. Directed graph viewer with small scoped graphs first.


## Work activity decay policy

As of 2026-05-26, productivity should not remain active just because an idle agent pane exists. Qualifying work signals have a 3-minute grace window:

- `/api/work-action` (Stream Deck / CLI / manual signal),
- PromptSubmit hook,
- AskUserQuestion answer hook,
- recent processing/agent pane activity,
- tmux typing guard detecting pending input.

After 3 minutes without a qualifying signal, TimerEngine receives `set_productivity(False)` and enters IDLE if activity is otherwise working. IDLE then has a 7-minute timeout before auto-break. Total normal grace from last work signal to break is about 10 minutes. If activity is still a distraction when productivity drops, TimerEngine's v2 rules may derive BREAK immediately because `inactive + distraction => BREAK`.
