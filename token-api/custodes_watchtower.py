#!/usr/bin/env python3
"""Custodes Watchtower — Work Hours Offline Escalation

Detects when the Emperor is offline during work hours (9am-5pm weekdays)
and escalates via a repeating ladder rather than a one-shot flag.

Escalation ladder (per offline period, each level fires once):
  0-30 min   — grace period, silence
  30+ min    — Level 1: DM operator
  60+ min    — Level 2: DM + post to #operations
  90+ min    — Level 3: DM + sound (Glass)
  120+ min   — Level 4: DM + Pavlok beep

Offline = no manual (non-subagent) Claude instances running.
Skips escalation when work_mode is clocked_out, gym, or campus.

State is persisted to a JSON file and resets automatically at midnight.
Designed to run every 5-15 minutes via cron.
"""

import json
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, date
from pathlib import Path

BASE = "http://localhost:7777"
STATE_FILE = Path("/tmp/custodes-watchtower-state.json")

WORK_HOURS_START = 9   # 9 AM
WORK_HOURS_END = 17    # 5 PM (exclusive)

# work_mode values that mean the Emperor is legitimately away
EXEMPT_MODES = {"clocked_out", "gym", "campus"}

ESCALATION_THRESHOLDS = {
    1: 30,   # minutes
    2: 60,
    3: 90,
    4: 120,
}


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text())
        # Reset if date has rolled over
        if data.get("date") != date.today().isoformat():
            return _fresh_state()
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return _fresh_state()


def _fresh_state() -> dict:
    return {
        "date": date.today().isoformat(),
        "offline_since": None,   # ISO timestamp string or None
        "escalation_level": 0,   # highest level fired this period
    }


def _save_state(state: dict):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as e:
        print(f"  WARNING: could not save state: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Token-API queries (defensive — return None on failure)
# ---------------------------------------------------------------------------

def _get(path: str) -> dict | list | None:
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  token-api unreachable ({path}): {e}", file=sys.stderr)
        return None


def _post(path: str, params: dict | None = None) -> dict | None:
    """HTTP POST with optional query-string params. Body is empty."""
    url = f"{BASE}{path}"
    if params:
        from urllib.parse import urlencode
        url += "?" + urlencode(params)
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  token-api POST failed ({path}): {e}", file=sys.stderr)
        return None


def get_work_mode() -> str | None:
    """Return work_mode string, or None if unreachable."""
    data = _get("/api/work-mode")
    if data is None:
        return None
    return data.get("work_mode", "clocked_in")


def get_manual_instance_count() -> int | None:
    """Return count of active non-subagent instances, or None if unreachable."""
    data = _get("/api/instances")
    if data is None:
        return None
    instances = data if isinstance(data, list) else data.get("instances", [])
    manual = [
        i for i in instances
        if i.get("status") in ("active", "processing", "idle")
        and not i.get("is_subagent")
    ]
    return len(manual)


# ---------------------------------------------------------------------------
# Escalation actions
# ---------------------------------------------------------------------------

def _discord_dm(message: str):
    result = subprocess.run(
        ["discord", "dm", message],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        print(f"  discord dm failed: {result.stderr.strip()}", file=sys.stderr)
    else:
        print(f"  DM sent: {message[:80]}")


def _discord_send(channel: str, message: str):
    result = subprocess.run(
        ["discord", "send", channel, message],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        print(f"  discord send failed ({channel}): {result.stderr.strip()}", file=sys.stderr)
    else:
        print(f"  Posted to #{channel}")


def _sound(sound_name: str = "Glass"):
    result = _post("/api/notify/sound", {"sound": sound_name})
    if result is None:
        print(f"  sound ({sound_name}) failed — token-api unreachable", file=sys.stderr)
    else:
        print(f"  Sound: {sound_name}")


def _pavlok_beep(value: int = 50):
    result = _post("/api/pavlok/zap", {"type": "beep", "value": value, "reason": "offline_work_hours"})
    if result is None:
        print(f"  Pavlok beep failed — token-api unreachable", file=sys.stderr)
    else:
        print(f"  Pavlok beep: value={value}")


def fire_escalation(level: int, elapsed_min: int):
    """Execute the action(s) for an escalation level."""
    msg = f"Watchtower: Emperor offline {elapsed_min}m during work hours (level {level}/4)."

    if level == 1:
        print(f"  Escalation L1 ({elapsed_min}m): DM")
        _discord_dm(msg)

    elif level == 2:
        print(f"  Escalation L2 ({elapsed_min}m): DM + #operations")
        _discord_dm(msg)
        _discord_send("operations", f"**Custodes Watchtower** — {msg}")

    elif level == 3:
        print(f"  Escalation L3 ({elapsed_min}m): DM + sound")
        _discord_dm(msg)
        _sound("Glass")

    elif level == 4:
        print(f"  Escalation L4 ({elapsed_min}m): DM + Pavlok beep")
        _discord_dm(msg)
        _pavlok_beep(50)


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def is_work_hours(now: datetime) -> bool:
    """True if now is a weekday between 9 AM and 5 PM."""
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return WORK_HOURS_START <= now.hour < WORK_HOURS_END


def main():
    now = datetime.now()
    print(f"Custodes Watchtower — {now.strftime('%Y-%m-%d %H:%M:%S')}")

    # 1. Only run during work hours
    if not is_work_hours(now):
        print("  Outside work hours. Exiting.")
        # Clear offline tracking when outside work hours (natural end-of-day reset)
        state = _load_state()
        if state.get("offline_since"):
            state["offline_since"] = None
            state["escalation_level"] = 0
            _save_state(state)
        return

    # 2. Check work_mode — skip if legitimately away
    work_mode = get_work_mode()
    if work_mode is None:
        print("  token-api unreachable — skipping escalation check.")
        return

    if work_mode in EXEMPT_MODES:
        print(f"  work_mode={work_mode} — exempt, no escalation.")
        # Also clear offline tracking so we don't pick up duration from before gym/etc.
        state = _load_state()
        if state.get("offline_since"):
            state["offline_since"] = None
            state["escalation_level"] = 0
            _save_state(state)
        return

    # 3. Check instance count
    count = get_manual_instance_count()
    if count is None:
        print("  Could not read instances — skipping escalation check.")
        return

    state = _load_state()

    if count > 0:
        # Emperor is online — reset tracking
        if state.get("offline_since"):
            print(f"  Emperor back online ({count} manual instances). Resetting offline tracking.")
            state["offline_since"] = None
            state["escalation_level"] = 0
            _save_state(state)
        else:
            print(f"  Online ({count} manual instances). No action.")
        return

    # 4. Emperor is offline during work hours
    if not state.get("offline_since"):
        # First detection — start the clock
        state["offline_since"] = now.isoformat()
        state["escalation_level"] = 0
        _save_state(state)
        print(f"  Offline detected. Grace period started (offline_since={state['offline_since']})")
        return

    # Calculate elapsed offline minutes
    try:
        offline_since = datetime.fromisoformat(state["offline_since"])
    except (ValueError, TypeError):
        # Corrupt state — reset
        state["offline_since"] = now.isoformat()
        state["escalation_level"] = 0
        _save_state(state)
        print("  Corrupt offline_since — reset.")
        return

    elapsed_min = int((now - offline_since).total_seconds() / 60)
    current_level = state.get("escalation_level", 0)
    print(f"  Offline {elapsed_min}m (level fired so far: {current_level})")

    # Find next level to fire
    for level in sorted(ESCALATION_THRESHOLDS.keys()):
        threshold = ESCALATION_THRESHOLDS[level]
        if elapsed_min >= threshold and current_level < level:
            fire_escalation(level, elapsed_min)
            state["escalation_level"] = level
            _save_state(state)
            # Fire only one new level per run (allow time between escalations)
            break
    else:
        if elapsed_min < ESCALATION_THRESHOLDS[1]:
            print(f"  Grace period ({elapsed_min}m < {ESCALATION_THRESHOLDS[1]}m). No action yet.")
        else:
            print(f"  All applicable escalations already fired (level {current_level}).")


if __name__ == "__main__":
    main()
