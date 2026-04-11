# Token-API Code Review — Sisters of Battle Audit

**Date**: 2026-04-07
**Reviewer**: Codex (Sisters of Battle)
**Scope**: Full codebase structural analysis
**Verdict**: The cathedral stands. The sprawl is load-bearing. Catalog follows.

---

## 1. The Monolith: main.py (14,592 lines)

### By the Numbers

| Metric | Value |
|--------|-------|
| Route handlers | 184 endpoints |
| Global state dicts | 44 mutable containers |
| Functions > 100 lines | 23 (2 exceed 250 lines) |
| Inline SQL queries | 256+ (all parameterized, no injection risk) |
| `aiosqlite.connect()` calls | 122 |
| Sections (header-delimited) | 47 |

### Section Map (largest blocks)

| Lines | Section | Notes |
|-------|---------|-------|
| 1,060–2,425 | Instance Lifecycle Routes | Registration, delete, kill, unstick, rename, voice, transplant, zealotry, legion |
| 2,426–3,452 | Golden Throne + Kreig Dispatch | Multi-instance coordination, batch tmux pane startup |
| 3,453–3,957 | Instance Lifecycle Type | Archive/unarchive, type switching |
| 3,994–4,364 | Productivity Check-In System | Daily notes, frontmatter merging, Discord checkins |
| 4,365–4,699 | Enforcement + Pavlok | Enforcement cascade v3, shock watch integration |
| 5,377–5,875 | Phone v3 Endpoints | 18 endpoints for activity, enforcement, lock states |
| 5,989–6,755 | Phone Activity Detection | Complex validation with state machines (268 lines) |
| 8,287–9,865 | TTS Queue System | Background worker, sequential playback, sound routing (1,579 lines) |
| 9,866–11,071 | Claude Code Hook Handlers | SessionStart (513 lines!), PreToolUse, PostToolUse, Stop (277 lines) |
| 11,672–12,835 | Aspirant Pipeline (Inbox) | Note capture, trials system, implantation workflow |
| 12,836–14,092 | Session Documents | Doc lifecycle, primarch linking, deployment queue |
| 14,318–14,592 | Fleet State + Habits | Fleet-wide state dict, habit tracker |

### Monster Functions

| Function | Lines | Risk |
|----------|-------|------|
| `handle_session_start` | 513 | 5 different supplant logic paths, touches 12+ concerns |
| `run_implantation` | 282 | Trial execution with state transitions |
| `handle_stop` | 277 | Instance cleanup touching 15+ global state dicts |
| `handle_phone_activity` | 268 | Phone app gating with break time logic |
| `handle_desktop_detection` | 207 | AHK state machine with implicit transitions |
| `timer_worker` | 201 | Background loop with 3 implicit session-tracking globals |

### Duplicated Patterns

| Pattern | Count | Fix |
|---------|-------|----|
| `async with aiosqlite.connect(DB_PATH) as db:` | 122 | Extract connection helper or use pool |
| `SELECT COUNT(*) FROM claude_instances WHERE status IN (...)` | 10+ | `count_active_instances()` helper |
| `raise HTTPException(status_code=..., detail=...)` | 44 | Validation middleware or shared validators |
| `await log_event(...)` | 117 | Already centralized — this is fine |
| Phone/desktop state dict reads | 80+ | Dataclass or TypedDict with accessor methods |
| Zealotry UPDATE pattern | 5 | `update_zealotry(instance_id, value)` |

### Dead Code

- **Headless Mode** (lines 5,144–5,376): Present but guarded by `MACHINE != "mac"`, endpoints still decorated
- **`_sync_*` functions** (lines 4,703+): Sync wrappers that appear unreachable
- **Audio Proxy State** (line 5,133): Empty placeholder section header with no actual code

### Architectural Observations

**What works well:**
- Section headers are consistent — you can navigate by searching `# ============`
- SQL is all parameterized (no injection risk)
- Import structure is clean — no circular deps
- Background workers are launched in `lifespan()` with proper async context

**What doesn't:**
- 44 global dicts are the nervous system — any state mutation is implicit, grep-dependent
- No query abstraction: schema changes require editing 40+ hand-written SQL statements
- State machines (phone activity, desktop detection, enforcement cascade) are hand-coded if-else — no invalid-transition protection
- Background workers (`tts_queue_worker`, `timer_worker`) have no crash recovery or supervision

---

## 2. Satellite: token-satellite.py (1,307 lines)

**Architecture**: Clean. Stateless FastAPI with two persistent engines (TTSEngine, DeskFlowWatchdog).

**Strengths:**
- Zero coupling to main.py — pure utility service
- Well-structured classes with clear lifecycle methods
- 17 endpoints, none over 60 lines

**Weaknesses:**
- 74-line PowerShell script embedded as Python string literal (TTS_ENGINE_PS)
- 12+ hardcoded Windows paths (CMD_EXE, POWERSHELL_EXE, AHK_EXE)
- MAC_API_BASE = `100.95.109.23:7777` hardcoded (Tailscale IP)
- Repeated `subprocess.run()` patterns with no abstraction

**Verdict**: Healthy. Low risk, low debt.

---

## 3. TUI: token-api-tui.py (3,723 lines)

**Architecture**: Functional monolith with Rich library. No classes.

**Critical Issue**: `main()` is 1,014 lines — terminal setup, key listening, 50+ action handlers, rendering, and refresh logic all in one function with 11 global declarations.

**Duplicated Patterns:**
- Terminal interaction pattern (input_mode → live.stop → tcsetattr → prompt → restore) repeated 6x
- 60+ keybinding elif blocks — should be `KEY_MAP = {'q': 'quit', 'r': 'rename', ...}` + loop

**Coupling:**
- 24+ hardcoded API endpoint URLs (would break if main.py renames routes)
- 2 direct sqlite3 queries to agents.db (assumes `session_documents` table)
- No connection pooling for urllib

**Verdict**: Functional but brittle. The 1,014-line main() is the highest-risk function in the entire codebase for maintainability.

---

## 4. Supporting Modules

### Core Engines (imported by main.py)

| Module | Lines | Coupling | Health |
|--------|-------|---------|--------|
| timer.py | 784 | Pure (zero imports) | Excellent — clean state machine, fully tested |
| cron_engine.py | 1,142 | Imports nas_mount, db_schema; lazy-imports post_run_graph | Good — well-structured, tested |
| db_schema.py | 450 | Imports CronEngine for table init | Good — canonical schema owner (new) |
| schedule.py | 233 | Self-contained FastAPI router | Good — Calendly integration |
| nas_mount.py | 106 | Pure stdlib | Good — macOS utility |

### Standalone Agents (HTTP-decoupled, run as cron/subprocess)

| Module | Lines | Purpose | Status |
|--------|-------|---------|--------|
| custodes_heartbeat.py | 744 | Discord #briefing commentary | PROD |
| custodes_watchtower.py | 294 | Offline escalation ladder | PROD |
| custodes_checkin.py | 179 | Contextual check-in | PROD |
| alpharius_heartbeat.py | 118 | Fleet health watchdog (seeded job) | PROD |
| corax_watchtower.py | 281 | File integrity monitoring | PROD |
| morning_launcher.py | 338 | Morning context gathering | PROD |
| morning_session.py | 496 | Morning session launch | PROD |
| stop_hook.py | 753 | Session transcript + cleanup | PROD |
| discord_responder.py | 82 | Discord bot message handler | PROD |
| post_run_graph.py | 284 | LangGraph guard + victory chain | PROD |

### Dead/Experimental

| Module | Lines | Status |
|--------|-------|--------|
| fleet_dispatch_poc.py | 382 | POC — name says it. Not in critical path. |
| timer-debug-log.py | 142 | Debug utility, never invoked |
| tts-studio.py | 412 | WSL voice audition tool |
| tui-screenshot.py | 99 | Screenshot utility |

**Architectural Strength**: All named agents (custodes, corax, alpharius, morning) communicate via HTTP only — zero Python imports from main.py. Failure is isolated.

---

## 5. Test Coverage

### What's Tested

| Suite | Tests | Lines | Quality |
|-------|-------|-------|---------|
| test_timer.py | 83 | 926 | Excellent — pure unit, deterministic, comprehensive |
| test_cron_engine.py | 800+ | 1,030 | Strong — unit + integration, proper mocking |
| tests/test_legion_synced.py | ~30 | 411 | Adequate — schema + API smoke tests |
| tests/test_voice_pool.py | ~20 | 219 | Partial — unit solid, API integration flaky |

### What's Not Tested

- **main.py** (14,592 lines): Zero unit tests. Zero integration tests. 184 endpoints untested.
- **Hook system**: `StopValidate` action is posted by `stop-validator.sh` but not recognized by main.py — live bug visible in logs
- **Session doc lifecycle**: Documented as producing 410+ duplicates — no tests for resolution logic
- **Stop hook critical path**: No tests for transcript extraction, evaluator scheduling, Discord mirroring
- **TTS routing**: No tests for satellite fallback, queue ordering, skip behavior
- **Phone/desktop activity detection**: Complex state machines with zero test coverage

---

## 6. Technical Debt Catalog

### Tier 1: Structural (high effort, high impact)

| Issue | Location | Impact |
|-------|----------|--------|
| **14.6k-line monolith** | main.py | Every change risks side effects across 47 sections |
| **44 global state dicts** | main.py | Implicit mutation, impossible to trace without grep |
| **256+ inline SQL** | main.py | Schema changes require editing 40+ statements |
| **1,014-line main()** | token-api-tui.py | Unmaintainable, untestable |
| **Zero main.py tests** | tests/ | No safety net for the core server |

### Tier 2: Maintainability (medium effort, medium impact)

| Issue | Location | Impact |
|-------|----------|--------|
| **State machines as if-else** | main.py (phone, desktop, enforcement) | Invalid transitions possible |
| **Duplicated DB queries** | main.py (10+ identical SELECT patterns) | Schema drift risk |
| **Hardcoded IPs/paths** | token-satellite.py | Environment-specific, no config file |
| **No background worker supervision** | main.py lifespan() | Crash = silent failure |
| **TUI terminal interaction pattern** | token-api-tui.py (6x duplication) | Copy-paste bugs |

### Tier 3: Cleanup (low effort, low impact)

| Issue | Location | Impact |
|-------|----------|--------|
| `_sync_*` wrappers | main.py:4703+ | Unreachable |
| `fleet_dispatch_poc.py` | root | POC that never shipped |
| `timer-debug-log.py` | root | Debug artifact |
| `AGENTS.md.bak` / `AGENTS.md.new` | root | Stale backup files |
| `60min` empty file | root | Unknown purpose |

---

## 7. Recommended Refactoring Order

Based on the 2026-04-06 handoff doc and this review:

1. **Schema unification** — Already in progress (db_schema.py). Verify on Mac, then remove dual init paths from main.py.

2. **Extract query helpers** — `count_active_instances()`, `get_instance_by_id()`, `update_instance_field()`. Eliminates 40+ inline SQL patterns. Low risk, high payoff.

3. **Extract state containers** — Replace 44 global dicts with TypedDicts or dataclasses. Enables IDE navigation and type checking without full ORM.

4. **Split main.py by section** — The 47 section headers are already a roadmap:
   - `routes/instances.py` (1,366 lines)
   - `routes/phone.py` (767 lines)
   - `routes/timer.py` (433 lines)
   - `routes/tts.py` (1,579 lines)
   - `routes/hooks.py` (1,206 lines)
   - `routes/session_docs.py` (805 lines)
   - `routes/aspirant.py` (1,164 lines)
   - etc.

5. **Session doc resolution** — Design exists (2026-04-06). Needs `resolve_or_create_session_doc()` wired into `handle_session_start()`.

6. **TUI refactor** — Extract `main()` into key_listener, action_dispatcher, refresh_loop. Extract terminal interaction pattern into reusable helper.

---

## 8. What's Actually Good

This review catalogs debt, not merit. For the record:

- **Hub-and-spoke architecture** is sound — agents communicate via HTTP, failure is isolated
- **timer.py** is a model module: pure logic, zero I/O, fully tested, clean API
- **cron_engine.py** is well-structured with proper async patterns and test coverage
- **token-satellite.py** is cleanly decoupled — could be deployed independently
- **Named agents** (custodes, corax, alpharius) follow Unix philosophy: small, focused, communicate via pipes (HTTP)
- **Section headers** in main.py are consistent and navigable
- **All SQL is parameterized** — no injection vectors despite 256+ queries
- **The system runs**. 14.6k lines, 184 endpoints, multi-device orchestration, and it works. The sprawl is the feature.

---

*Ave Imperator. The cathedral has been surveyed. Its foundations are sound; its hallways need numbering.*
