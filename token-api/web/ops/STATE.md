# Ops Cockpit — Active State

**Last updated:** 2026-07-08
**Location:** `token-api/web/ops` — THE live cockpit, served at `/ui/ops` from the
committed build in `token-api/ui/ops`. Promoted from the `ops-mockup` design study.

**Status: LIVE, phase 1.** Timer graph + timer/balance/phone/desktop/cron/tts dials
+ TTS stack are wired to `GET /api/ui/ops/state` (2s) and
`GET /api/ui/ops/timer/history` (30s) through `src/api.ts` → `src/cockpitData.ts`
adapters → one `CockpitData` context. Fleet/worker surfaces, enforce/gt dials, and
the drawer catalog stay unwired (phase 2) and render honest placeholders/demo knobs.
Vite + React + TypeScript. `noUnusedLocals` / `noUnusedParameters` are on.

## Run it

```bash
cd token-api/web/ops
npm run dev        # Vite dev server (default :5199 per vite.config.ts)
npm run typecheck  # tsc -b --noEmit  (must be clean)
npm run build      # tsc -b && vite build
```

## Critical files

- `src/OpsCockpit.tsx` — the whole cockpit (timer field, break hub + arc,
  persona/worker dials, TTS stack, corner dial, demobar, placement tool).
- `src/cockpit.css` — all styling.
- `src/cockpitData.ts` — cockpit render-model types + pure live-contract adapters.
- `src/api.ts` / `src/types.ts` — Token-API polling hooks + typed contracts
  (transplanted from the retired old cockpit app).

## What's live in the current build

### Generic screen-size resilience — `uiScale` (NEW, this checkpoint)

The cluster was authored entirely at a **1440px design width** (`DESIGN_W`). One
viewport-derived factor now shrinks the whole instrument assembly coherently at any
narrower width:

- `uiScale = clamp(scaleMin, vp.w / DESIGN_W, 1)` — **capped at 1** (never upscales
  past the authored design; ≥1440 usable width ⇒ `uiScale === 1`, **pixel-identical
  to the pre-resilience desktop tuning** — verified: `--hub-r` 260px, `--corner-dial-d`
  104px, nothing moved) and **floored at `scaleMin`** (default `SCALE_MIN = 0.45`, a
  live demo-bar "Scale floor" knob + live `uiScale` readout for tuning by eye).
- **Generic (does the work):** base constants stay authored at 1440 (single source);
  every instrument LENGTH multiplies by `uiScale` at the point of consumption, while
  ANGLES and every `*Frac` ratio stay invariant → proportional scaling is
  geometrically exact (dials keep nesting in the rim, the arc keeps meeting it).
  - `.page` publishes scaled `--hub-r` / `--hub-shift` / `--hub-bottom` /
    `--corner-dial-d` **and the raw `--ui-scale`** factor; CSS instrument text/insets
    scale via `calc(<base>px * var(--ui-scale))` (`.ring__glyph`, `.persona-icon`,
    `.agent-dial__idx`, `.corner-dial__num/__bar`, `.break-marker`, `.place-dial__idx`).
  - JS layers take a `scale` param: `graphRightBorderPx`, `computeFacade`,
    `rimOffset`, `dialOffset`, `createArc`, `breakHubView`; `TimerField` / `ArcLayer` /
    `Dials` / `TtsStack` take `uiScale` and scale their length constants. `arcPath` /
    `lapPath` / `computeBounds` untouched (break-trail rides the CSS-scaled SVG).
- **Isolated (only where generics can't):** ONE `RESPONSIVE OVERRIDES` block at the
  bottom of `cockpit.css` (`--bp-narrow: 720px`) — currently holds ONLY the demo-bar
  collapse (`.demobar__toggle` / `.demobar__body`, `demoOpen` state). The old ad-hoc
  `@media(max-width:720px){.dials{scale .82}}` one-off is retired. Future
  portrait reflows / data-driven per-size tweaks belong HERE, nowhere else.

**KNOWN MOBILE ISSUE (not yet fixed — next tuning target):** on the phone the **timer
graph midline (the horizon / break-even line) sits too LOW on the screen.** The plot
height is `--graph-h = 27vh` and the horizon is a fixed fraction (`Y_MAX/(Y_MAX−Y_MIN)
= 50/95 ≈ 0.526`) of it — deliberately vh-locked / NOT `uiScale`-scaled (it's a data
mapping, not an instrument length). On a tall portrait phone that fraction lands the
midline too far down. Fix is a **mobile tuning** of the graph band (likely the `27vh`
height and/or the `Y_MIN/Y_MAX` window, or a portrait override in the RESPONSIVE
OVERRIDES block) — NOT a change to the `uiScale` pipeline, which is settled. Desktop
must stay pixel-identical.

### Persona dials on the arc (fixed roster — NOT a demo knob)

- `PERSONA_COUNT = 6`. Slots march LEFT off the pocket anchor: `1` (rightmost,
  Custodes) … `6` (leftmost, newest).
- Size classes (`PERSONA_SIZES`, k=0→"1" … k=5→"6"):
  `MEDIUM, LARGE, LARGE, LARGE, MEDIUM, SMALL` (radii 45 / 50 / 45 / 39). The row
  tapers heaviest at the centre trio, lighter at the `1`/`5` ends, lightest at `6`.
- `PERSONA_STEP_GAPS = [106, 112, 112, 106, 93]` — constant arc-length centre-to-
  centre, order `[1→2, 2→3, 3→4, 4→5, 5→6]`. Viewport-independent (roster never
  spreads with width).
- `AGENT_RIM_GAP = 4` — buffer between the `1` ring and the reserved overflow
  column. NOTE: the anchor is `min(column limit, hub limit)`; at 1440 the **hub
  clearance (`AGENT_HUB_GAP = 6`) may be the binding constraint**, so changing
  `AGENT_RIM_GAP` alone can produce little/no visible shift of `1`. If `1` needs to
  move right, check which constraint is binding in `ringClears()` first.

### Worker dials (flat row below the arc)

- `WORKER_R = 34` (~20% down from the old 42). Right-anchored, fill right→left, wrap
  into further right-anchored rows trailing down the RHS. Live knobs in the demobar:
  Worker count / Worker row X / Worker row Y.

### Coordinate-capture placement tool (authoring aid) — `PlaceLayer`

- Demobar **"Place mode"** checkbox. ON → a full-bleed `.place-layer`
  (`pointer-events:auto`, crosshair, z:65 — above the fan/arc, below the demobar)
  captures clicks; OFF → `pointer-events:none`, clicks pass through to the cockpit.
- Each click drops a **TTS-sized** (`PLACE_DIAL_PX = 36`, r18) numbered ring at the
  cursor and records `{x, y}` (viewport-top px, valid at scroll-top). A faint dashed
  connector threads the drops in placement order (reads as a queue/path).
- Drops persist across reload via `usePersistedJSON('placedDials', [])`
  (localStorage `ops-mock:placedDials`). Place mode itself is ephemeral (resets off
  on reload — by design).
- Demobar readout: live count + JSON box. Each entry annotated with
  `xFromRight = clientWidth − x` (the layout is right-anchored, so captures replay
  width-independently). Buttons: **Copy** (clipboard), **Undo last** (pop),
  **Clear** (empty).
- The captured coords are the **source for a hand-authored "little"/commander-
  assigned dial layout** — that consumption step is NOT built yet.

#### First-draft capture set (from the operator, at 1440 width — two staggered rows)

```json
[
  { "x": 960, "y": 302, "xFromRight": 71 },
  { "x": 884, "y": 325, "xFromRight": 147 },
  { "x": 852, "y": 304, "xFromRight": 179 },
  { "x": 795, "y": 322, "xFromRight": 236 },
  { "x": 750, "y": 301, "xFromRight": 281 },
  { "x": 700, "y": 320, "xFromRight": 331 },
  { "x": 646, "y": 302, "xFromRight": 385 },
  { "x": 609, "y": 321, "xFromRight": 422 },
  { "x": 539, "y": 304, "xFromRight": 492 },
  { "x": 513, "y": 323, "xFromRight": 518 }
]
```


### Resize/scaling note — 2026-07-09

Future cockpit resize/scaling reports should start from the cockpit's own
viewport path:

- The cockpit derives layout from `document.documentElement.clientWidth` /
  `clientHeight` and the `uiScale` pipeline.
- The `Scale floor` knob persists in the browser profile through the
  `localStorage` key family `ops-mock:*`.
- Same-profile reloads should preserve those keys; if sizing appears to reset,
  verify the active browser profile, viewport dimensions, and stored scale keys
  before changing layout code.
- Phone-vs-desktop cockpit tuning should be handled as explicit viewport/device
  classes if it returns, not as global hard-coded dimensions.

## Open threads / next steps

0. **Mobile display tuning (ACTIVE)** — `uiScale` makes the cluster fit the phone,
   but portrait composition still needs tuning. First target: **timer-graph midline
   sits too low** (see KNOWN MOBILE ISSUE above). Desktop (`uiScale === 1`) is a
   verified checkpoint and must not regress — keep mobile tweaks in the RESPONSIVE
   OVERRIDES block / vh-window, not in the shared `uiScale` math.
1. **Consume the captured coords** into a real "little" (commander-assigned) dial
   layer — the follow-up the placement tool was built to feed. Use `xFromRight` so
   it re-pins width-independently. Not started.
2. **`1`-dial RHS buffer** — operator wanted it tighter and didn't see the
   `AGENT_RIM_GAP` 8→4 change move anything. Likely hub-clearance-bound (see note
   above); revisit by identifying the binding constraint, not by nudging one gap.
3. No drag-to-adjust of existing drops yet (click-to-drop + Undo only).

## Verify checklist (last run: clean)

- `npm run typecheck` + `npm run build` clean.
- **`uiScale`:** clientWidth ≥ 1440 ⇒ `uiScale === 1`, `--hub-r` 260px /
  `--corner-dial-d` 104px (desktop pixel-identical, never upscales); ~477px ⇒ floored
  0.45, whole cluster shrinks welded (personas nest in rim, arc springs off rim to
  x=0, no h-overflow), demo bar collapses to its `▸ controls` toggle. No console errs.
- Place mode toggles capture on/off; drops land TTS-sized + numbered + connected;
  Copy/Undo/Clear work; drops persist across reload; toggle-off passes clicks
  through.
- Persona row reads `6 5 4 3 2 1` left→right with the tapered size classes.
