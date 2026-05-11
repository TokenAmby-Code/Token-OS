# Mobile Development Tools

Tools and configuration for phone automation via Termux and MacroDroid.

## Design Rules

**No hacky polls.** Never write ad-hoc polling loops (sleep+check in bash, retry loops).
Use event-driven patterns: MacroDroid notification triggers, HTTP webhooks, LaunchAgent intervals.
If polling is truly unavoidable, staple it to the existing single poll macro in MacroDroid.

## Shizuku (ARCHIVED 2026-03-10)

Shizuku is no longer used. v2+ enforcement uses stock Android + MacroDroid (no root/ADB). Archive at `macros/archive/pre-v2-shizuku-era-2026-03-10.mdr`. CLI tools (`shizuku-connect`) still exist but are inactive.

## Overview

This directory contains:
- Termux shell configuration templates
- MacroDroid official `.macro` JSON workflow and schema documentation
- Documentation for mobile automation tooling

The phone (Samsung S24) connects via Tailscale and runs Termux for SSH access.

## Focus Management System (v3)

Phone-server system for app usage management. Phone reports telemetry via Token-Ping (local relay with Discord fallback). Server decides enforcement actions and pushes to phone.

### Architecture (v3, 2026-03-29)

```
Phone (MacroDroid)                    Desktop Server (Token-API)
──────────────────                    ────────────────────────────

[Telemetry — 1 unified macro, 36 triggers]
  All apps open/close  ──>  Token-Ping  ──>  POST /phone/event
  YouTube special (YT/YT_BG/YT_BTN)         (parses trigger name)
  Spotify (clears yt_bg)                          │
  Geofence (Home/Gym/Campus)                      ▼
                                           Server analyzes context
  Token-Ping on failure ──>  Discord       (time, location, usage)
                             webhook              │
                             fallback             ▼
                                           Enforcement cascade
[Notification/Enforcement — v3 unified params]    │
  /notify?params  <───────────────────────────────┘
  /enforce?params (+ Pavlok zap + Spotify redirect)
  /zap?params     (direct Pavlok, lightweight)
```

### Phone Endpoints (MacroDroid HTTP Server, port 7777)

| Endpoint | Purpose |
|----------|---------|
| `/notify?vibe=N&beep=N&tts_text=X&banner_text=X&type=T` | Notification + TTS + Pavlok vibe/beep |
| `/enforce?zap=N&tts_text=X&banner_text=X&type=T` | Same + Pavlok zap + Spotify redirect |
| `/zap?zap=N` | Direct Pavlok zap (lightweight) |
| `/token-ping` | Local relay → Token-API with Discord fallback |
| `/heartbeat` | Health check |
| `/sshd` | Start Termux SSH daemon |
| `/list-exports` | Trigger macro export |

### Macro Categories (25 total, 18 enabled)

| Category | Count | Purpose |
|----------|-------|---------|
| Telemetry | 1 | Unified app open/close (36 triggers, 18 apps) |
| YouTube | 3 | YT + YT_BG + YT_BTN (background audio tracking) |
| Spotify | 1 | Cross-app state (clears yt_bg) |
| Token-Ping | 1 | Local HTTP relay with Discord fallback |
| Notify/Enforce | 3 | Notify, Enforce, Zappa (v3 unified params) |
| Geofence | 1+6 | Unified + 6 legacy disabled |
| System | 3 | sshd, Phone Health, Heartbeat |
| Other | 6 | BT, gestures, list-exports, legacy |

See `macros/MACRODROID.md` for full macro inventory and v3 param schema.

## Connection

All devices use `ssh-connect` (standardized SSH with redirect-on-exit).
Commands: `ssh-mac`, `ssh-wsl`, `ssh-phone` (each wraps `ssh-connect <target>`).

```bash
ssh-phone               # Interactive SSH to phone
ssh-phone "command"     # Run command on phone and exit
ssh-phone --proxy       # Nest SSH instead of redirecting (when already in SSH)
```

### Redirect-on-Exit Flow

When SSH'd into host A and you switch to host B, instead of nesting
(origin→A→B), the session redirects so you connect directly (origin→B).

```
  Phone                    Mac                      WSL
    │                       │                        │
    │──── ssh-mac ─────────>│                        │
    │     (wrapper loop)    │ (interactive session)  │
    │                       │                        │
    │                  user runs ssh-wsl             │
    │                       │                        │
    │                       │── reverse SSH ─────────│
    │<── "echo wsl > ──────"│   (not to WSL, back   │
    │     ~/.ssh-next"      │    to phone/origin)    │
    │                       │                        │
    │   (file written)      │── kill -HUP $PPID     │
    │                       ╳  (session closes)      │
    │                                                │
    │   wrapper loop wakes                           │
    │   reads ~/.ssh-next = "wsl"                    │
    │   rm ~/.ssh-next                               │
    │                                                │
    │──── ssh wsl (direct) ─────────────────────────>│
    │     (no nesting!)     │                        │
```

**Key mechanisms:**
- `$SSH_CLIENT` IP → `ip_to_host()` → identifies origin device
- Reverse SSH writes `~/.ssh-next` on origin (not `/tmp`, Termux can't write there)
- `kill -HUP $PPID` auto-closes the SSH session
- Origin's wrapper loop picks up the redirect file and connects directly
- `--proxy` skips all of this and nests normally

## MacroDroid Automation

MacroDroid is an Android automation app. We generate and import **official MacroDroid `.macro` JSON wrapper files**.

### Canonical Source

`macrodroid-llm-schema.yaml` is the source of truth. The old custom YAML DSL is retired.

Hard rules:

- Do **not** create new `macros/*.yaml` specs.
- Do **not** add trigger/action/constraint builders to `macrodroid-gen`.
- Do **not** translate through the old lossy compiler.
- Generate strict JSON in MacroDroid's official wrapper format.
- Validate with `macrodroid-validate` before pushing.
- After phone import, pull/export `.mdr`; deployed MacroDroid JSON is canonical truth.

### Official `.macro` Wrapper Format

A `.macro` file is strict JSON:

```json
{
  "macroExportVersion": 1,
  "macro": {
    "m_name": "Example",
    "m_enabled": true,
    "m_completed": true,
    "m_GUID": 0,
    "m_category": "Automation",
    "aiGenerated": 1,
    "m_description": "What this macro does",
    "m_triggerList": [
      {"m_classType": "EmptyTrigger", "m_SIGUID": 0}
    ],
    "m_actionList": [],
    "m_constraintList": []
  },
  "globalVariables": [],
  "userIcons": null,
  "aiFeedback": "Generated from official MacroDroid schema."
}
```

Structural rules from the official schema:

- `m_triggerList`, `m_actionList`, and `m_constraintList` are separate.
- Triggers go only in `m_triggerList`.
- Actions go only in `m_actionList`.
- Constraints go only in `m_constraintList` or an item's `m_constraintList`.
- Never put a `*Constraint` object directly in `m_actionList`.
- Actions are a flat list. `IfConditionAction`, `ElseAction`, `ElseIfConditionAction`, and `EndIfAction` are sibling markers, not nested containers.
- Every selectable item needs `m_classType` and `m_SIGUID`; placeholder `0` is acceptable for import.
- New importable macros use `m_GUID: 0`, `m_completed: true`, and `aiGenerated: 1`.
- Manual-only macros use `EmptyTrigger`; MacroDroid macros must have at least one trigger.

### CLI Tools

| Command | Description |
|---------|-------------|
| `macrodroid-gen` | Official JSON skeleton/normalizer only. No YAML support. |
| `macrodroid-validate` | Strict official `.macro` wrapper validator. |
| `macrodroid-push` | Validates and pushes `.macro` files to the phone via SSH. |
| `macrodroid-pull` | Pull files from phone via SSH. |
| `macrodroid-read` | Inspect `.mdr` exports and extract official `.macro` wrappers. |
| `macrodroid-state` | Trigger/pull current phone export and display it. |

### Quick Start: Create a Macro

Generate a manual skeleton:

```bash
macrodroid-gen --empty "My Manual Macro" --category Automation --pretty > my-manual.macro
macrodroid-validate my-manual.macro
macrodroid-push my-manual.macro
```

Generate a simple HTTP endpoint:

```bash
macrodroid-gen \
  --http-endpoint heartbeat \
  --name Heartbeat \
  --category Telemetry \
  --response-text '{"status":"alive"}' \
  --pretty > heartbeat.macro

macrodroid-validate heartbeat.macro
macrodroid-push heartbeat.macro
```

For real macros, author the official JSON directly using `macrodroid-llm-schema.yaml` for class fields and enum values.

### Quick Start: Extract a Deployed Macro

Extract from the current `.mdr` export into an importable official wrapper:

```bash
macrodroid-read EXPORT.mdr --macro Heartbeat --export-macro > heartbeat.macro
macrodroid-validate heartbeat.macro
```

The extractor resets the macro GUID to `0` and adds official import fields where missing.

### Current State / Exports

```bash
macrodroid-state                    # Pull latest export and show summary
macrodroid-state --detail           # Show class-level detail
macrodroid-state --json             # Raw .mdr JSON
macrodroid-state --list             # List exports on phone

macrodroid-read EXPORT.mdr --list
macrodroid-read EXPORT.mdr --macro "Token-Ping"
macrodroid-read EXPORT.mdr --macro "Token-Ping" --json
macrodroid-read EXPORT.mdr --macro "Token-Ping" --export-macro > token-ping.macro
macrodroid-read EXPORT.mdr --geofences
macrodroid-read EXPORT.mdr --http-config
```

### Workflow for Updating Macros

1. Pull current state:
   ```bash
   macrodroid-state --pull
   ```

2. Extract the deployed macro if modifying an existing one:
   ```bash
   macrodroid-read /tmp/macrodroid-state/EXPORT.mdr --macro "Macro Name" --export-macro > macro-name.macro
   ```

3. Edit the official JSON directly using `macrodroid-llm-schema.yaml`.

4. Validate:
   ```bash
   macrodroid-validate macro-name.macro
   ```

5. Push:
   ```bash
   macrodroid-push macro-name.macro
   ```

6. Import on phone:
   - MacroDroid → Settings → Import/Export → Import
   - Select from `~/macros/`

7. Export/pull again and verify:
   ```bash
   macrodroid-state --detail
   ```

8. Delete staged `.macro` files after import. The `.mdr` export/deployed macro is the canonical source of truth.

### File Locations

- Official schema: `mobile/macrodroid-llm-schema.yaml`
- Current phone export: `mobile/EXPORT.mdr` or `mobile/macros/EXPORT.mdr`
- Phone staging directory: `~/macros/`
- Legacy YAML DSL archive: `mobile/macros/archive/legacy-yaml-dsl-2026-05-09/`
- Legacy compiler backup: `cli-tools/archive/macrodroid-legacy-yaml-dsl-2026-05-09/`

### HTTP Server Trigger Details

MacroDroid's HTTP Server runs on port `7777`. Endpoints are:

```text
http://<phone-ip>:7777/<identifier>
http://<phone-ip>:7777/<identifier>?param=value
```

Useful magic variables in actions:

- `{http_query_string}` - raw query string
- `{http_request_body}` - POST body
- `{http_param=name}` - specific query parameter

Use official `HttpServerTrigger`, `HttpServerResponseAction`, and `HttpRequestAction` fields from `macrodroid-llm-schema.yaml`.

### Debug Logging Pattern

MacroDroid actions can silently succeed/fail. Use shell-script logging actions for visibility.

Log file:

```text
/storage/emulated/0/MacroDroid/logs/debug.log
```

Watch live:

```bash
ssh-phone "tail -f /storage/emulated/0/MacroDroid/logs/debug.log"
```

Shell action payload pattern:

```bash
echo "$(date +%H:%M:%S) [TAG] message qs={http_query_string}" >> /storage/emulated/0/MacroDroid/logs/debug.log
```

Useful MacroDroid magic variables:

| Variable | Context | Value |
|----------|---------|-------|
| `{http_query_string}` | HTTP trigger | Raw query string |
| `{http_request_body}` | HTTP trigger | POST body |
| `{http_param=key}` | HTTP trigger | Specific query param |
| `{lv=varname}` | Any | Local variable value |
| `{v=varname}` | Any | Global variable value |
| `{iterator_dictionary_key}` | Dict iteration | Current key |
| `{iterator_value}` | Dict iteration | Current value |
| `{trigger}` | Any | Trigger name string |
| `{system_time}` | Any | System time |

Place logs at entry, after parsing, inside iterations, and before/after constrained or external actions.

## Termux Configuration

### Templates

- `termux-bashrc-template` - Bash configuration with aliases and shortcuts
- `termux-properties-template` - Termux appearance/behavior settings

### Key Aliases (from bashrc)

```bash
ssh-mac         # SSH to Mac Mini (with redirect-on-exit)
ssh-wsl         # SSH to WSL (with redirect-on-exit)
fetch-bashrc    # Pull latest bashrc from Mac
```

### Storage Access

If MacroDroid can't access Termux private storage, run on phone:
```bash
termux-setup-storage
```

This creates `~/storage/` with symlinks to Downloads, DCIM, etc.
Then push to `~/storage/downloads/` for broader app access.

## Extending MacroDroid Coverage

Do not extend `macrodroid-gen` with custom trigger/action builders. Add coverage by using the official class definitions in `macrodroid-llm-schema.yaml` directly in `.macro` JSON. If MacroDroid adds new classes, update the schema file and rely on `macrodroid-validate` to enforce placement and structure.
