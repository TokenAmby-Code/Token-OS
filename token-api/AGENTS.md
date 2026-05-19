# Token-API Project

Local FastAPI server for Claude instance management, notifications, and system coordination.

## Architecture

- **Mac Server**: `main.py` - FastAPI app on port 7777 (LaunchAgent `ai.openclaw.tokenapi`)
- **WSL Satellite**: `token-satellite.py` - Companion server on WSL port 7777 (systemd `token-satellite.service`)
- **Somnium display/control**: HTML/Obsidian is the preferred rich dashboard surface; tmux owns active-pane operational keybindings; `token-api-tui.py` is legacy terminal status/selection UI
- **Database**: `~/.claude/agents.db` (SQLite, shared with Claude Code)

### Multi-Device Network

```
Mac Mini (100.95.109.23:7777)     ← primary server, all state lives here
  ├── WSL (100.66.10.74:7777)     ← satellite: TTS (Windows SAPI), process enforcement, /restart
  └── Phone (SSH)                  ← TUI restart signals only, webhook notifications
```

- Mac proxies to WSL via `DESKTOP_CONFIG` for enforcement and `/satellite/restart`
- **TTS routing**: WSL-first (Windows SAPI voices) with Mac `say` fallback. Satellite availability cached with 30s TTL health probes. Mobile sessions use webhook notifications instead (no TTS queue).
- `token-restart` orchestrates all three: Mac restart → WSL restart → phone signal
- TUI runs on any device, connects to Mac API at `100.95.109.23:7777`, but new rich visualization work should target HTML served by Token-API and embedded in Obsidian
- 15s startup grace period ignores silence detections after server restart (AHK restart race)

### Somnium Display/Control Directive

High-level directive: see `/Volumes/Imperium/Imperium-ENV/Terra/Ultramar/Somnium Display and Control Surface Directive.md`.

The current direction is:

- HTML is the primary rich visualization surface for somnium dashboards, graphs, timelines, and cohesive state views.
- Obsidian is the preferred frame for viewing those HTML dashboards.
- tmux owns active-pane operator keybindings for the somnium pane/window.
- `token-api-tui.py` should narrow toward compact status rendering and explicit selection-state export.
- Token-API remains the authoritative state/mutation backend.

Do not add new primary operational keybindings to `token-api-tui.py`. New actions should be Token-API/CLI mutations invoked from tmux keybindings. Existing TUI commands are compatibility behavior while the migration is underway.

## Key Files

| File | Purpose |
|------|---------|
| `main.py` | FastAPI server (~5000 lines) |
| `token-satellite.py` | WSL companion server: TTS engine, enforcement (systemd: `token-satellite.service`) |
| `tts-studio.py` | TUI for auditioning/selecting Windows SAPI voices (run on WSL) |
| `timer.py` | TimerEngine v2 — layered composite model, pure logic, no I/O |
| `test_timer.py` | Unit tests for TimerEngine v2 (83 tests) |
| `token-api-tui.py` | Legacy Rich terminal dashboard; should evolve toward compact status/selection export |
| `init_db.py` | Database initialization |
| `DESIGN.md` | Original design doc (partially outdated) |

## Database Tool

Query the local agents.db with the `agents-db` CLI:

```bash
agents-db instances              # Show all instances
agents-db events --limit 10      # Recent events
agents-db tables                 # List tables
agents-db describe claude_instances
agents-db query "SELECT * FROM events WHERE event_type='instance_renamed'"
agents-db --json instances       # JSON output
```

## Key Tables

### claude_instances
Core instance registry. This remains the source of truth for live instance state,
but sanctioned writes now flow through the `instance_mutation.py` helper so
provenance and reconciliation can reason about drift. Key columns:
- `id` - Instance UUID
- `tab_name` - Display name (set via rename, or auto "Claude HH:MM")
- `working_dir` - Instance working directory
- `status` - typically `idle`, `processing`, or `stopped`
- `last_activity` - heartbeat timestamp used for stale detection and reconciliation ordering
- `device_id` - runtime host identity, e.g. `Mac-Mini`, `Token-S24`
- `is_subagent` - 1 if spawned headlessly or as a background worker
- `tmux_pane` - pane projection target for `@CC_STATE` and legion tint
- `legion` / `synced` / `input_lock` - high-frequency control-plane fields now covered by provenance
- `tts_mode` / `tts_voice` / `notification_sound` - per-instance voice and mute state
- `session_doc_id` / `session_doc_policy` / `continuity_binding_source` - active continuity binding
- `workflow_state` / `workflow_updated_at` / `stop_allowed` / `next_required_action` - coarse workflow state
- `dispatch_target` / `dispatch_window` / `dispatch_mode` / `dispatch_slot` - dispatch identity
- `instance_type` / `follow_up_sop` / `zealotry` / `victory_at` / `victory_reason` - lifecycle + follow-up controls
- `discord_hosted` / `discord_channel` / `transplant_target_session` - operator linkage and transplant handoff state
- `wrapper_launch_id` - wrapper ingress correlation key when present

### events
Event log for instance lifecycle, renames, TTS, notifications.

### workflow_events
Append-only workflow/continuity event stream for machine-readable state transitions.
Examples: `session_doc_bound`, `continuity_binding_changed`, `workflow_state_changed`, `workflow_closed`.

### instance_mutations
Append-only provenance log for sanctioned `claude_instances` row mutations.
Key fields:
- `mutation_type` - coarse mutation class such as `instance_registered`, `status_changed`, `continuity_binding_changed`, `instance_stopped`
- `write_source` - sanctioned writer identity: `hooks`, `api`, `system_worker`, `migration`, `exceptional_direct`
- `write_txn_id` - per-write correlation UUID
- `actor` - request or subsystem actor, e.g. `SessionStart`, `SessionEnd`, `assign-doc`
- `service_version` - git SHA or fallback app version captured at process boot
- `wrapper_launch_id` - launcher correlation when the mutation is tied to wrapper ingress
- `field_names_json`, `before_json`, `after_json` - concise changed-field snapshot, not full row dumps

Sanction policy:
- Direct reads are fine.
- New instance-row writes should prefer `sanctioned_update_instance()` / `sanctioned_insert_instance()`.
- Remaining direct SQL writes to `claude_instances` should be treated as explicit debt and will show up as suspicious in reconciliation until migrated.

## Core API Endpoints

### Aspirant full-session launch

Token-API inbox aspirants now launch real managed Claude sessions instead of the old automatic MiniMax/Sonnet implantation pipeline.

Entry points:
```
POST   /api/inbox/create                # Create aspirant note and launch managed legion session
POST   /api/inbox/notify                # Notify/create aspirant and launch managed legion session
```

Launch contract:
- Create aspirant note under `Imperium-ENV/Aspirants/`.
- Create linked session doc under `Imperium-ENV/Terra/Sessions/`.
- Session doc filenames are descriptive lower-kebab and never date-prefixed; dates stay in frontmatter.
- Do not synthesize names from cwd, timestamp, UUID, pane ID, model, or machine. If a doc needs a name, nudge the live instance to choose one with `session-doc-name "Descriptive Title"`.
- Attached instances may derive from a real named session doc (`<session-doc-slug>-1`, `<session-doc-slug>-2`, ...), not by mutating the doc from the instance name.
- Mark note frontmatter with `aspirant_launcher: dispatch`, `aspirant_dispatch_target: legion:new`, launch id, session status, and session doc path.
- Start `dispatch --target legion:new --dir <Imperium-ENV> --session-doc <doc> --system-prompt-file <file> --prompt-file <file> --gt`.
- Suppress duplicate launches when the note already has `aspirant_launch_id` and status `launching` or `launched`.
- On failure, mark the note `aspirant_session_status: failed` and record `aspirant_launch_error`.

Related CLI behavior is documented in `/Volumes/Imperium/Scripts/cli-tools/docs/aspirant-dispatch.md`.

### Instance Management
```
POST   /api/instances/register          # Register new instance
DELETE /api/instances/{id}              # Stop instance
PATCH  /api/instances/{id}/rename       # Rename (sets tab_name)
POST   /api/instances/{id}/activity     # Update processing state
POST   /api/instances/{id}/unstick     # Nudge stuck instance (?level=1 SIGWINCH, ?level=2 SIGINT)
GET    /api/instances/{id}/diagnose    # Get detailed process diagnostics
GET    /api/instances                   # List all instances
GET    /api/instances/{id}/todos        # Get instance task list
GET    /api/instances/{id}/workflow-events  # Recent workflow event log
GET    /api/instances/{id}/provenance       # Recent sanctioned instance mutations
GET    /api/instances/{id}/reconciliation   # Drift classification for one instance
GET    /api/reconciliation/instances        # Fleet reconciliation read model
```

### Notifications
```
POST   /api/notify                      # Send notification
POST   /api/notify/tts                  # TTS only
POST   /api/notify/sound                # Sound only
GET    /api/notify/queue/status         # TTS queue status
POST   /api/tts/skip                    # Skip current TTS (?clear_queue=true to clear queue)
```

### Pavlok (Shock Watch)
```
POST   /api/pavlok/zap                  # Send stimulus (?type=zap|beep|vibe&value=1-100&reason=manual)
POST   /api/pavlok/toggle               # Toggle enabled (?enabled=true|false, no param = toggle)
GET    /api/pavlok/status               # Current state (enabled, cooldown, last stimulus)
```

Pavlok is hooked into all 3 enforcement paths (desktop blocked, phone blocked, break exhausted).
Config: `PAVLOK_CONFIG` in main.py. Token: `.env` `PAVLOK_API_TOKEN`. Cooldown: 45s between stimuli.

### Productivity Check-Ins
```
POST   /api/checkin/submit              # Submit check-in response (energy, focus, mood, etc.)
GET    /api/checkin/today               # All check-ins for today + pending/completed
GET    /api/checkin/status              # Next check-in, completed list, pending list
POST   /api/checkin/trigger/{type}      # Manually trigger a check-in (testing)
```

Check-in types: `morning_start` (9am), `mid_morning` (10:30), `decision_point` (11am), `afternoon` (1pm), `afternoon_check` (2:30pm). Weekdays only. Skipped when work_mode is `clocked_out` or `gym`.

Submit body: `{"type": "morning_start", "energy": 7, "focus": 8, "mood": "good", "notes": "..."}`

Responses are stored in `checkins` table and written as time-stamped frontmatter fields (e.g., `energy_0900`, `focus_0900`) to the daily note in `~/Documents/Imperium-ENV/Journal/Daily/`.

### Dictation
```
GET    /api/dictation                   # Current dictation (Wispr) state
POST   /api/dictation                   # Set dictation state (?active=true|false)
```

### System
```
GET    /api/dashboard                   # Dashboard data
GET    /api/work-mode                   # Current work mode
POST   /api/headless                    # Toggle headless mode
POST   /satellite/restart               # Proxy restart to WSL satellite
GET    /health                          # Health check (includes tts_backend status)
```

### Satellite Endpoints (WSL, port 7777)
```
GET    /health                          # Heartbeat (includes tts_engine + kvm_watchdog status)
POST   /enforce                         # Close Windows process by alias
GET    /processes                       # List distraction-relevant processes
POST   /tts/speak                       # Speak via Windows SAPI (blocking, persistent PS engine)
POST   /tts/skip                        # Skip current TTS playback
GET    /kvm/status                      # DeskFlow watchdog state (state, mac_reachable, deskflow_running)
POST   /kvm/control                     # Manual DeskFlow control (action: start|reload|stop|hold, hold_minutes: 30)
POST   /restart                         # Git pull + systemd restart
```

**KVM Watchdog**: WSL satellite manages the server side. It starts DeskFlow at boot, checks for an established Mac client connection, and uses tiered recovery: Mac wake/start → local DeskFlow reload → full local restart → Mac client reload/restart. Failed recovery attempts back off exponentially and eventually enter `ceased` until manual/signal intervention. Mac Token-API also runs a client-side supervisor: if the WSL DeskFlow port is absent, it stops the Mac client so DeskFlow cannot retry-spam internally, then probes with exponential backoff and reopens the client only when the server port is reachable. Mac KVM start/reload also runs `Shell/deskflow-keymap-guard.sh`, which pins the macOS input source to Australian and keeps `languageSync=false`; this is the authoritative fix for the recurring `'` → `:` keymap regression. Replaces the old "Deskflow" Windows scheduled task (now disabled). Vault reference: `Terra/Ultramar/Personal-Infra/Deskflow KVM.md`.

### Discord Integration
```
POST   /api/discord/message           # Receive forwarded message from discord-cli daemon
```

The discord-cli daemon (port 7779) forwards all incoming Discord messages to this endpoint. Messages are logged to the `events` table with `event_type='discord_message'` and `device_id='discord'`.

Query recent Discord messages:
```bash
agents-db query "SELECT json_extract(details, '$.channel_name') as channel, json_extract(details, '$.author_name') as author, json_extract(details, '$.content') as msg, created_at FROM events WHERE event_type='discord_message' ORDER BY created_at DESC LIMIT 10"
```

## Timer State Machine (v2 — Layered Composite Model)

Inputs are signals, not output modes. Productivity detections and distraction detections contribute to internal layer state; the server derives the public timer mode from that composite. Phone, desktop, work-action, process, and geofence observations should not directly assert final modes such as `working`, `multitasking`, or `break`.

Three independent layers currently compose into 6 effective modes:

### Layers

| Layer | Values | Source |
|-------|--------|--------|
| **Activity** | `working`, `distraction` | AHK audio-monitor, phone app detection |
| **Productivity** | `active`, `inactive` | Claude instances processing, work-action calls |
| **Manual** | `None`, `BREAK`, `SLEEPING` | User-initiated overrides |

### Effective Mode Derivation (priority order)

| Priority | Condition | Mode |
|----------|-----------|------|
| 1 | Manual override set | **BREAK** or **SLEEPING** |
| 2 | Prod inactive + distraction | **BREAK** (auto) |
| 3 | Prod active + scrolling/gaming ≥10min | **DISTRACTED** |
| 4 | Prod active + distraction <10min | **MULTITASKING** |
| 5 | Prod inactive + working | **IDLE** |
| 6 | Prod active + working | **WORKING** |

### Break Rates (integer 1:1)

| Mode | Rate | Effect |
|------|------|--------|
| WORKING | +1:1 | Earns 60 min/hr |
| MULTITASKING | 0:0 | Neutral |
| IDLE | 0:0 | Neutral |
| DISTRACTED | -1:1 | Spends 60 min/hr |
| BREAK | -1:1 | Spends 60 min/hr |
| SLEEPING | 0:0 | Neutral |

### Key Rules

- **DISTRACTED requires productivity** — only scrolling/gaming trigger it after 10min. Video stays MULTITASKING.
- **Phone foreground is a distraction contribution** — it may move the derived activity layer to `distraction`, but it must not create `trigger=phone_app` shifts or assert `work_gaming`.
- **Productivity remains server-derived** — active Claude/Codex instances and work-action calls drive `Productivity`; phone apps have no authority to mark work active.
- **Location is a modifier** — geofence state can apply exemptions/bounties/manual context, but it should feed the composite state rather than bypassing derivation.
- **Parameterized idle timeout**: 2hr from WORKING, 2min from MULTITASKING.
- **Gym bounty**: +30 min break on gym exit (`apply_gym_bounty()`).
- **Daily reset**: 7 AM (CronTrigger hour=7).
- **Serialization**: `format_version: 2` in DB. Legacy v1 flat modes auto-migrated on load.

### Timer API Endpoints

```
GET    /api/timer                     # Full state: effective_mode + layers + counters
POST   /api/timer/break               # Enter break (manual override)
POST   /api/timer/pause               # Set productivity inactive (→ IDLE)
POST   /api/timer/resume              # Exit break/sleeping, set prod active
POST   /api/timer/sleep               # Enter sleeping (manual override)
POST   /api/work-action               # Signal productivity active
POST   /api/timer/daily-reset         # Force daily reset
POST   /api/timer/reset               # Reset to fresh state
POST   /api/timer/set-break           # Debug: set break time directly
GET    /api/timer/shifts              # Today's shift analytics
```

### Timer Engine Methods (timer.py)

- `set_activity(activity, is_scrolling_gaming, now_ms)` — AHK/phone detection
- `set_productivity(active, now_ms)` — Claude instances / work actions
- `enter_break(now_ms)` / `enter_sleeping(now_ms)` — manual overrides
- `resume(now_ms)` — exit manual mode
- `apply_gym_bounty(now_ms)` — +30min on gym exit
- `effective_mode` — derived property from layers
- `tick(now_ms, date, hour)` — main loop (1s interval)

### Integration Points (main.py)

- **Desktop detection** (`handle_desktop_detection`): silence/music → `set_activity(WORKING)`, video/scrolling/gaming → `set_activity(DISTRACTION)`
- **Phone activity** (`handle_phone_activity`): open events for distraction apps contribute `Activity.DISTRACTION` and log `phone_distraction_observed`; close/stale events should remove or age out that contribution, not assert work by themselves
- **Timer worker** (every 10s): polls DB for processing instances → `set_productivity()`
- **Hook handlers** (`prompt_submit`, `post_tool_use`): → `set_productivity(True)`
- **Location events**: gym exit → `apply_gym_bounty()`, gym/campus → `idle_timeout_exempt`

## Instance Naming

The `tab_name` field stores the instance display name. The TUI displays:
1. Custom `tab_name` (if user renamed it)
2. `working_dir` path (if using default "Claude HH:MM" name)

Auto-generated names match pattern `Claude HH:MM`. Any other name is considered custom.

Rename via:
- CLI: `instance-name "my-name"`
- API: `PATCH /api/instances/{id}/rename` with `{"tab_name": "..."}`
- Legacy TUI compatibility: press `r` on selected instance

## Subagent Tagging

Headless Claude instances spawned by `subagent --claude`, cron jobs, or scripts are tagged at registration to reduce TUI clutter.

**How it works:**
1. The `subagent` CLI exports `TOKEN_API_SUBAGENT="subagent:claude"` before invoking `claude -p`
2. The Claude Code hook (`~/.claude/hooks/generic-hook.sh`) forwards this env var in the `.env` payload
3. The server reads `TOKEN_API_SUBAGENT` from the hook payload and sets `is_subagent=1`, `spawner=<value>`
4. Subagents are auto-named `"sub: <spawner>"` and **skip TTS profile assignment** (no voice slot consumed)

**TUI/display behavior:**
- Subagents are **hidden by default** — press `a` to toggle visibility
- When visible, subagent rows are dimmed with an `@` prefix
- Status bar shows `+N sub` count when subagents are hidden

**Extending to other spawners:**
To tag cron jobs or watchdog scripts, export `TOKEN_API_SUBAGENT` before the `claude -p` call:
```bash
export TOKEN_API_SUBAGENT="cron:task-worker"
claude -p "do the thing"
```

## Somnium Controls

The old TUI keyboard layer predates the managed tmux workspace and overlaps with tmux responsibilities. Treat it as compatibility behavior.

Preferred model:

- tmux key tables own active-pane operator shortcuts for the somnium pane/window
- CLI/API commands perform mutations
- the TUI publishes selected UI object state, for example `~/.claude/tui-state/somnium.json`
- HTML/Obsidian renders rich dashboards and graphs from Token-API read models

Selection-state files are operator UI state only. Commands consuming them must validate referenced instances/jobs against Token-API before mutating anything.

### Legacy TUI Controls

```
↑↓ / jk  - Navigate instances (up/down)
h / l    - Switch info panel (Events ↔ Logs)
r        - Rename selected
s        - Stop selected
d        - Delete (with confirm)
y        - Copy resume command to clipboard (yank)
U        - Unstick frozen instance (SIGWINCH, gentle nudge)
I        - Interrupt frozen instance (SIGINT, cancel current op)
K        - Kill frozen instance (SIGKILL, auto-copies resume cmd)
a        - Toggle subagent visibility (hidden by default)
c        - Clear all stopped
m        - Cycle TTS mode (verbose/muted/silent/voice-chat)
o        - Change sort order
R        - Restart server
q        - Quit
```

### Info Panel Pages

The TUI has a paginated info panel (toggled with H/L):
- **Page 0 (Events)**: Recent events from the database (registrations, stops, renames, TTS)
- **Page 1 (Logs)**: Server logs from the API

The current page is shown in the status bar.

## Common Debug Patterns

```bash
# Check if server is running
token-ping health                # or: curl http://localhost:7777/health

# View active instances
agents-db instances

# Check recent events
agents-db events --limit 20

# Verify rename worked
agents-db query "SELECT id, tab_name FROM claude_instances WHERE id='...'"

# Inspect sanctioned instance writes
agents-db query "SELECT mutation_type, write_source, actor, write_txn_id, created_at FROM instance_mutations ORDER BY id DESC LIMIT 20"

# Inspect workflow event stream
agents-db query "SELECT event_type, workflow_state, event_owner, created_at FROM workflow_events ORDER BY id DESC LIMIT 20"

# Watch server logs (if running via systemd)
journalctl -u token-api -f

# Test TTS (wait 10s after for user feedback)
token-ping notify/test           # or: curl -s http://localhost:7777/api/notify/test | jq .
# Then sleep 10 and ask user if they heard it
```

**Testing TTS:** After running a TTS test, sleep for ~10 seconds before continuing so the user can confirm whether they heard sound and/or speech.

## Profile System

### Voice Pool (9 foreign accents + 3 fallback)

Voices assigned via **random-start linear probe** (open addressing): one random call per slot, increment on collision. Only active instances (`processing`/`idle`) hold a voice — stopped instances release theirs.

**Primary pool** (foreign accents, distinct and identifiable):

| Profile | WSL Voice | Mac Fallback | Region | Sound |
|---------|-----------|-------------|--------|-------|
| profile_1 | Microsoft George | Daniel | UK M | chimes.wav |
| profile_2 | Microsoft Susan | Karen | UK F | notify.wav |
| profile_3 | Microsoft Catherine | Karen | AU F | ding.wav |
| profile_4 | Microsoft James | Daniel | AU M | tada.wav |
| profile_5 | Microsoft Sean | Moira | IE M | chord.wav |
| profile_6 | Microsoft Hazel | Moira | IE F | recycle.wav |
| profile_7 | Microsoft Heera | Rishi | IN F | chimes.wav |
| profile_8 | Microsoft Ravi | Rishi | IN M | notify.wav |
| profile_9 | Microsoft Linda | Karen | CA F | ding.wav |

**Fallback pool** (US English, used when primary is exhausted):
- David, Zira, Mark → less distinct, but functional

## Provenance And Reconciliation

### Sanctioned Write Layer

`instance_mutation.py` is the sanctioned bridge layer for `claude_instances` row writes.
It is not the final service extraction, but it now covers most runtime control-plane mutations.

Current sanctioned coverage:
- hook-driven registration, supplant refresh, and session end flows already migrated in `main.py`
- manual session-doc bind / create / unbind and hard-delete unlink side effects
- API writes for rename, activity/status, stop, kill fallback stop-marking, unstick PID refresh, legion, synced, input_lock
- API writes for transplant pending, zealotry, discord linkage, instance type, archive / unarchive, and victory metadata
- per-instance voice reassignment, per-instance `tts_mode`, voice-chat mode sync, and global TTS mode fanout
- background/system writes for stale cleanup, stale processing clear, stop-evaluator idle transition, auto-name rename
- sync stop-hook stop marking via `sanctioned_update_instance_sync()`

Remaining direct-write debt worth tracking:
- bootstrap / migration / full-table admin operations still use direct SQL where provenance is not the goal
- the main runtime exception is full-table admin deletion in `DELETE /api/instances/all`
- reconciliation still treats any future unmigrated runtime writes as suspicious rather than fatal

### Reconciliation Statuses

`GET /api/instances/{id}/reconciliation` classifies:
- `clean` - current row and pane projection align with sanctioned writes
- `pending_projection` - pane queue entries exist, so tmux projection is expected to catch up
- `unprovenanced_write` - current row diverges from latest sanctioned value for one or more tracked fields
- `state_drift` - workflow / continuity fields are internally incoherent
- `projection_drift` - tmux pane state is stale or missing with no pending queue

Suspicious statuses emit `instance_reconciliation_drift` into the normal `events` log.

**Ultimate fallback**: Microsoft David (if 12+ concurrent instances somehow)

Re-select voices via `tts-studio.py` on WSL. The DB `tts_voice` column stores the WSL voice name. Profile lookup derives mac_voice for fallback.

### TTS Routing

- **Queue path** (`tts_queue_worker`): Looks up profile by WSL voice, tries satellite first, falls back to Mac
- **Direct path** (`/api/notify/tts`, `/api/notify`): Mac-only (no profile context)
- **Mobile path** (`device_id == "Token-S24"`): Webhook to phone, no TTS queue
- **Skip**: Routes to satellite `/tts/skip` or kills local `say` process based on `TTS_BACKEND["current"]`
- **Satellite down**: Health probe cached 30s, re-detected within 30s of PC coming online

### TTS Mode Cycle

Mode cycle: `verbose -> muted -> silent -> voice-chat -> verbose`. Stored as `tts_mode` in DB per instance. Legacy TUI compatibility cycles it with `m`; new operator bindings should call CLI/API mutations from tmux.

| Mode | Behavior |
|------|----------|
| verbose | Full TTS speech for all notifications |
| muted | Sound effects only, no speech |
| silent | No audio output |
| voice-chat | Activates AskUserQuestion voice hooks and dictation tracking |

### Voice Chat

> **STATUS: UNVALIDATED (2026-03-09)** — Not yet tested end-to-end.

Voice chat is a TTS mode (`voice-chat`) rather than a separate system. Legacy TUI compatibility cycles into it with `m`. When active, the instance name shows a microphone emoji in the TUI (TUI-only display, not stored in DB).

The old `/api/instances/{id}/voice-chat` endpoint still works but also sets `tts_mode` in DB. `VOICE_CHAT_SESSIONS` in-memory dict is re-hydrated from DB `tts_mode` on instance list queries.

## Dictation State Tracking

> **STATUS: UNVALIDATED (2026-03-09)** — Not yet tested end-to-end.

Global dictation state tracked via `POST /api/dictation` (set) and `GET /api/dictation` (read). State is in-memory (`DICTATION_STATE` global), not persisted to DB.

### Sources

| Script | Trigger | Method |
|--------|---------|--------|
| `script-compiler.ahk` | `~^#Space` keyboard toggle | Blind toggle |
| `ring-remap.ahk` | Right button | Blind toggle |
| `voice-select-other.ahk` | Explicit on/off | GET state, only toggle if needed (WisprOff/WisprOn) |

`voice-select-other.ahk` uses explicit WisprOff/WisprOn instead of blind toggles — it GETs the current state first and only toggles if the state doesn't match the desired value.

## CLI Tools

### Token-API Specific

| Command | Purpose |
|---------|---------|
| `agents-db` | Query local agents.db database |
| `token-status` | Quick server status check |
| `token-restart` | Multi-device restart orchestrator (Mac → WSL → phone, no sync needed) |
| `notify-test` | Send test notifications |
| `tts-skip` | Skip current TTS (--all to clear queue) |
| `instance-name` | Rename current session |
| `instance-stop` | Stop/unstick/kill instance by name (fuzzy match) |
| `instances-clear` | Bulk clear stopped instances |
| `token-ping` | Hit any endpoint (fuzzy match, auto-restart, OpenAPI-aware) |
| `timer-status` | Quick timer status (mode, break time, work time) |
| `timer-mode` | Switch timer mode (break, pause, resume) |
| `subagent` | Multi-backend sub-agent launcher (--claude, --codex, --blocking) |

### General (also useful here)

| Command | Purpose |
|---------|---------|
| `deploy local` | Run local dev server with ngrok |
| `test` | Send test messages to local server |

### Examples

```bash
# Quick status check
token-status

# Multi-device restart (Mac → WSL → phone, no sync needed in NAS era)
token-restart                    # Full restart: Mac, WSL satellite, phone TUI
token-restart --from <dir>       # Update plist to serve from <dir>, then restart
token-restart --wsl-only         # WSL satellite only (HTTP or SSH fallback)
token-restart --tui-only         # TUI restart signals only (no server restart)
token-restart --kill             # Kill Mac server (launchd auto-restarts)
token-restart --watch            # Full restart + tail logs
token-restart --status           # Multi-device status (Mac + WSL + phone)

# Stop/unstick/kill instances
instance-stop "auth-refactor"    # Stop by name
instance-stop --unstick "auth"   # Nudge stuck instance (L1, SIGWINCH)
instance-stop --unstick=2 "auth" # Interrupt stuck instance (L2, SIGINT)
instance-stop --kill "auth"      # Kill frozen instance (SIGKILL, shows resume cmd)
instance-stop --diagnose "auth"  # Show process state, wchan, children, FDs
instance-stop --list             # List active instances
instances-clear                  # Preview stopped instances
instances-clear --confirm        # Delete stopped instances

# Test TTS
notify-test "Hello from Token-API"

# Test sound only
notify-test --sound-only

# Skip TTS
tts-skip                         # Skip current TTS
tts-skip --all                   # Skip and clear queue

# Timer
timer-status                     # One-line: mode, break, work time
timer-status --watch             # Live updating (1s refresh)
timer-status --json              # Raw JSON
timer-mode break                 # Enter break mode
timer-mode pause                 # Set productivity inactive (→ IDLE)
timer-mode resume                # Exit break/sleeping, set prod active
timer-mode status                # Show mode + layer state

# Hit any endpoint
token-ping                       # List all endpoints (from OpenAPI)
token-ping health                # GET /health
token-ping timer/break           # POST /api/timer/break (prefix match)
token-ping break                 # POST /api/timer/break (suffix match)
token-ping zap type=beep value=75  # POST with query params
token-ping notify message=hello  # POST with JSON body (schema-aware)
token-ping --raw health | jq .   # Pipe-friendly raw JSON
token-ping --no-restart health   # Skip auto-restart if server down

# Query database
agents-db events --limit 5
```

## Known Issues & Fixes

### Display Name Priority (Fixed 2026-01-26)
The `format_instance_name()` function in the TUI now correctly prioritizes custom `tab_name` over `working_dir`. Previously, renamed instances still showed the directory path.

Location: `token-api-tui.py:280` - `is_custom_tab_name()` and `format_instance_name()`

### Backspace in TUI Rename
The TUI rename input captures raw terminal characters. Backspace (`\x7f`) may appear in names if terminal handling is imperfect. Workaround: use `instance-name` CLI instead.

### is_processing Flag Not Persisting (Fixed 2026-01-26)
Three bugs caused the green arrow (processing indicator) to not display properly:

1. **PostToolUse clearing flag**: The `handle_post_tool_use()` was setting `is_processing=0` on every tool use, immediately clearing the flag set by `prompt_submit`. Fixed to only update `last_activity` as a heartbeat.

2. **Timezone mismatch in stale worker**: The `clear_stale_processing_flags()` worker compared Python local timestamps against SQLite UTC time, causing a 7-hour offset. All flags appeared "stale" immediately. Fixed by adding `'localtime'` to the SQLite datetime comparison.

3. **Todos endpoint wrong path**: The `/api/instances/{id}/todos` endpoint looked in `~/.claude/todos/` (old format) instead of `~/.claude/tasks/{id}/` (new TaskCreate format). Fixed to read individual task JSON files from the correct location.

Location: `main.py` - `handle_post_tool_use()`, `clear_stale_processing_flags()`, `get_instance_todos()`

### TUI Todo Caching (Added 2026-01-26)
The TUI now caches todo data per instance. When `is_processing=0`, it displays cached data instead of empty values. This prevents progress/task columns from disappearing between prompts.

Location: `token-api-tui.py` - `todos_cache` global, `get_instance_todos()` with `use_cache` parameter

## Development Notes

- Server runs on port 7777 (hardcoded in `main.py`)
- TUI polls database directly, not via API (for speed)
- TUI refresh interval: 2 seconds
- Database changes from CLI/API are picked up on next TUI refresh
- New rich dashboards should be implemented as HTML served by Token-API, suitable for Obsidian iframe/embed viewing.
- New operational shortcuts should be tmux active-pane bindings calling CLI/API commands, not new TUI key handlers.

## Potential Future Tools/Skills

- **/token-debug skill**: Interactive debugging workflow
