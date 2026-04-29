#!/usr/bin/env python3
"""Log TUI timer prediction vs authoritative API state every 0.5s for pattern analysis."""

import json
import time
import sqlite3
import urllib.request

DB_PATH = "/home/token/.claude/agents.db"
API_URL = "http://localhost:7777"

# Mirror the TUI's break rate table
BREAK_RATE_PER_SEC = {
    "work_silence": 0.5,
    "work_music": 0.25,
    "work_video": -0.25,
    "work_gaming": -0.5,
    "work_gym": 0.75,
    "gym": 1.0,
    "break": -1.0,
    "pause": 0.0,
}

SYNC_INTERVAL = 5  # same as TUI


def read_db():
    """Read timer state from SQLite (same as TUI does)."""
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT state_json FROM timer_state WHERE id = 1").fetchone()
        conn.close()
        if row:
            state = json.loads(row[0])
            bal = state.get("break_balance_ms")
            if bal is None:
                bal = state.get("accumulated_break_ms", 0) - state.get("break_backlog_ms", 0)
            return {
                "break_ms": max(0, bal),
                "backlog_ms": abs(min(0, bal)),
                "mode": state.get("current_mode", "work_silence"),
            }
    except Exception as e:
        return {"break_ms": 0, "backlog_ms": 0, "mode": "?", "error": str(e)}


def read_api():
    """Read live timer state from API (ground truth)."""
    try:
        req = urllib.request.Request(f"{API_URL}/api/timer")
        with urllib.request.urlopen(req, timeout=1) as resp:
            data = json.loads(resp.read().decode())
            bal_ms = data.get("break_balance_ms")
            if bal_ms is None:
                bal_ms = data.get("accumulated_break_ms", 0) - data.get("break_backlog_ms", 0)
            return {
                "break_ms": max(0, bal_ms),
                "backlog_ms": abs(min(0, bal_ms)),
                "mode": data.get("current_mode", "?"),
            }
    except Exception as e:
        return {"break_ms": 0, "backlog_ms": 0, "mode": "?", "error": str(e)}


def predict(sync_state, elapsed_s):
    """Replicate TUI's _predict_timer logic."""
    mode = sync_state["mode"]
    rate = BREAK_RATE_PER_SEC.get(mode, 0.0)
    delta = rate * elapsed_s

    break_ms = sync_state["break_ms"] + delta * 1000
    backlog_ms = sync_state["backlog_ms"]

    if break_ms < 0:
        backlog_ms += abs(break_ms)
        break_ms = 0
    elif backlog_ms > 0 and delta > 0:
        if delta * 1000 >= backlog_ms:
            break_ms = sync_state["break_ms"] + (delta * 1000 - backlog_ms)
            backlog_ms = 0
        else:
            backlog_ms -= delta * 1000
            break_ms = 0

    return int(break_ms), int(backlog_ms)


def fmt(ms):
    """Format ms as seconds with 1 decimal."""
    return f"{ms / 1000:.1f}s"


def main():
    import sys

    def p(s):
        print(s, flush=True)

    p("tick | db_break   | api_break  | predicted  | db_drift | api_drift | mode")
    p("-----+-----------+------------+------------+----------+-----------+------")

    sync_state = read_api()  # sync from API like the new TUI does
    sync_time = time.monotonic()
    last_sync = sync_time
    tick = 0

    while True:
        now = time.monotonic()
        elapsed = now - sync_time

        # Resync from API every SYNC_INTERVAL (same as new TUI)
        if now - last_sync >= SYNC_INTERVAL:
            sync_state = read_api()
            sync_time = now
            last_sync = now
            elapsed = 0

        # Get all three values
        db = read_db()
        api = read_api()
        pred_break, pred_backlog = predict(sync_state, elapsed)

        # Calculate drift
        db_drift = pred_break - db["break_ms"]
        api_drift = pred_break - api["break_ms"]

        is_sync = "SYNC" if elapsed < 0.1 else ""

        p(
            f"{tick:4d} | "
            f"{fmt(db['break_ms']):>9s} | "
            f"{fmt(api['break_ms']):>9s} | "
            f"{fmt(pred_break):>9s} | "
            f"{db_drift:>+7.0f}ms | "
            f"{api_drift:>+7.0f}ms | "
            f"{api['mode']} {is_sync}"
        )

        tick += 1
        time.sleep(0.5)


if __name__ == "__main__":
    main()
