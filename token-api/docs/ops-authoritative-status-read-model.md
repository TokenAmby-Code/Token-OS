# Ops Authoritative Status Read Model

Status: planning contract, 2026-07-01. This is the forward path for retiring legacy status commands and making the ops cockpit data plane the canonical operational read source.

## Decision

Agents that need to assess Token-OS state should read from Token-API's ops read model, not from scattered CLI tools, SQLite queries, raw tmux/tmuxctl probes, or ad-hoc endpoint stitching.

Token-API becomes the privileged status broker because it can join:

- Token-API registry/session/timer/enforcement/TTS state.
- tmuxctld live pane/lifecycle/projection state.
- Read-model assertions already built for `/ui/ops`.

Tmuxctld remains the loopback authority for tmux-side mechanics. Agents should not normally call tmuxctld directly for status; Token-API should call tmuxctld behind the read model and publish the joined interpretation.

## Non-goal: preserving old CLI status commands

We do **not** need to preserve CLI tools that have outlived their usefulness. We should also avoid creating new CLI wrappers that merely ping endpoints unless a real operator workflow proves they are necessary.

Migration stance:

- Old commands may be deleted, 410'd, or temporarily reduced to docs pointing at the canonical endpoint.
- Thin CLI wrappers are allowed only when they add durable ergonomics beyond `curl` / `token-ping` and have a named user path.
- The endpoint contract is the product; the CLI is not the source of truth.

## Canonical endpoint family

Primary agent/CLI read:

```http
GET /api/ops/status
```

Browser cockpit read remains:

```http
GET /api/ui/ops/state
```

Both should drink from the same internal aggregation layer. `/api/ui/ops/state` can remain display-rich; `/api/ops/status` should be concise, flat, stable, and optimized for agents and scripts.

Optional scoped endpoints may exist later when proven useful:

```http
GET /api/ops/assertions
GET /api/ops/fleet
GET /api/ops/tmux-projection
GET /api/ops/current-instance?pid=...&cwd=...
```

These must share builders/selectors with `/api/ops/status`; they must not become new independent stitching paths.

## Internal builder shape

Add an internal ops read-model layer along these lines:

```py
build_ops_facts()           # source-normalized flat facts, no UI presentation
build_ops_assertions()      # status/assertion engine over facts
build_ops_status()          # concise agent/status contract
build_ops_ui_state()        # rich cockpit state contract
build_ops_layout_hints()    # optional backend-provided layout hints if needed later
```

The rule is: source joins and semantic state classification happen once. Browser components, agents, and future surfaces consume typed projections from that shared layer.

## Proposed `/api/ops/status` shape

```ts
type OpsStatus = {
  surface: 'ops-status';
  generated_at: string;
  status: OpsSeverity;
  summary: string;
  sources: OpsSourceMap;
  timer: TimerStatusFact;
  attention: AttentionStatusFact;
  fleet: FleetStatusFact;
  tmux: TmuxProjectionStatusFact;
  tts: TtsStatusFact;
  enforcement: EnforcementStatusFact;
  assertions: OpsAssertion[];
  recommended_actions: OpsRecommendedAction[];
};
```

Use explicit source health and freshness everywhere stale data could mislead:

```ts
type OpsSeverity = 'ok' | 'warn' | 'bad' | 'unknown';

type OpsSourceHealth = {
  ok: boolean;
  status: OpsSeverity;
  freshness_seconds: number | null;
  generated_at: string | null;
  error: string | null;
};

type OpsSourceMap = {
  token_api: OpsSourceHealth;
  tmuxctld: OpsSourceHealth;
  agents_db: OpsSourceHealth;
  timer_db: OpsSourceHealth;
  satellite?: OpsSourceHealth;
};
```

Tmux projection must publish stable labels and interpretation, not raw `%pane` identifiers in human/agent summaries:

```ts
type TmuxProjectionStatusFact = {
  reachable: boolean;
  live_panes: number | null;
  bound_instances: number;
  unbound_live_panes: number;
  dead_or_missing_panes: number;
  projection_drift: number;
  active_pane_label: string | null;
};
```

## Frontend TypeScript impact

The robust flat data layer should make frontend contracts stronger, not looser.

Current `/ui/ops` types mirror a large aggregate `OpsState` and then derive layout in `layoutModel.ts`. That is the right V1 direction. Once `/api/ops/status` / flat facts exist, the frontend can become more explicit:

1. **Separate source facts from presentation.**
   - `OpsFacts` / `OpsStatus` describes canonical system truth.
   - `CockpitLayoutModel` describes what the cockpit chooses to show.
   - Components consume layout/view-model types, not raw backend miscellany.

2. **Use discriminated unions for unavailable/stale data.**
   Avoid ambiguous `null`/optional chains where source state matters.

   ```ts
   type Fresh<T> = { state: 'fresh'; value: T; freshness_seconds: number };
   type Stale<T> = { state: 'stale'; value: T | null; freshness_seconds: number; reason: string };
   type Unavailable = { state: 'unavailable'; value: null; reason: string };
   type OpsFact<T> = Fresh<T> | Stale<T> | Unavailable;
   ```

3. **Make severity and actionability first-class.**
   Frontend predicates should not rediscover what is bad/warn/expected. The backend assertion engine can publish severity and recommended actions; the frontend layout model can decide prominence.

   ```ts
   type OpsAssertion = {
     id: string;
     label: string;
     value: string;
     severity: OpsSeverity;
     confidence: 'high' | 'medium' | 'low';
     source_ids: string[];
     evidence: string[];
     actionability: 'none' | 'operator' | 'agent' | 'system';
     recommended_action_id: string | null;
   };
   ```

4. **Flatten high-churn status summaries.**
   Keep detailed nested data available where needed, but publish agent/cockpit summary fields as stable facts. This reduces component-level defensive programming and makes tests simpler.

5. **Generate or validate types at the API boundary.**
   Prefer OpenAPI-derived TypeScript or a small runtime validator at `api.ts` for the canonical status endpoints. `api.ts` remains the only fetch/mutation boundary.

6. **Keep layout derivation centralized.**
   `layoutModel.ts` should evolve from hand-written frontend predicates toward selectors over backend-classified facts:

   ```ts
   type CockpitLayoutModel = {
     noteworthyDials: NoteworthyDial[];
     hiddenDialCatalog: HiddenDial[];
     activeTtsWaiters: ActiveTtsWaiter[];
     drawerSummaries: DrawerRailSummary[];
     supportingAssertions: OpsAssertion[];
   };
   ```

   The frontend can still decide what is visible, but it should not decide whether tmux projection drift is real, whether a row is stale, or whether a TTS queue is degraded.

## Practical frontend contract direction

Near-term:

- Keep `OpsState` for `/api/ui/ops/state`.
- Add `OpsStatus` types for `/api/ops/status` once the endpoint exists.
- Keep `layoutModel.ts` as the single frontend selector layer.
- Add tests that assert normal facts hide normal dials and degraded facts become noteworthy.

Medium-term:

- Refactor `OpsState` to include a `facts` or `status` section shared with `/api/ops/status`.
- Move repeated status concepts to shared TS types:
  - `OpsSeverity`
  - `OpsSourceHealth`
  - `OpsAssertion`
  - `OpsRecommendedAction`
  - `FleetStatusFact`
  - `TmuxProjectionStatusFact`
- Make UI-only types smaller and more descriptive:
  - `NoteworthyDial`
  - `DrawerRailSummary`
  - `TimerFieldViewModel`
  - `FleetAttentionRow`

Long-term:

- `/api/ui/ops/state` becomes a cockpit projection over canonical ops facts.
- `/api/ops/status` becomes the default agent/status read.
- Legacy commands either disappear or explicitly document the endpoint they have been replaced by.

## Implementation principles

- Observation-first: degraded real state beats fake green state.
- Token-API owns joined interpretation; tmuxctld owns tmux mechanics.
- No human/agent status surface should require raw tmux identifiers.
- No direct DB reads for routine operational status.
- No new endpoint-pinger CLI unless a concrete workflow proves it necessary.
- Strong contracts beat permissive blobs: prefer explicit severities, freshness, and source metadata.


## Development phase map

Treat this as a data-plane migration, not a UI feature.

### Phase 0 — freeze the target contract

Define and commit the first `/api/ops/status` contract before large implementation churn.

Deliverables:

- Stable doc/test fixture for the first response shape.
- Explicit top-level sections: `status`, `sources`, `timer`, `attention`, `fleet`, `tmux`, `tts`, `enforcement`, `assertions`, `recommended_actions`.
- Clear degraded-source behavior for tmuxctld unavailable, DB unavailable, stale timer data, and satellite unavailable.
- Decision on whether `/api/ops/status` includes only summary fields initially or also compact noteworthy lists.

Exit criterion: a future worker can implement against a reviewed contract without inventing shape mid-code.

### Phase 1 — construct the shared data layer

Extract the existing ops-cockpit aggregation into reusable internal builders without changing `/api/ui/ops/state` behavior.

Target builder split:

```py
build_ops_facts()           # source-normalized facts, minimal presentation
build_ops_assertions()      # degradation/actionability classification
build_ops_status()          # concise agent/status projection
build_ops_ui_state()        # existing browser cockpit projection
```

Deliverables:

- Existing `/api/ui/ops/state` still passes its tests and serves the cockpit.
- New `/api/ops/status` exists and uses the same facts/assertions.
- Source health/freshness metadata is present even when degraded.
- No new CLI wrapper unless a concrete workflow proves it necessary.

Exit criterion: browser and agent/status reads drink from one backend truth layer.

### Phase 2 — join tmuxctld projection behind Token-API

Add tmuxctld-derived projection facts to the shared layer. Token-API owns the joined interpretation; tmuxctld owns tmux mechanics.

Deliverables:

- tmuxctld reachability and source freshness.
- live pane count, bound instance count, dead/missing/unbound counts, projection drift count.
- stable active pane label where available.
- degraded facts when tmuxctld is unavailable; no raw `%pane` identifiers in human/agent summaries.

Exit criterion: statusline/persona/pane-binding class problems have a canonical read-model seam instead of ad-hoc pane lookups.

### Phase 3 — tighten frontend TypeScript contracts

Once flat facts exist, strengthen frontend contracts around canonical state and presentation view models.

Deliverables:

- Add `OpsStatus`, `OpsSourceHealth`, `OpsAssertion`, `OpsRecommendedAction`, `FleetStatusFact`, `TmuxProjectionStatusFact` types.
- Refactor `layoutModel.ts` to consume backend-classified facts where possible.
- Keep components consuming view-models (`NoteworthyDial`, `DrawerRailSummary`, `TimerFieldViewModel`) instead of raw source miscellany.
- Add unit coverage proving degraded facts become noteworthy and normal facts remain hidden.

Exit criterion: frontend predicates are selectors over authoritative facts, not independent semantic classifiers.

### Phase 4 — endpoint and CLI census

Inventory existing GET/status endpoints and CLI status tools after the canonical read model is functional.

Classification buckets:

1. canonical data source
2. mutation/control
3. debug-only
4. legacy duplicate
5. dead one-off

Deliverables:

- OpenAPI/route inventory with disposition.
- CLI inventory with disposition.
- Candidate removal list grouped by safe delete, 410-on-touch, debug-only, and keep.

Exit criterion: every status-ish read path has a documented future: canonical, scoped, debug-only, or dead.

### Phase 5 — cull aggressively after parity

After `/api/ops/status` proves it covers real agent/operator needs, remove obsolete access paths.

Deliverables:

- Delete or 410 stale one-off GET endpoints.
- Delete obsolete CLI status tools rather than preserving wrappers by default.
- Update docs/agent guidance to point at `/api/ops/status` and `/api/ui/ops/state`.
- Remove duplicate frontend/CLI data stitching.

Exit criterion: routine state assessment has one privileged data plane, with scoped/debug exceptions deliberately named.

## First implementation slice

1. Extract current ops state aggregation into reusable builders without changing `/api/ui/ops/state`.
2. Add `GET /api/ops/status` using those builders plus tmuxctld projection health.
3. Add TypeScript `OpsStatus` contracts beside existing `OpsState` contracts.
4. Update docs and agent guidance: state assessment uses `/api/ops/status` or `/api/ui/ops/state`, not old status commands.
5. Retire or 410 obsolete status commands as they are touched.
