# MacroDroid Cleanup & Infrastructure Plan

**Status**: Implementation in progress
**Last updated**: 2026-02-26

---

## Current State (Audited 2026-02-26)

### Phone Macro Inventory (39 total)

| Macro | Category | State | YAML | Action |
|-------|----------|-------|------|--------|
| Enforce | Enforcement | ✓ enabled | ✓ enforce.yaml | Keep — v2 with wait_until |
| Enforce-old | Enforcement | ✓ enabled | — | **DELETE** (still has /enforce id!) |
| Twitter Management | Enforcement | ✗ disabled | — | Keep — local fallback, create YAML |
| Games Management | Enforcement | ✗ disabled | — | Keep — local fallback, create YAML |
| Youtube Disable | Enforcement | ✓ enabled | — | Create YAML |
| Youtube Enable | Enforcement | ✓ enabled | — | Create YAML |
| Youtube Toggle | Enforcement | ✓ enabled | — | Create YAML |
| Enable Local Fallback | Enforcement | ✓ enabled | ✓ | Keep |
| Disable Local Fallback | Enforcement | ✓ enabled | ✓ | Keep |
| Shizuku Death Logger | System | ✓ enabled | ✓ | **DELETE** — superseded by Shizuku Died |
| Shizuku Died | System | — | ✓ shizuku-died.yaml | **Import new** |
| Shizuku Restored | System | — | ✓ shizuku-restored.yaml | **Import new** |
| Shizuku Restart | System | ✓ enabled | ✓ | Keep |
| Shizuku Watchdog | Telemetry | ✗ disabled | — | **DELETE** |
| Boot Start SSHD | System | ✓ enabled | ✓ boot-sshd.yaml | **DELETE** — superseded by Boot Startup |
| Boot Startup | System | — | ✓ boot-startup.yaml | **Import new** |
| Server Poll | Endpoints | ✓ enabled | — | **DELETE** — replaced by Phone Health |
| Phone Health | System | — | ✓ phone-health.yaml | **Import new** |
| Heartbeat | Endpoints | ✓ enabled | ✓ | Keep (endpoint, not poll) |
| sshd | Endpoints | ✓ enabled | ✓ | Keep |
| Claude notifications | Endpoints | ✓ enabled | — | Create YAML |
| List Exports API | Endpoints | ✓ enabled | ✓ | Keep |
| Geofence Home Enter/Exit | Geofence | ✓ enabled | ✓ | Keep |
| Geofence Gym Enter/Exit | Geofence | ✓ enabled | ✓ | Keep |
| Campus Enter/Exit | Geofence | ✓ enabled | — | Create YAML |
| Twitter Open | Telemetry | ✗ disabled | ✓ | Fix ref to Twitter Management |
| Twitter Open-old | Telemetry | ✓ enabled | — | **DELETE** |
| Twitter Close | Telemetry | ✓ enabled | ✓ | Keep |
| YouTube Open | Telemetry | ✓ enabled | ✓ | Keep |
| YouTube Close | Telemetry | ✓ enabled | ✓ | Keep |
| Games Open | Telemetry | ✓ enabled | ✓ | ✅ Fixed ref → "Games Management" |
| Games Close | Telemetry | ✓ enabled | ✓ | Keep |
| Zappa | System | ✓ enabled | — | Investigate/document |
| Button | System | ✗ disabled | — | **DELETE** |
| Hello worl | Uncategorized | ✓ enabled | — | **DELETE** |
| Termux Open | Telemetry | ✓ enabled | — | Create YAML (or it's an orphan?) |
| Spotify start | Music | ✓ enabled | ✓ | Keep |
| Spotify open | Telemetry | ✓ enabled | ✓ | Keep |
| Change song (swipe) | Music | ✓ enabled | — | Create YAML |
| Youtube Pause/Play | Telemetry | ✓ enabled | ✓ | Keep |
| Campus Enter/Exit | Geofence | ✓ enabled | — | Create YAML |

### Broken Cross-References

| File | Bug | Fix |
|------|-----|-----|
| `games-open.yaml` | Calls `"Thronefall Management"` (doesn't exist) | → `"Games Management"` |
| `twitter-open.yaml` | Calls `"Twitter Management"` (disabled) | Verify works when enabled |

### Duplicate Active Endpoint

**`Enforce-old` is still enabled with identifier `/enforce`** — this means BOTH Enforce (v2) and Enforce-old are listening on the same endpoint. Must delete Enforce-old immediately.

---

## Phase 1: Immediate Cleanup (Push → Import → Delete)

### Delete from phone (manual in MacroDroid):
1. **Enforce-old** — duplicate `/enforce` listener, causes conflicts
2. **Shizuku Watchdog** — superseded by Death Logger
3. **Twitter Open-old** — superseded by Twitter Open
4. **Button** — test macro
5. **Hello worl** — test macro

### Fix and push:
1. `games-open.yaml` — fix macro_name reference
2. `twitter-open.yaml` — verify local fallback ref

---

## Phase 2: Create Missing YAML Specs

Reverse-engineer from .mdr using `macrodroid-read --detail --macro "Name"`:

- `server-poll.yaml` (will be replaced in Phase 4 but document it first)
- `twitter-management.yaml`
- `games-management.yaml`
- `claude-notifications.yaml`
- `campus-enter.yaml` + `campus-exit.yaml`
- `youtube-toggle.yaml`
- `youtube-disable.yaml` + `youtube-enable.yaml`

---

## Phase 3: Boot Sequence Improvement

Rename `boot-sshd.yaml` → `boot-startup.yaml`, expand:

1. Wait 15s (system settle)
2. Start sshd via Termux RUN_COMMAND
3. Wait 3s
4. POST `/phone` boot event to Token-API
5. Notification: "Boot complete — tap to start Shizuku"
6. Launch Shizuku app (user must tap Start — can't automate on unrooted Android 14+)

---

## Phase 4: Consolidated Health Poll (`phone-health.yaml`)

Replace **Server Poll** (server-only, 10min) with a single macro:

**Trigger**: Regular interval, 15 min

**Actions**:
1. GET `/health` → `server_code`
2. If server 200 AND Connection==false → run Disable Local Fallback
3. If server not 200 AND Connection==true → set Connection=false (server gone down)
4. Shizuku check (see Shizuku plan for detection method)
5. POST `/phone` with combined payload: `{"server": 200, "shizuku": "RUNNING"}`
6. Log to telemetry.log

**Delete** Server Poll after verified.

---

## Phase 5: Enforce Endpoint Refactor

The v2 Enforce is in place with:
- if/else_if switch on app name
- disable_app → wait_until app close (5s) → timeout = Shizuku dead
- Calls Shizuku Restart on death
- Single response at end

Pending validation that the wait_until + timeout_var works correctly in production.

---

## Phase 6: Push → Import → Pull Verify Loop

For each batch:
```bash
macrodroid-gen macros/foo.yaml | macrodroid-push - foo.macro
# user imports
macrodroid-state  # verify
ssh-phone "rm ~/macros/*.macro"  # cleanup staging
```

---

## Known Generator Issues Fixed This Session

| Issue | Fix |
|-------|-----|
| `wait_until` missing `timeout_var` / `booleanVariableName` | Added `timeout_var` field |
| Empty string variable comparisons unreliable | Use sentinel values (`"none"`, `"RUNNING"/"STOPPED"`) |
| Multi-line YAML `>` folds to single line (breaks shell scripts) | Single-line commands with `;` separators |
| MacroDroid magic vars `{wifi_ssid}`, `{system_date}`, `{trigger_type}` don't expand in shell | Use shell commands instead: `date`, `dumpsys wifi`, etc. |
| `use_helper: true` breaks output_var capture | See Shizuku plan — detection rethink needed |
