# MacroDroid Macro Inventory

Current source inventory for MacroDroid phone automation. Last updated for the
numbered Token-OS TTS phone executor set.

## Source of Truth

- Official schema: `../macrodroid-llm-schema.yaml`
- Current full exports: `../EXPORT.mdr` and `EXPORT.mdr`
- Active workflow: official `.macro` wrapper JSON only
- Retired workflow: custom YAML DSL and staged generated `.macro` files

Legacy files were moved to archives:

- `archive/legacy-yaml-dsl-2026-05-09/`
- `archive/staged-macro-files-2026-05-09/`

Do not add new YAML specs to this directory.

## Inspecting Current State

```bash
macrodroid-state                    # Pull latest phone export and show summary
macrodroid-state --detail           # Show trigger/action/constraint classes
macrodroid-read EXPORT.mdr --list   # List macros in this export
macrodroid-read EXPORT.mdr --macro "Heartbeat"
macrodroid-read EXPORT.mdr --macro "Heartbeat" --export-macro > heartbeat.macro
macrodroid-validate heartbeat.macro
```

## Summary from `../EXPORT.mdr`

As of the local 2026-03-31 export in `../EXPORT.mdr`:

- Total macros: 25
- HTTP server port: 7777
- Key global variable: `yt_bg` tracks YouTube background playback

Enabled/disabled status changes on phone over time. Pull a fresh export before editing.

## Core Systems

### Telemetry

Single unified telemetry macro with app open/close triggers. It reports app events through Token-Ping to the desktop Token API with Discord fallback.

### YouTube Special Handling

YouTube uses multiple macros because background playback/PiP must be distinguished from real close events:

- `YT` — app open/close handling and `yt_bg` state transitions
- `YT_BG` — music-playing state changes while YouTube is in background mode
- `YT_BTN` — manual/floating-button closure path

Preferred Token-API event shape for granular YouTube playback edges:

```json
{"app":"Youtube","play":"true"}
{"app":"Youtube","play":"false"}
```

`play=true` is treated as a YouTube open/active edge; `play=false` is treated
as a close/inactive edge. The server also accepts JSON booleans (`true`/`false`)
and keeps legacy `Application Launched/Closed (Youtube)` telemetry compatible.

### Spotify

Spotify clears YouTube background state when Spotify playback starts and reports Spotify events.

### Token-Ping

Local HTTP relay. Caller macros POST structured requests to the phone's local MacroDroid endpoint; Token-Ping forwards to Token API and uses Discord fallback on failure.

### Notification / Enforcement

Server-driven endpoints on the phone use the MacroDroid HTTP server:

```text
/notify?<params>   notification + TTS + Pavlok vibe/beep
/enforce?<params>  notification + TTS + Pavlok zap + Spotify redirect
/zap?<params>      direct single Pavlok stimulus (zap/beep/vibe)
/pause             pause active YouTube/media playback
/heartbeat         reachability
/list-exports      export/list support
/sshd              starts Termux sshd
```

`/zap` accepts both the legacy `?zap=30` form and the generic
`?action=zap|beep|vibe&intensity=1-100` form. The Zappa macro must contain
exactly one `SendIntentAction` so Pavlok stimuli cannot be bundled in one phone
request.

## Official Edit Workflow

1. Pull/export current state:
   ```bash
   macrodroid-state --pull
   ```

2. Extract the macro:
   ```bash
   macrodroid-read /tmp/macrodroid-state/EXPORT.mdr --macro "Macro Name" --export-macro > macro-name.macro
   ```

3. Edit JSON directly using `../macrodroid-llm-schema.yaml`.

4. Validate:
   ```bash
   macrodroid-validate macro-name.macro
   ```

5. Import:
   ```bash
   MACRODROID_AUTO_IMPORT=1 macrodroid-import macro-name.macro
   ```

6. Export/pull again and verify. The import launcher can time out or report a
   false success when MacroDroid's UI rejects a shape, so the deployed export is
   the final truth.

## Debug Logs

MacroDroid debug log path:

```text
/storage/emulated/0/MacroDroid/logs/debug.log
```

Watch live:

```bash
ssh-phone "tail -f /storage/emulated/0/MacroDroid/logs/debug.log"
```

Use shell actions that append timestamped checkpoints before/after HTTP requests, variable parsing, dictionary iteration, Pavlok calls, and branch points.

## Canonical TTS execution macros

These official `.macro` wrappers are the canonical phone-side Token-OS TTS set. They supersede the earlier `tts-phone-*`, `tts-overlay-*`, and `90`-`94` retirement-stub files. Macro names and filenames are numbered so the distinctive part is visible in the Android import picker.

Phone-side contract:

- Token-OS owns TTS session, queue, current chunk, `playback_id`, and control state.
- Phone is execution-only and holds `current + next/backfill` only; Token-OS remains authoritative for order and controls.
- Notification controls do not mutate local playback directly. Button actions call local `/tts-control`, which forwards to Token-OS `/api/tts/control`; only a later `/tts-local-control` echo is local execution authority.
- Phone has no Mac fallback. Failures go up to Token-OS via `/tts-error` → `/api/tts/backend-error`.
- `IterateDictionaryAction` is intentionally absent from the active TTS macro set after import-control-flow failures. Chunk ingress uses the `request` dictionary with direct local dereferences such as `{lv=request[current_chunk]}`; backfill uses `JsonParseAction` plus direct parsed-field assignments such as `{lv=backfill[next_chunk]}`.
- `04 TTS Chunk Player` speaks only scalar local variables (`{lv=current_chunk_text}`, `{lv=next_chunk_text}`, `{lv=backfill_next_chunk}`), never literal HTTP params or `tts_*` globals.

Canonical macros:

| File | Macro | Endpoint / Trigger | Purpose |
|---|---|---|---|
| `01-controls-notification.macro` | 01 TTS Controls Notification | `/tts-control-surface` | Posts the persistent Token-OS TTS control notification. Buttons use MacroDroid 5.65 direct `actionClassType`/`actionJson` notification action fields to run local HTTP requests. |
| `02-control-ingress.macro` | 02 TTS Control Ingress | `/tts-control` | Public phone control ingress; forwards notification commands to Token-OS first. |
| `03-local-echo-control.macro` | 03 TTS Local Echo Control | `/tts-local-control` | Private Token-OS echo consumer; updates local control state and cancels active chunk/backfill macros for skip/stop. |
| `04-chunk-player.macro` | 04 TTS Chunk Player | `/tts-chunk` | Direct-deref full executor: accepts current+next, speaks scalar local chunks, calls `/api/tts/chunk-next` in-loop, parses backfill by direct dictionary deref, and reports progress plus `buffer_drained`. |
| `05-backfill-fetcher.macro` | 05 TTS Backfill Fetcher | manual helper | Numbered diagnostic/manual helper retained in the canonical set; `04` now performs normal backfill itself. |
| `06-error-report.macro` | 06 TTS Error Report | `/tts-error` | Reports phone executor failure to Token-OS. |

Retired sources:

- `tts-phone-*` files are removed from active source.
- `tts-overlay-*` files are removed from active source.
- `90`-`94` overlay retirement stubs are not part of the active set and should
  not be restored for normal TTS testing.

### `04 TTS Chunk Player` execution flow

`04` is the live playback worker. It should be the only macro that performs
normal chunk backfill during playback.

1. `/tts-chunk` receives query params into local dictionary `request`.
2. It immediately responds accepted and posts/refreshes `01 TTS Controls Notification`.
3. It scalarizes inbound fields from `request`:
   - `{lv=request[current_chunk]}` -> `{lv=current_chunk_text}`
   - `{lv=request[next_chunk]}` -> `{lv=next_chunk_text}`
   - `{lv=request[current_index]}` -> `{lv=current_index}`
   - `{lv=request[next_index]}` -> `{lv=next_index}`
   - `{lv=request[session_id]}`, `{lv=request[playback_id]}`,
     `{lv=request[utterance_id]}` -> scalar local metadata.
4. It speaks `{lv=current_chunk_text}` with `m_waitToFinish: true`.
5. If `{lv=next_chunk_text}` is present, it queues it, emits
   `current_complete_next_starting`, and enters the backfill loop.
6. Each loop iteration calls `POST /api/tts/chunk-next`, stores the raw response
   in `{lv=backfill_raw}`, parses it into dictionary `backfill`, and dereferences:
   - `{lv=backfill[next_chunk]}` -> `{lv=backfill_next_chunk}`
   - `{lv=backfill[next_index]}` -> `{lv=backfill_next_index}`
   - `{lv=backfill[done]}` -> `{lv=backfill_done}`
   - `{lv=backfill[control_state]}` -> `{lv=control_state}`
7. If `control_state` is `stop`, it emits `buffer_drained` with
   `detail=control_stop` and breaks.
8. If a backfill chunk exists, it speaks `{lv=backfill_next_chunk}`, advances
   indexes, and loops.
9. If no backfill chunk exists, it emits final `buffer_drained` and exits.

Speak-field invariant:

```text
SpeakTextAction.m_textToSay must be exactly one of:
  {lv=current_chunk_text}
  {lv=next_chunk_text}
  {lv=backfill_next_chunk}
```

Do not put `{http_param=...}`, `{lv=request[...]}`, `{lv=backfill[...]}`, or
`{v=tts_*}` directly in `SpeakTextAction.m_textToSay`. MacroDroid has accepted
some of those shapes in import/validation while the live TTS engine speaks them
literally.

Validation:

```bash
set -e
for f in mobile/macros/[0-9][0-9]-*.macro; do macrodroid-validate "$f"; done
python -m pytest -q mobile/tests/test_tts_phone_macros.py
```

Import/export gate:

`macrodroid-validate` is not sufficient. Import only `mobile/macros/01`–`06`; do not import retired `tts-phone-*`, `tts-overlay-*`, or `90`–`94` files. After import, export/pull with `macrodroid-state --pull` and verify the deployed `.mdr` contains the numbered macro names, direct notification button fields (`actionClassType`, `actionName`, `actionJson`), request-dictionary derefs in `04`, and no literal HTTP/global variable text in TTS speak fields before treating the phone executor as live.

Import order:

```bash
cd /Users/tokenclaw/worktrees/Token-OS/wt-fix-phone-tts-chunk-playback
for f in mobile/macros/0[1-6]-*.macro; do
  MACRODROID_AUTO_IMPORT=1 macrodroid-import "$f"
done
```

Known import-tool caveats:

- `macrodroid-import --replace` can false-success or fail verification after
  MacroDroid UI rejection. Watch the phone UI.
- `macrodroid-validate` does not catch every MacroDroid UI/TTS-engine rejection.
- `01 TTS Controls Notification` can take long enough that verification may
  time out; confirm on the phone and then pull/export.

Post-import verification:

```bash
macrodroid-state --pull
macrodroid-read /tmp/macrodroid-state/EXPORT.mdr --list | grep -E '^(01|02|03|04|05|06) TTS'
macrodroid-read /tmp/macrodroid-state/EXPORT.mdr --macro "04 TTS Chunk Player" --export-macro > /tmp/live-04.macro
macrodroid-validate /tmp/live-04.macro
```

Live smoke:

1. Send a 3+ chunk utterance through the normal Token-OS phone TTS route.
2. Tail phone logs:
   ```bash
   ssh-phone "tail -f /storage/emulated/0/MacroDroid/logs/debug.log"
   ```
3. Pass criteria:
   - phone speaks all chunks as natural text;
   - no literal `{lv=...}`, `{http_param=...}`, or `{v=tts_*}` text is spoken;
   - Token-OS receives `current_complete_next_starting`;
   - Token-OS receives final `buffer_drained`;
   - pause/stop/skip commands route through `/tts-control` -> Token-OS ->
     `/tts-local-control`.
