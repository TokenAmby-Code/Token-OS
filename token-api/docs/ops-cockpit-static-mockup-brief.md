# Ops Cockpit Static Mockup Brief

Status: prompt handoff for a frontend mockup bot. This is **not** the live cockpit implementation contract.

## Reference material

- Live/source cockpit: `token-api/web/ops/`
- Live contract docs: `token-api/docs/ops-cockpit.md`
- Design brief: `token-api/docs/ops-cockpit-frontend-design-brief.md`
- Plane sketch source: `/Volumes/Imperium/Imperium-ENV/Excalidraw/Drawing 2026-06-20 10.54.39.excalidraw.md`
- Pasted image reference: **not found in `/tmp` or the current worktree during this update**. If the operator provides it later, add the file path here and pass it to the mockup bot.

## Prompt for frontend mockup bot

You are building a **static, TypeScript-ready React mockup** for the Token-OS Ops Cockpit next visual pass. Do not wire to Token-API, do not poll, and do not implement mutations. The output should be easy to transplant into `token-api/web/ops` later: React + TypeScript components, typed mock data, CSS modules/plain CSS acceptable, no backend dependencies.

Use the plane Excalidraw sketch as the directional reference:

`/Volumes/Imperium/Imperium-ENV/Excalidraw/Drawing 2026-06-20 10.54.39.excalidraw.md`

If a pasted image is supplied by the operator, treat it as the strongest visual reference. If not, proceed from the Excalidraw text/sketch and the current live cockpit files.

### Visual goal

Create a dramatic but static mockup of the ops cockpit layout:

1. A dominant **top timer field**, not a chart inside a bordered card.
   - Frozen timer data only.
   - Show several mode shifts across the day: `working`, `multitasking`, `distracted`, `break`, `idle`.
   - Include visible break backlog/debt: the line should go below zero for part of the graph.
   - Use segmented background mode bands and a prominent zero line.
2. A floating/edge visual language inspired by the sketch.
   - Left side: TTS / chapter-lock drawer placeholder stack.
   - Right side: state-dial drawer placeholder stack.
   - These are visual affordances only; no drawer behavior.
3. Two demo sliders:
   - `TTS queue` slider controls how many **blank circles** render in the left stack.
   - `State dials` slider controls how many **blank circles** render in the right stack.
   - The circles do not track real data. They exist only to demonstrate stack density and visual rhythm.
4. Main surface content below the timer field:
   - Active fleet area higher than evidence/status panels.
   - Compact state assertion chips/cards as supporting content, not dominant content.
   - A small active TTS/current speaker strip may appear, but full queue/chapter-lock details belong behind the left stack concept.

### TypeScript shape

Use explicit local types even though data is mocked:

```ts
type MockTimerMode = 'working' | 'multitasking' | 'distracted' | 'break' | 'idle';

type MockTimerPoint = {
  t: string;
  mode: MockTimerMode;
  breakBalanceMinutes: number;
};

type MockModeSegment = {
  start: string;
  end: string;
  mode: MockTimerMode;
};

type StackDemoState = {
  ttsQueueCircles: number;
  stateDialCircles: number;
};
```

Keep mock data in a separate file such as `mockCockpitData.ts` so the component structure is ready for real `OpsState` / `CockpitLayoutModel` later.

### Interaction scope

Allowed:

- Slider state for demo circle counts.
- Hover styles/tooltips if cheap.
- Responsive CSS enough to show the layout does not collapse.

Forbidden:

- Fetching Token-API.
- Recreating backend semantics.
- Making the drawer rails functional.
- Adding fake live controls or fake mutation success states.

### Deliverables

Produce a self-contained mockup package or patch with:

- `MockOpsCockpit.tsx` or equivalent root component.
- `mockCockpitData.ts` typed frozen timer data.
- CSS for timer field, rail stacks, blank circles, sliders, fleet/evidence blocks.
- A short README explaining how to run/view it and noting it is static-only.

### Acceptance check

A reviewer should be able to glance at the mockup and see:

- Timer field dominates the top.
- Break backlog/debt is obvious.
- Left and right stack density changes when sliders move.
- The layout points toward the Excalidraw sketch without pretending to be the final production cockpit.


### Dial interaction design invariant for the mockup

Represent state dials as icon-only circles in the floating stacks. Do not render subtitles under the circles in the HUD/stack view. The dial data should still include subtitle/tooltip text, but that text is revealed only by hover tooltip or in an expanded drawer-style presentation.

Assume every dial is clickable in the TypeScript shape:

- default click target: side drawer entry;
- TTS item click target: play/promote TTS;
- productivity/distraction click target: sources modal.

For the static mockup, the click behavior can be no-op or a local visual state. The important deliverable is the type and visual assumption: state dials are interactive icon-only controls, not labeled mini cards.
