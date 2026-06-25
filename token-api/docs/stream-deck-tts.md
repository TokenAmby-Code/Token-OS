# Stream Deck TTS controls

Physical Stream Deck buttons that drive the Token-API TTS **pause queue** — the
manual-play trigger the pause queue was always designed around. Normal TTS
accumulates in the pause queue (the deliberate "accumulate, operator plays"
buffer); these buttons drain it. Custodes/enforcement TTS bypasses the pause
queue and plays immediately, so it is unaffected by these controls.

## Wiring

Bind each button to a **web request** in the Stream Deck web-request plugin.
All four are `POST`. `localhost` works because the Stream Deck and Token-API are
co-resident on the Mac.

| Button | Method | URL | Body (JSON) |
|---|---|---|---|
| **Play next** | `POST` | `http://localhost:7777/api/tts/queue/promote` | `{}` |
| **Play all** | `POST` | `http://localhost:7777/api/tts/queue/play-all` | `{}` |
| **Skip current** | `POST` | `http://localhost:7777/api/tts/skip` | _(none)_ |
| **Skip + clear** | `POST` | `http://localhost:7777/api/tts/skip?clear_queue=true` | _(none)_ |
| **Mute toggle** | `POST` | `http://localhost:7777/api/tts/global-mode` | `{"mode":"toggle"}` |

Set `Content-Type: application/json` for the requests that carry a JSON body
(**Play next**, **Play all**, **Mute toggle**).

## What each control does

- **Play next** — promotes the next (oldest) pause-queue item to the front of
  the hot queue and plays it. Pulls tmux focus to that item's pane.
- **Play all** — drains the **entire** pause queue into the hot queue in FIFO
  order and plays it all. Does **not** pull per-item tmux focus (bulk drain).
  Returns `{"success": true, "promoted": <count>}`.
- **Skip current** — skips the TTS currently playing.
- **Skip + clear** — skips the current TTS **and** clears all pending items.
- **Mute toggle** — one button that flips the global TTS mode between
  `verbose` and `muted`. Returns `{"status":"ok","mode":"<new>","old_mode":"<prev>"}`.

## Notes

- "Play next" and "Play all" are no-ops when the pause queue is empty
  (`promoted: 0`).
- The mute toggle resolves `toggle` → `muted` when currently `verbose`, else
  `verbose`. To set an explicit mode instead, send `{"mode":"muted"}`,
  `{"mode":"verbose"}`, or `{"mode":"silent"}`.
- Queue state is observable at `GET http://localhost:7777/api/notify/queue/status`.
