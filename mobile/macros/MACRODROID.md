# MacroDroid Macro Inventory

Current state of macros deployed to the phone. Last updated 2026-03-30 (v3.1 — banner fix, debug logging, culled legacy).

**Archive:** Pre-v2 backup at `archive/pre-v2-shizuku-era-2026-03-10.mdr`

## Summary

- **Total Macros:** 18
- **Enabled:** 15
- **Disabled:** 3
- **Endpoint:** All phone HTTP endpoints run on MacroDroid HTTP server, port 7777
- **Logging:** Telemetry via Token-Ping; debug logging via shell scripts to `/storage/emulated/0/MacroDroid/logs/debug.log`

## Global Variables

| Variable | Type | Purpose |
|----------|------|---------|
| `yt_bg` | bool | YouTube background playback active (PiP/audio-only) |

## Telemetry (1 unified macro)

Single macro with 36 triggers (open + close for 18 apps). Routes through Token-Ping for server relay + Discord fallback.

| Macro | Triggers | Action |
|-------|----------|--------|
| Telemetry | X, YouTube, games, Snapchat, Tinder, Instagram, Discord, Gmail, Obsidian, Keep Notes, Vivaldi, MacroDroid, Rocket Money, Frontiers, Missiles, Chat | POST to `localhost:7777/token-ping` |

**Apps monitored (18):** X, 20 Minutes Till Dawn, Thronefall, Chat, Discord, Frontiers, Gmail, Instagram, Keep Notes, MacroDroid, Missiles, Obsidian, OneBit Adventure, Rocket Money, Slice & Dice, Snapchat, Tinder, Vivaldi

Each app has two triggers: `launched=True` (open) and `launched=False` (close). The raw MacroDroid trigger name is sent as payload — server-side `parse_macrodroid_trigger()` extracts the app name and action.

## YouTube Special (3 macros)

YouTube requires special handling for background audio (PiP / audio-only).

| Macro | Status | Triggers | Purpose |
|-------|--------|----------|---------|
| YT | Enabled | App Launched (YouTube open/close) | Open: clear `yt_bg`, disable YT_BTN, report. Close: check if music playing → set `yt_bg`, disable YT_BTN, or report close |
| YT_BG | Enabled | Music Playing (start/stop) | When `yt_bg==true`: report music state changes via Token-Ping |
| YT_BTN | Disabled | Floating Button | Manual: clear `yt_bg`, report close, wait for music stop, then disable self |

## Spotify (1 macro)

| Macro | Triggers | Purpose |
|-------|----------|---------|
| Spotify | Spotify playback start/stop | If `yt_bg==true` on Spotify start: clear `yt_bg`, report YT close. Always reports Spotify events. |

## Token-Ping (1 macro)

Local HTTP relay for macro parameterization. Enables server-first + Discord-fallback pattern.

| Macro | Endpoint | Purpose |
|-------|----------|---------|
| Token-Ping | `/token-ping` | Receives `forward-request` dict with `endpoint`, `method`, `body`; forwards to Token-API; falls back to Discord webhook on failure |

**Pattern:**
```
Caller macro → POST localhost:7777/token-ping
  body = {"endpoint": "/phone/event", "method": "POST", "body": "{\"app\": \"...\"}"}

Token-Ping → forwards to Token-API
  → on non-200: posts to Discord #fallback webhook
```

## Notification & Enforcement (3 macros)

All use the **v3 unified param schema**. Params passed as query string, parsed into `request-params` dictionary, iterated with per-key dispatch.

| Macro | Endpoint | Purpose |
|-------|----------|---------|
| Notify | `/notify?<params>` | Notification + TTS + Pavlok vibe/beep |
| Enforce | `/enforce?<params>` | Notification + TTS + Pavlok zap + Spotify redirect |
| Zappa | `/zap?<params>` | Direct Pavlok zap via intent |

### v3 Param Schema

```
GET /notify?vibe=30&beep=0&tts_text=Hello&banner_text=Short+msg&type=descriptive
GET /enforce?zap=50&tts_text=Close+the+app&banner_text=Enforcement&type=prescriptive
```

| Parameter | Type | Range | Description |
|-----------|------|-------|-------------|
| `vibe` | int | 0-100 | Pavlok vibration intensity (0 = skip) |
| `beep` | int | 0-100 | Pavlok beep intensity (0 = skip) |
| `tts_text` | string | — | Text spoken aloud by MacroDroid TTS |
| `banner_text` | string | — | Notification banner text |
| `type` | enum | descriptive/prescriptive | Notification urgency hint |

### Macro Internals

Both Notify and Enforce iterate over `request-params` dictionary. All dispatch uses **action-level constraints** (not if-blocks).

**Debug logging:** Both macros have shell script actions that log to `debug.log` at entry, dict parse, each iteration, and constraint passes. These are operational — keep until v3 is fully stable, then strip.

| Action | Constraint | Notify | Enforce | Status |
|--------|-----------|--------|---------|--------|
| Debug shell logs | None / per-constraint | 4 scripts | 5 scripts | Active — remove when stable |
| Vibrate | None (always) | Default pattern | Stronger pattern | Verified |
| Pavlok SendIntent | key matches `(vibe\|beep)` AND value != "0" | Yes | — | Verified |
| Pavlok SendIntent | key matches `zap` AND value != "0" | — | Yes | Verified |
| Notification | key == `banner_text` | Yes | Yes | Verified — root cause was action button config, not constraint |
| Speak TTS | key == `tts_text` | Yes | Yes | Verified |
| Launch Spotify + Play | None | — | Yes (always) | Verified |

**Any param combo is valid.** Missing params are simply not iterated. Empty calls (`/notify` or `/enforce` with no params) still return OK — vibrate fires but nothing else matches.

Notification taps open Termux via the `launch termux` helper macro.

> **Constraint gotcha (2026-03-30):** Numeric comparison constraints (`value > 0`) fail on `{iterator_value}` — MacroDroid can't cast the string to a number in constraint context. Use **string not-equals "0"** instead. Pavlok SendIntent intensity extra must be **INT type** (not auto-detect).

> **Banner gotcha (2026-03-30):** NotificationAction works fine with action-level constraints. The original failure was caused by an invalid action button configuration, not constraint evaluation. If-block workaround is unnecessary.

### Zappa (Direct Zap)

Standalone Pavlok zap endpoint. Sends intent directly to `com.pavlok3.core` without the full notification/TTS/Spotify machinery.

```
GET /zap?<request params>
```

## Geofence (1 macro)

Unified macro handles all location entry/exit events via Token-Ping.

| Macro | Status | Triggers | Purpose |
|-------|--------|----------|---------|
| Geofence | Enabled | 6 triggers (Home/Gym/Campus enter/exit) | Unified geofence → Token-Ping |

**Geofence IDs:**
- Home: `1e4e4f0d-ccd8-40a2-b84a-6027a2843cb8`
- Gym: `7fa61f1d-8e1e-409a-810a-ce7f4a660f2b`
- Campus: `a747bc5f-1cb8-4a10-b817-7d6db5578357`

## System (3 macros)

| Macro | Trigger | Purpose |
|-------|---------|---------|
| sshd | HTTP `/sshd` | Start Termux sshd, return status JSON |
| Phone Health | Every 15 min | GET Token-API `/health`; notify on failure |
| Heartbeat | HTTP `/heartbeat` | Return `{"status": "alive", "time": "..."}` |

## Endpoints (1 macro)

| Macro | Endpoint | Purpose |
|-------|----------|---------|
| List Exports API | `/list-exports` | Trigger macro export, return success |

## Utility (2 macros)

| Macro | Status | Purpose |
|-------|--------|---------|
| launch termux | Enabled | Helper for notification tap → opens Termux |
| Potential bluetooth device priority | Enabled | BT connect priority (blocked by permissions) |

## Disabled (3 macros)

| Macro | Purpose |
|-------|---------|
| YT_BTN | Floating button for manual YT close (disabled, enabled dynamically by YT macro) |
| BT Disconnect XM5 | HTTP `/bt-disconnect` — disconnect WF-1000XM5 |
| Button | Test macro |

## Debug Log

**File:** `/storage/emulated/0/MacroDroid/logs/debug.log`

```
14:15:23 [DEBUG] HttpServerTrigger qs=vibe=30&banner_text=test
14:15:23 [DEBUG] dict={vibe:30,banner_text:test}
14:15:23 [DEBUG] iter key=vibe val=30
14:15:23 [CONSTRAINT] pavlok PASS KEY key=vibe val=30
14:15:23 [DEBUG] iter key=banner_text val=test
14:15:23 [CONSTRAINT] banner PASS key=banner_text val=test
```

**Access:**
```bash
ssh phone "tail -50 /storage/emulated/0/MacroDroid/logs/debug.log"
```

## Changelog

### v3.1 (2026-03-30) — Banner Fix + Debug Logging + Culling
- **Banner notification**: Root cause was action button config, not constraints. Fixed action button to launch Termux via helper macro
- **Pavlok SendIntent**: Fixed intensity extra (was hardcoded `{lv=request[vibe]}`, now `{iterator_value}` with INT type)
- **Constraints**: Numeric `> 0` constraints replaced with string `!= "0"` (iterator values are strings)
- **Debug logging**: Shell script probes added to Notify (4) and Enforce (5) for constraint/dispatch tracing
- **Token-API**: All callers migrated to v3 params (`_send_to_phone()` helper, `ENFORCE_LEVEL_PARAMS` mapping)
- **Token-API integrations**: Morning enforce, Golden Throne followup, PreToolUse AskUserQuestion — all now send phone notifications via v3
- **Fallback chain**: phone v3 → server-side Pavlok API → Discord webhook
- **Culled**: Enforce-alt, Notify-old, Enforce-old, Constraint Probes, Debug Logging Blocks, all legacy geofences (6), Change song — 30 → 18 macros

### v3 (2026-03-29) — Unified Params + Token-Ping
- **Notify/Enforce**: Replaced old query params with unified dictionary-iteration schema (vibe, beep, tts_text, banner_text, type)
- **Telemetry**: Consolidated from 6+ macros to 1 unified macro with 36 triggers
- **Token-Ping**: New local HTTP relay pattern for macro-to-server communication
- **Geofence**: Consolidated from 6 individual macros to 1 unified macro
- **Zappa**: New direct Pavlok zap endpoint
- **Total**: 44 → 25 macros

### v2 (2026-03-11) — Shizuku-Free
- Archived Shizuku-based enforcement
- Introduced cascade levels 1-5
- Added Discord fallback transport

### v1 (pre 2026-03-10) — Shizuku Era
- Shizuku ADB-based app disable/enable
- Individual app macros
