# 2026-05-01 Backlog Work-State TUI Handoff

## Current User Intent

The user is live-testing the backlog enforcement chain and wants live restarts after implementation. They reported:

- Token-API/TUI showed `working` or productive state even when they believed no agent instances were running.
- The TUI needs a more explicit display of what the system thinks is happening.
- Desired TUI header shape: six lines, replacing the compact one-line header.
- Desired key visual: a fraction-like display, active instances over distraction state.
- Desired icons/signals: YouTube, Spotify, Steam/catch-all gaming including mobile Slay the Spire, and Mewgenics.
- Enforcement needs negative-edge visibility: when the distraction app closes, log/show that closure so a later shock is clearly prevented and auditable.

## Live State Observed

Before the latest edits, `GET /api/work-state` returned productive because two DB rows were `idle` with live tmux panes:

- `%0`, `Claude 14:24`, pane command was actually `bash`
- `%10`, `cli-tools`, pane command was actually `bash`

So the typed work-state was over-counting stale registered rows whose panes existed but were no longer running Claude/Codex.

Live `/api/work-state` also showed:

- `desktop_mode = video`
- `phone_app = youtube`
- `timer_mode = multitasking`
- YouTube icon active

This part may be real live distraction state or stale phone/desktop telemetry; verify after restart.

## Code Already Implemented Earlier This Turn

Files touched before compaction:

- `token-api/main.py`
- `token-api/token-api-tui.py`
- `token-api/db_schema.py`
- `token-api/routes/hooks.py`
- `token-api/tests/test_enforcement_core.py`
- `cli-tools/bin/work-action`
- `cli-tools/bin/timer-test`

Expected-ack/backlog chain already implemented and tested:

- Persistent expected-ack table with `fired_levels_json`.
- APScheduler expected-ack jobs use the `golden_throne` jobstore.
- Recovery runs after scheduler startup.
- `backlog_violation` compressed ladder: immediate enforcement, 15-second parry, Pavlok eligibility.
- `/api/work-action` and `work-action` CLI resolve phone/backlog acks and stop cascade.
- Prompt submit routes through work-action behavior.
- Negative-edge app close logging started via `enforcement_negative_edge`.

Tests passed before the final interrupted header work:

```bash
.venv/bin/python -m pytest -q test_timer.py tests/test_enforcement_core.py tests/test_game_turn_events.py
# 105 passed
```

## Latest In-Progress Edits Before Compaction

The latest partial code edits were made after the user requested the six-line fraction display:

### `main.py`

Added or changed:

- Typed `WorkStateResponse`, `AgentRuntime`, and `ActivityIconState`.
- `/api/work-state`.
- `/api/timer` embeds `work_state`.
- `compute_work_state()`.
- `_tmux_pane_rows()`.
- `_tmux_command_is_agent()`.
- `_detect_tmux_agent_panes()`.
- `timer_worker` now uses `compute_work_state()` instead of raw recent-processing SQL.
- `check_window_enforcement()` and phone open logic use `compute_work_state()`.
- `acknowledge_phone_acks()` now includes `backlog_violation` for phone closes.
- `enforcement_negative_edge` is logged on phone close and desktop distraction-to-nondistraction transitions.

Important partial fix:

- `compute_work_state()` was tightened so local idle rows only count if their tmux pane is actually running an agent command. This was intended to fix `%0`/`%10` stale bash panes being counted as productive.

### `token-api-tui.py`

Added or changed:

- `_read_timer()` caches embedded `work_state`.
- `_activity_icon_text()`.
- `_active_distraction_label()`.
- `get_timer_header_text()` was rewritten toward a six-line header:
  1. Mode and break balance
  2. `Work/Dist  active_count / distraction_label`
  3. Icons
  4. Agent counts
  5. Distraction source
  6. Control state
- Compact/narrow status and mobile widget also append activity icons.
- Event panel recognizes `enforcement_negative_edge`.

## Next Steps After Compact

1. Re-run syntax checks first because the last edit was interrupted:

```bash
cd /Volumes/Imperium/Token-OS/token-api
.venv/bin/python -m py_compile main.py token-api-tui.py
```

2. Run focused tests:

```bash
.venv/bin/python -m pytest -q tests/test_enforcement_core.py tests/test_game_turn_events.py
```

3. If green, run broader timer set:

```bash
.venv/bin/python -m pytest -q test_timer.py tests/test_enforcement_core.py tests/test_game_turn_events.py
```

4. Restart live service. User explicitly said always restart for this live work:

```bash
token-restart
```

5. Verify the stale-pane fix:

```bash
curl -sf http://localhost:7777/api/work-state | python3 -m json.tool
tmux list-panes -a -F '#{pane_id}\t#{pane_current_command}\t#{pane_current_path}\t#{window_name}'
```

Expected: panes whose command is `bash`/`zsh` should not count as productive tracked instances just because the DB row has `status=idle`.

6. If continuing the boundary test, set break to zero again:

```bash
timer-test 0
```

7. Check TUI after restart. Header should be six lines and show a readable active/distraction fraction plus icons.

## Micro Summary

We built backlog expected-ack enforcement, work-action parry resolution, and negative-edge close logging. The current active issue is work-state over-counting stale idle DB rows with live bash panes; a partial fix now requires tmux pane command to be an agent before counting local idle rows. Finish by syntax-checking, testing, restarting, and verifying `/api/work-state` no longer reports productivity with only bash/zsh panes.
