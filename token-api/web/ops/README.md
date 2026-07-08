# Ops Cockpit

THE live Token-OS ops cockpit — served at `/ui/ops` from the committed build in
`token-api/ui/ops`. React + TypeScript + Vite, in the **cogitator console**
design language (warm graphite + oxidized brass, phosphor telemetry, hazard
debt striping; self-hosted Chakra Petch / IBM Plex Mono / IBM Plex Sans — no
external font requests). Promoted from the `ops-mockup` design study: same
visuals, live data spine.

## Run it

```bash
npm install
npm run dev        # http://localhost:5199 — proxies /api to localhost:7777
npm run typecheck  # tsc -b --noEmit (strict, must be clean)
npm run build      # tsc -b && vite build → emits into ../../ui/ops
```

## Live data spine

Two Token-API read-models, polled from `src/api.ts` (the only fetch layer):

- `GET /api/ui/ops/state` — every 2s. Timer mode + break balance, phone/desktop
  attention, per-subsystem source health, the TTS current/hot/pause queues, and
  the active-instance roster (used to resolve TTS sender personas).
- `GET /api/ui/ops/timer/history?window=<since 07:20>s&bucket=60s` — every 30s.
  Balance points + mode segments for the timer field; the window grows from the
  07:20 day anchor so the graph compresses as the day fills.

`src/cockpitData.ts` holds the pure adapters (`mapMode`, `toTimerPoints`,
`toModeSegments`, `buildDials`, `toTtsQueue`) that project those contracts onto
the cockpit's render models. The root component (`src/OpsCockpit.tsx`) memoizes
one `CockpitData` object and provides it via context (`useCockpitData()`); no
component fetches on its own.

## What's wired (phase 1)

- **Timer field** — live balance line + coalesced mode bands from timer
  history, with a live now-point appended from the state feed so the head of
  the line moves between history polls. The break hub's rim laps/ball/glow
  render the same live signed balance.
- **State dials** — timer (live mode), balance (signed minutes), phone
  (foreground app / distraction; click force-clears stuck phone attention via
  `POST /api/ui/ops/phone/clear`), desktop (attention mode), cron and TTS
  (source health → nominal / degraded / down).
- **TTS stack** — the live serialized speak queue: current utterance at the
  head (reverberating while it's on the wire), hot then pause queues below, in
  order. Senders resolve to persona heraldry via the instance roster.
- **Degraded state** — while a feed is loading or erroring, a fixed hazard chip
  declares it and the instruments render empty/last-good real data. Nothing is
  ever fabricated to look healthy; unwired dials read an explicit `—`.

## What's phase 2 (intentionally NOT wired)

Fleet lifecycle (worker queues, idle-worker rail + clock, lemon persona
sections), the enforce / gt / mac / wsl / mesh dials, the corner-dial fraction,
and the right drawer's full catalog. Those surfaces still run on the demobar's
demo knobs and render honest placeholders where they surface values.

## Quality floor

Responsive via the single `uiScale` factor (see STATE.md), a11y-labelled,
`prefers-reduced-motion` honoured, `npm run typecheck` and `npm run build`
clean (`noUnusedLocals` / `noUnusedParameters` / `exactOptionalPropertyTypes`).
