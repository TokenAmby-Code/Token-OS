---
name: enforce
description: "Phone enforcement and notification system. Usage: /enforce (reference) or /enforce --build (active dev with full file context)."
user_invocable: true
---

# /enforce — Phone Enforcement & Notification System

## Mode

Parse the user's arguments:
- `/enforce` or `/enforce --help` — print the **Quick Reference** section below, then stop
- `/enforce --build` — load full dev context (read files listed in **Dev Context** section), then enter implementation mode
- `/enforce --status` — hit the phone heartbeat and report reachability + active enforcement state

---

## Quick Reference

### Unified Phone Endpoint Schema (v3)

Both `/notify` and `/enforce` on the phone (MacroDroid HTTP server, port 7777) accept the same parameter schema. All params are passed as query string key-value pairs and parsed into a `request-params` dictionary variable. The macro iterates over the dictionary, dispatching each key to the appropriate action.

```
GET http://100.102.92.24:7777/notify?<params>
GET http://100.102.92.24:7777/enforce?<params>
```

| Parameter | Type | Range | Description |
|-----------|------|-------|-------------|
| `vibe` | int | 0-100 | Pavlok vibration intensity (0 = skip) |
| `beep` | int | 0-100 | Pavlok beep intensity (0 = skip) |
| `tts_text` | string\|null | — | Text spoken aloud by MacroDroid TTS |
| `banner_text` | string\|null | — | Notification banner text (keep short) |
| `type` | enum | `descriptive`\|`prescriptive` | Determines notification style/urgency |

**Type semantics:**
- `descriptive` — informational, report-style (stop hooks, status reports, cron summaries). Lower urgency.
- `prescriptive` — action-required (AskUserQuestion hooks, enforcement, timers). Higher urgency, more aggressive vibration/sound.

### How macros process params

Both macros use the same dictionary-iteration pattern:
1. Parse query params into `request-params` dict variable
2. Vibrate (default pattern for notify, stronger for enforce)
3. **Iterate dictionary** — for each key/value pair:
   - **Pavlok intent**: `SendIntent` to `com.pavlok3.core` with `action={iterator_key}`, extra `intensity={iterator_value}` (INT type)
     - `/notify`: fires for keys matching regex `(vibe|beep)` where value != "0"
     - `/enforce`: fires for key matching regex `zap` where value != "0"
   - **Notification**: banner with `title=Claude is Calling`, `text={iterator_value}` — only when key == `banner_text` (**unverified — constraint not firing**)
   - **TTS**: speak `{iterator_value}` — only when key == `tts_text`
4. **Enforce only**: launch Spotify + media play (unconditional — always fires)
5. HTTP response: `OK`

Any param combo is valid. Missing params simply don't match. Empty calls return OK (vibrate fires, nothing else).

> **Constraint gotcha:** MacroDroid cannot cast `{iterator_value}` strings to numbers in action-level constraints. Use string comparisons (not-equals "0") instead of numeric (greater-than 0). Pavlok SendIntent intensity extra must be INT type (not auto-detect) with `{iterator_value}`.

### Additional endpoint: `/zap`

Direct Pavlok zap via `SendIntent` — no notification, TTS, or Spotify. Lighter weight.

```
GET http://100.102.92.24:7777/zap?zap=30
```

### /notify vs /enforce

| | `/notify` | `/enforce` |
|---|-----------|-----------|
| Vibrate | Default pattern | Stronger pattern |
| Pavlok vibe/beep | Yes (if `vibe`/`beep` > 0) | No (uses zap instead) |
| Pavlok zap | No | **Yes** (if `zap` > 0) |
| TTS | Per `tts_text` | Per `tts_text` |
| Banner | Per `banner_text` | Per `banner_text` |
| Spotify redirect | No | Yes (launches + plays) |
| Telemetry log | No | Yes (`enforce TRIGGERED level=...`) |

### Callers (Token-API server-side)

All callers migrated to v3 (2026-03-30). Fallback chain: phone v3 → server-side Pavlok API → Discord webhook.

| Hook / System | Endpoint | Type | Params | Status |
|---------------|----------|------|--------|--------|
| Stop hook (agent finished) | `/notify` | `descriptive` | `tts_text`, `banner_text=[tab] finished`, `vibe=30` | Deployed |
| `/api/notify` (webhook path) | `/notify` | — | `tts_text`, `banner_text`, `vibe=30` | Deployed |
| Enforcement cascade (levels 1-3) | `/notify` | `prescriptive` | vibe/beep/tts_text/banner_text per level | Deployed |
| Enforcement cascade (levels 4-5) | `/enforce` | `prescriptive` | zap/tts_text/banner_text + Spotify | Deployed |
| Twitter timeout | `/enforce` | `prescriptive` | `zap=30`, `tts_text`, `banner_text` | Deployed |
| Break exhaustion | `/enforce` | `prescriptive` | `zap=30`, `tts_text=Break time exhausted`, `banner_text` | Deployed |
| Instance count drop | `/notify` | `prescriptive` | `vibe=50-80`, `beep=50`, `tts_text`, `banner_text` | Deployed |
| PreToolUse AskUserQuestion | `/notify` | `prescriptive` | `tts_text=<question>`, `banner_text=Question`, `vibe=50`, `beep=30` | **Pending** |
| Phone heartbeat silence | server-side Pavlok | — | N/A (phone unreachable by definition) | Unchanged |

### Enforcement Cascade (server-controlled)

Token-API's `_enforcement_cascade_worker()` escalates levels on a timer until `app_close` telemetry or 5-min timeout. Each level maps to v3 endpoint + params via `ENFORCE_LEVEL_PARAMS`:

| Level | Delay | Endpoint | Params | Fallback |
|-------|-------|----------|--------|----------|
| 1 | 0s | `/notify` | `vibe=30`, `banner_text=Close {app}` | Pavlok API vibe |
| 2 | +15s | `/notify` | `vibe=50`, `beep=30`, `tts_text=Close {app}`, `banner_text` | Pavlok API vibe |
| 3 | +15s | `/notify` | `vibe=80`, `beep=50`, `tts_text=Final warning`, `banner_text` | Pavlok API vibe |
| 4 | +10s | `/enforce` | `banner_text=Enforcement active` | Discord |
| 5 | +30s (repeats) | `/enforce` | `zap=50`, `tts_text=Pavlok fired`, `banner_text` | Pavlok API zap |

Fallback chain: phone v3 endpoint → server-side `send_pavlok_stimulus()` → Discord webhook.

### Pavlok API (server-side fallback)

```python
send_pavlok_stimulus(
    stimulus_type="zap"|"beep"|"vibe",  # stimulus type
    value=1-100,                         # intensity
    reason="string",                     # audit trail
    respect_cooldown=True                # 45s default cooldown
)
```

Used as fallback when phone is unreachable. Also the primary path for desktop-originated stimuli (distraction blocked, heartbeat silence) where the user is at the desk, not the phone.

---

## Dev Context

When `/enforce --build` is invoked, read these files before starting implementation work:

### Core Implementation
1. **Token-API main.py** — the server-side enforcement logic:
   - `send_pavlok_stimulus()` — ~line 4058
   - `_send_enforce_to_phone()` — ~line 4805
   - `_send_discord_fallback()` — ~line 4839
   - `_enforcement_cascade_worker()` — ~line 4861
   - `start/stop_enforcement_cascade()` — ~line 4933/4946
   - Stop hook handler (mobile path) — ~line 9675
   - PreToolUse AskUserQuestion handler — ~line 9796
   - `/api/notify` endpoint — ~line 8742
   - `NotifyRequest` model — ~line 278
   - `WindowEnforceResponse` model — ~line 346

   ```bash
   # Read key sections
   cat -n /Volumes/Imperium/Scripts/token-api/main.py | sed -n '275,300p'   # models
   cat -n /Volumes/Imperium/Scripts/token-api/main.py | sed -n '4055,4130p' # pavlok
   cat -n /Volumes/Imperium/Scripts/token-api/main.py | sed -n '4805,4960p' # enforcement
   cat -n /Volumes/Imperium/Scripts/token-api/main.py | sed -n '8742,8820p' # notify
   cat -n /Volumes/Imperium/Scripts/token-api/main.py | sed -n '9610,9730p' # stop hook
   cat -n /Volumes/Imperium/Scripts/token-api/main.py | sed -n '9768,9855p' # pretooluse
   ```

2. **Phone-side macros:**
   - `/Volumes/Imperium/Scripts/mobile/macros/v2-enforce-cascade.yaml` — MacroDroid enforce spec
   - `/Volumes/Imperium/Scripts/mobile/macros/MACRODROID.md` — full macro inventory
   - MDR export: `macrodroid-read /Volumes/Imperium/Scripts/mobile/macros/EXPORT.mdr --macro "Enforce" --detail`
   - Fresh pull (if phone reachable): `macrodroid-read --refresh --detail`

3. **Phone config & connectivity:**
   - Phone IP: `100.102.92.24`, MacroDroid HTTP port: `7777`
   - SSH: `ssh-phone` (port 8022)
   - Discord fallback webhook: `DISCORD_FALLBACK_WEBHOOK` env var in main.py (~line 4833)

### Architecture Docs
4. **Vault notes:**
   - `/Volumes/Imperium/Imperium-ENV/Terra/Ultramar/Phone Enforcement Architecture.md`
   - `/Volumes/Imperium/Scripts/mobile/AGENTS.md` — mobile dev tools, macro spec format, CLI tools

### Mobile CLI Tools
5. **CLI tools** (all in `/Volumes/Imperium/Scripts/cli-tools/bin/`):
   - `macrodroid-read` — parse .mdr backups
   - `macrodroid-gen` — generate .macro from YAML
   - `macrodroid-push` — push .macro to phone
   - `macrodroid-pull` — pull files from phone
   - `macrodroid-state` — fetch live state
   - `notify` — desktop notification CLI

### Phone Directory
6. **`/Volumes/Imperium/Scripts/mobile/`** — templates, macro specs, tasker scripts
   - `macros/` — YAML specs, MDR exports, archive
   - `tasker-scripts/` — Termux:Tasker integration (pavlok.sh etc.)
   - `termux-*` — shell/tmux config templates

### After reading, you have full context to:
- Modify Token-API endpoint schemas (NotifyRequest, enforce params)
- Update MacroDroid macro specs (YAML → macrodroid-gen → push)
- Adjust hook handlers (Stop, PreToolUse) to use new schema
- Update the Phone Enforcement Architecture vault note
- Test reachability: `curl http://100.102.92.24:7777/heartbeat`
