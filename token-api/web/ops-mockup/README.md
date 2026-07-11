# Ops Cockpit — static design study

A **static, transplant-ready** React + TypeScript mockup of the Token-OS Ops
Cockpit's next visual pass. It is **not** wired to Token-API: no polling, no
mutations, no backend semantics. Everything renders from frozen mock data.

Built from `docs/ops-cockpit-static-mockup-brief.md` + the operator's hand-drawn
cockpit sketch, in the established **cogitator console** design language
(warm graphite + oxidized brass, phosphor telemetry, hazard debt striping;
self-hosted Chakra Petch / IBM Plex Mono / IBM Plex Sans — no external font
requests). The green in the sketch is pen ink, not the palette.

## Run it

```bash
npm install
npm run dev        # http://localhost:5199
npm run typecheck  # strict, clean
```

## What it shows

- **Timer field (the signature).** A full-bleed break-balance graph where the
  zero line is a *horizon*: break credit is calm phosphor sky above it, break
  debt is hazard-striped terrain the operator has sunk into below it. Frozen
  data crosses zero twice and troughs at −38 min around 14:10, so the debt is
  obvious at a glance. Segmented mode bands (working / multitask / distracted /
  break / idle) sit behind the line; a solid mode ribbon runs along the base.
- **Instrument binnacle (top-right).** An `X · Y` graph-legend anchor with the
  noteworthy state dials strung along a drawn brass arc. A left rail carries the
  three primary posture dials (timer / balance / phone).
- **Edge drawer stacks.** Left = voice / chapter-lock; right = state-dial
  catalog. Blank placeholder circles whose count is driven by the two demo
  sliders — density affordance only, no drawer behaviour.
- **Active fleet** as horizontal instrument bars, sorted by urgency
  (processing+talking → blocked → stale → processing → idle).
- **Evidence grid**: state assertions, attention evidence, event stream,
  subsystem cards, and a small directed relationship-graph placeholder.

## Files (transplant order)

- `src/mockCockpitData.ts` — all frozen, deterministic data + explicit local
  types (`MockTimerMode`, `MockTimerPoint`, `MockModeSegment`, `StackDemoState`,
  plus fleet / dial / assertion / event / subsystem shapes). Deliberately echoes
  the live `TimerHistory` / `OpsState` / `CockpitLayoutModel` contracts so the
  component boundaries survive a port. Swap this module for typed `api.ts` hooks.
- `src/MockOpsCockpit.tsx` — the root component and all sub-components
  (`TimerField`, `Binnacle`, `Ring`, `DrawerStack`, `FleetBar`,
  `RelationshipGraph`). Bespoke SVG throughout, matching the live cockpit's
  Node-less build stance (no chart/graph libraries).
- `src/cockpit.css` — the design language. Tokens mirror the live
  `token-api/web/ops/src/styles.css` `--m-*` / brass / phosphor / hazard set.
- `src/fonts/` — the same self-hosted woff2 subsets the live cockpit ships.

## Quality floor

Responsive to phone width (Android-over-Tailscale is a supported frame: the
binnacle unfans into a row, drawers become horizontal, the grid stacks),
`prefers-reduced-motion` honoured (sigil spin + drawer-pull pulse disabled),
keyboard focus visible on the sliders, and a labelled a11y tree.

## Scope guardrails (per brief — intentionally NOT done)

No Token-API fetches, no recreated backend semantics, no functional drawers, no
fake live controls or fake mutation success. This is a direction study that
points toward the sketch, not the production cockpit.
