# YouTube Telemetry — Recursive-Retry / Belt-&-Suspenders Era (Tech Demo)

**Archived:** 2026-06-27 · **Replaced by:** passthrough/backfill assert (Wave 1, branch `yt-telemetry-passthrough-assert`)

This is a snapshot of the YouTube/Spotify/game telemetry macros as deployed on
the phone before the passthrough-assert rewrite. Kept as a working reference —
it is a genuinely clever (over-)engineered design that is worth studying before
we delete it.

## What this era did

The phone tried to be the source of truth for "is YouTube playing," and used
two self-correcting mechanisms to survive MacroDroid's unreliable triggers:

1. **Recursive retry** (`YT_POLL`) — a phone-local `HttpServerTrigger /yt_poll`
   loopback that re-validates the asserted state against Token-API
   (`/api/state/validate`), and on mismatch disables triggers, re-posts a
   corrected edge, and re-fires itself. This is the "briefly flash 500 then
   settle to 200" behaviour.
2. **10-min re-assert belt** (`YT_BG` `RegularIntervalTrigger 600s`) — periodic
   re-statement of the current playback state so a missed edge self-heals.

## The macros

| File | Role |
|------|------|
| `YT.macro` | App-launch edge (`ApplicationLaunchedTrigger`). Posts a playback edge on YouTube open. |
| `YT_BG.macro` | Background-audio playback edges (`MusicPlayingTrigger` ×2) + the 10-min re-assert belt. |
| `YT_POLL.macro` | The recursive-retry engine (floating buttons + `/yt_poll` loopback). |
| `Spotify.macro` | Cross-app state — clears YT when Spotify starts. |
| `Telemetry.macro` | Unified open/close for all 18 apps (36 `ApplicationLaunched/Closed` triggers). |

## Why it was replaced (known failure modes at archive time)

See `yt-telemetry-flow.html` (in this folder) for the full annotated map. In short:

- **Bug A — broken belt.** `YT_BG` re-asserts with `{lv=map[{trigger_that_fired}]}`.
  The dict `map` only has keys for the two `MusicPlayingTrigger` names, so on the
  *interval* tick the key is `"Interval: 00:10:00"` → no match → the literal
  unexpanded string is sent. Server rejects it (`409`/`422`). The self-heal never
  fires, and this is what makes state stick on/off and the floating buttons churn
  200↔409↔422 while nothing is playing.
- **Bug B — launch/playback race.** `YT` posts a launch-edge while `YT_BG` posts a
  playback edge; on app-open both fire the same second with opposite values
  (the "200 and 500 at once"). Home-feed autoplay was a major contributor and was
  disabled in-app on 2026-06-27.
- **Bug C — Spotify never enters music mode.** Server-side `spotify` is not a
  tracked app and the raw trigger string is ignored; the macro only clears YT.
- **Bug D — games have no belt.** `Telemetry` drives games off raw launch/close
  edges, so a spurious "launched" (e.g. surfacing a backgrounded game from
  recents) fires the enforcement cascade against an app that was never played.

## The replacement (one-line summary)

The recursion + belt exist to compensate for unreliable client-side state. Wave 1
makes **Token-API authoritative**: the phone sends one idempotent
`/api/state/validate?app=…&assert=<bool>&backfill=1` and the server writes its
state to match. That deletes the recursion macro and most of the failure surface.
The current value of this archive is as the "before" half of that comparison.
