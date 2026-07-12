# MacroDroid Macro Inventory

Current state of macros deployed to the phone. Last updated 2026-05-09 after official-schema cleanup.

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

5. Import via direct MacroDroid file-handler prompt:
   ```bash
   MACRODROID_AUTO_IMPORT=1 macrodroid-import macro-name.macro
   ```

6. Export/pull again and verify.

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

## TTS execution macros — artifact transport, 2026-07-09

Track B phone-side artifacts for [[/Volumes/Imperium/Imperium-ENV/Mars/Tasks/tts-execution-architecture-tokenos-authoritative-phone-wsl-linux.md]]. These are official `.macro` wrappers maintained in this directory, validated with `macrodroid-validate`, and delivered only via `MACRODROID_AUTO_IMPORT=1 macrodroid-import --replace`.

Phone-side contract:

- Token-OS owns TTS session, queue, `playback_id`, control state, and synthesis: it renders OpenAI TTS to cached WAV artifacts at enqueue and hands consumers an `artifact_url`. The phone performs no local text-to-speech.
- Phone is execution-only: fetch the WAV artifact, play it to completion, post `buffer_drained`.
- Overlay controls do not mutate local playback directly. They hit local `/tts-control`, which forwards to Token-OS `/api/tts/control`; only a later `/tts-local-control` echo is local execution authority.
- Phone has no Mac fallback. Failures go up to Token-OS via `/tts-error` → `/api/tts/backend-error`.

Macros:

| File | Macro | Endpoint / Trigger | Purpose |
|---|---|---|---|
| `tts-phone-control-ingress.macro` | TTS Phone Control Ingress | `/tts-control` | Public phone control ingress; forwards overlay commands to Token-OS first. |
| `tts-phone-local-control.macro` | TTS Phone Local Control | `/tts-local-control` | Private Token-OS echo consumer; local-control hook point. |
| `tts-phone-chunk-player.macro` | 04 TTS Artifact Player | `/tts-artifact` | One-utterance artifact executor: shell-fetches the server-rendered OpenAI WAV artifact (`curl`/`wget`) to `/storage/emulated/0/MacroDroid/tts/token-tts-current.wav`, plays it with `PlaySoundAction` `waitToFinish`, then posts Token-OS `/api/tts/chunk-event` `buffer_drained`. No local text-to-speech, no `/api/tts/chunk-next` streaming/backfill. |
| `tts-phone-error-report.macro` | TTS Phone Error Report | `/tts-error` | Reports phone executor failure to Token-OS. |
| `tts-overlay-pause.macro` | TTS Overlay Pause | floating `tts-pause` | Calls local `/tts-control?command=pause`. |
| `tts-overlay-resume.macro` | TTS Overlay Resume | floating `tts-resume` | Calls local `/tts-control?command=resume`. |
| `tts-overlay-skip.macro` | TTS Overlay Skip | floating `tts-skip` | Calls local `/tts-control?command=skip`. |
| `tts-overlay-faster.macro` | TTS Overlay Faster | floating `tts-faster` | Retired/disabled for one-chunk v1; Token-OS rejects `faster`/`speed`. |
| `tts-overlay-stop.macro` | TTS Overlay Stop | floating `tts-stop` | Calls local `/tts-control?command=stop`. |

On-device gotchas (verified 2026-07-09, artifact player debugging):

- MacroDroid's native save-HTTP-response-to-file (`saveResponseType: 2` with all-files access) writes **nothing** on this device — with fixed or magic-text filenames, into `/Notifications` or into MacroDroid's own folder — while still completing the GET. Downloads must go through a `ShellScriptAction` (`curl`/`wget`), which can write under `/storage/emulated/0/MacroDroid/`.
- `{http_param=...}` magic text does **not** expand inside `ShellScriptAction` scripts; use `{lv=request[...]}` (the trigger's query-params dictionary).
- The artifact filename is fixed (`token-tts-current.wav`) and removed at macro entry: the queue is serialized so there is no concurrency, and a stale file can never mask a failed fetch or replay old audio.
- Every hop logs a timestamped checkpoint to `/storage/emulated/0/MacroDroid/logs/debug.log` (`accepted` → `downloaded` → `playback finished`); silent no-op playback was invisible without these.

Validation:

```bash
set -e
for f in mobile/macros/tts-*.macro; do macrodroid-validate "$f"; done
```
