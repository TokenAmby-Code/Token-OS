# Ops Cockpit — Manual instance selection + Talking-instance overlay

**Branch:** `ops-cockpit-instance-expansion-pane-marker` · **Started:** 2026-06-10

Session/scope doc for the two related Ops Cockpit features. Updated as work proceeds.

## Goal

Two features built on **one** "select + expand an instance" primitive (do not fork two):

- **A — Manual instance selection.** Double-clicking a row in the Ops Cockpit fleet
  table selects that instance and expands it (expanded instance view). The selected
  instance's tmux pane gets a visual marker so the selection is reflected in tmux too.
- **B — Talking-instance overlay.** The cockpit already partially "expands whoever is
  currently talking." Validate and extend so it's reliable, reusing A's primitive.

**Out of scope (do NOT build):** cursor-follows-text highlighting (deferred), any TUI
work (deprecated), the CAN-BAN board (prototyped separately).

## What already exists (verified in code, 2026-06-10)

- **tmux focus/zoom primitive:** `routes/tts.py::_focus_and_zoom_pane(pane_id)` —
  `select-pane` + `resize-pane -Z` with zoom-dedup. Tested in
  `tests/test_tts_focus_snap.py`.
- **Talking auto-snap (B, tmux side):** `routes/tts.py::_snap_focus_to_speaker(item)`
  fires on the TTS playback `None→item` transition in `tts_queue_worker`. Local-only,
  with gates: voice-chat / discord backend / remote pane / no-pane / dead-pane → skip.
- **Existing pane marker idiom:** `_set_tts_state(pane_id, "speaking")` sets the
  `@TTS_STATE` pane option; rendered by `pane-border-format` in
  `cli-tools/tmux/tmux-base.conf` (alongside `@CC_STATE`, `@GT_FIRE`, `@CONTEXT_INFO`).
- **Frontend `focusPane(id)`** (`web/ops/src/api.ts`) POSTs
  `/api/instances/{id}/focus-pane` — **but that endpoint did not exist** (404). It is
  wired into `VoiceQueuePanel` row clicks today, so those silently fail.
- **No web-UI expansion exists** for either the selected or the talking instance.
  `InstancesPanel` does not even know who is talking. So "the cockpit already partially
  expands whoever is talking" = the **tmux** auto-snap only; there is no web counterpart.

## Design — the single mechanism

**tmux layer (one primitive):** `_focus_zoom_and_mark(pane_id)` = `_focus_and_zoom_pane`
+ `_set_ops_selected(pane_id)` (sets `@OPS_SELECTED`, clearing it from any other pane).
Both triggers call it:
- **B (talking):** `_snap_focus_to_speaker` (existing gates preserved) → `_focus_zoom_and_mark`.
- **A (manual):** new `POST /api/instances/{id}/focus-pane` → resolve pane → `_focus_zoom_and_mark`.

The `@OPS_SELECTED` pane option is the required "visual marker," rendered as a border
badge in `tmux-base.conf` (mirrors the `@TTS_STATE` idiom). Native zoom + active border
already reflect focus; the badge makes "selected" explicit and persistent.

**web layer (one mechanism):** a single `selectedInstanceId` + `selectionSource`
(`manual` | `talking`) in `App.tsx`. One `<ExpandedInstance>` detail card renders the
selected instance regardless of how it was selected. Both triggers feed the same state:
- **A:** double-click a fleet row/card → `selectInstance(id, 'manual')` → also `focusPane(id)`.
- **B:** effect on `tts.current.instance_id` *changing to a new value* →
  `selectInstance(id, 'talking')` (web only; backend auto-snap already did tmux + marker).

## Decisions (deliberate v1 choices)

1. **Last-writer-wins, on distinct talk events.** Talking re-selects only when the
   talking instance id *changes* (not on every 2s poll), so a manual double-click sticks
   until the next distinct speaker. This matches the existing unconditional tmux
   focus-snap and keeps web ↔ tmux consistent. Source tag shown in the expanded view.
2. **Manual click does focus+zoom too** (same primitive as B), not a marker-only set —
   faithful to "build on a single mechanism." Manual trigger bypasses the
   voice-chat/discord gates (explicit operator intent) but keeps local-only + pane-exists.
3. **Talking's tmux stays backend-driven** (the existing auto-snap), so focus-snap works
   even when no browser is open. Frontend does not double-fire `focusPane` on talking.
4. **Expanded view = a detail card** at the top of the Active fleet panel (works for both
   desktop table and mobile cards), reusing existing sub-components. Double-clicking the
   selected instance again collapses/deselects.

## Work checklist

- [x] `routes/tts.py`: `_set_ops_selected`, `_focus_zoom_and_mark`, `select_instance_pane`
      + `POST /api/instances/{id}/focus-pane`, wired `_snap_focus_to_speaker` onto the
      shared primitive.
- [x] `tmux-base.conf`: `@OPS_SELECTED` "◆ SEL" border badge + doc. `assertions.py`:
      added `@OPS_SELECTED` to `PANE_CLOSE_TRANSIENT_OPTIONS`.
- [x] `App.tsx` (`useInstanceSelection`) + `InstancesPanel.tsx` (double-click, highlight,
      `ExpandedInstance`) + `styles.css`. Rebuilt `web/ops` → `ui/ops`.
- [x] Tests: 10 new in `test_tts_focus_snap.py` (resolver gates + marker), 22 pass.
      `tsc --noEmit` clean, `vite build` clean.

## Validation results (2026-06-10)

Booting the full dev app on this machine was rejected: its lifespan starts workers
(`pane_state_worker`, `tmux_db_reconciler_worker`, timer `@TIMER_SEG` push) that write
the **shared live tmux**, which would fight the production `:7777` instance. Validated
without that blast radius:

- **UI E2E (real built bundle, headless Edge via Playwright)** against a static-mock
  server (synthetic fleet + toggleable talker): 15/15 checks pass — double-click expands
  with the "◆ selected" tag and posts focus-pane; re-double-click collapses; a new talker
  overrides the manual pin into the "🔊 talking" card with the live message; rows mark
  `is-selected`/`is-talking`; no JS runtime errors. Screenshots: `/tmp/ops-2-manual.png`,
  `/tmp/ops-3-talking.png`.
- **tmux marker**: the real `_set_ops_selected` run against an isolated tmux socket clears
  stale `@OPS_SELECTED` panes and marks exactly the target; `tmux-base.conf` renders the
  `◆ SEL` badge only when set (no parse error).
- **Backend**: 22 `test_tts_focus_snap.py` + 20 `test_tmuxctl_persona_assert.py` pass;
  endpoint registered as `POST /api/instances/{instance_id}/focus-pane`.

Operator follow-up (needs a real talking fleet + live tmux): on the live cockpit, double-
click a local instance and confirm its tmux pane focuses/zooms and shows `◆ SEL`; confirm
a speaking instance auto-expands and its pane gets the badge.
