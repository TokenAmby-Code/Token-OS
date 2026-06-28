# Stream Deck TTS controls

Physical Stream Deck buttons that drive the Token-API TTS **pause queue** — the
manual-play trigger the pause queue was always designed around. Normal TTS
accumulates in the pause queue (the deliberate "accumulate, operator plays"
buffer); these buttons drain it. Custodes/enforcement TTS bypasses the pause
queue and plays immediately, so it is unaffected by these controls.

## Wiring

Bind each button to a **web request** (set **Request Type → `POST`** and press
**Save All**). `localhost` works because the Stream Deck and Token-API are
co-resident on the Mac.

**All five buttons are bodiless** — every argument rides in the URL as a query
param. This is deliberate: web-request plugins (e.g. APIMonkey) reliably send
`POST` but their JSON request body often arrives without a JSON `Content-Type`,
which the server would reject. Query params sidestep that entirely, so **leave
the Request Body empty.**

| Button | Method | URL (no body) |
|---|---|---|
| **Play next** | `POST` | `http://localhost:7777/api/tts/queue/promote` |
| **Play all** | `POST` | `http://localhost:7777/api/tts/queue/play-all` |
| **Skip current** | `POST` | `http://localhost:7777/api/tts/skip` |
| **Skip + clear** | `POST` | `http://localhost:7777/api/tts/skip?clear_queue=true` |
| **Mute toggle** | `POST` | `http://localhost:7777/api/tts/global-mode?mode=toggle` |

Leave the plugin's **Browser Url** field empty — it opens a browser tab on press
and is not needed here.

### APIMonkey checklist (if buttons don't fire)

- **Request Type** dropdown set to `POST`, then **Save All** (the selection
  isn't persisted until you save — an unsaved button defaults to `GET`, which
  the server rejects with `405 Method Not Allowed`).
- **Request Body** empty (all controls are bodiless).
- **Browser Url** empty (otherwise it just opens a web page).

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
  `verbose`. To set an explicit mode instead, use `?mode=muted`,
  `?mode=verbose`, or `?mode=silent` (a JSON body `{"mode":"..."}` also works).
- To promote a specific instance's items instead of the oldest, use
  `?instance_id=<id>` on the Play next URL.
- Queue state is observable at `GET http://localhost:7777/api/notify/queue/status`.
