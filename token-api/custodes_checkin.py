#!/usr/bin/env python3
"""Custodes Phase 1 — Contextual check-in script.
Reads Emperor state, synthesizes an observation, posts to Discord #briefing,
and appends to the daily note.
"""
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime

BASE = "http://localhost:7777"


def _get(path: str) -> dict | list:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as resp:
        return json.loads(resp.read())


def get_state():
    return _get("/api/state")


def get_instances():
    data = _get("/api/instances")
    return data if isinstance(data, list) else data.get("instances", [])


def active_instances(instances):
    return [
        i for i in instances
        if i.get("status") in ("active", "processing", "idle")
        and not i.get("is_subagent")
    ]


def get_session_doc(instance):
    """Try to read session doc for an instance. Returns summary or None."""
    doc_id = instance.get("session_doc_id")
    if not doc_id:
        return None
    try:
        result = subprocess.run(
            ["obsidian", "vault=Imperium-ENV", "read", f'path="Terra/Sessions/{doc_id}.md"'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0 and result.stdout.strip():
            # Return first non-empty, non-frontmatter lines as summary
            lines = [l for l in result.stdout.splitlines() if l.strip() and not l.startswith("---")]
            return " ".join(lines[:3])
    except Exception:
        pass
    return None


def get_daily_thread_id(today: str) -> str | None:
    """Extract thread ID from today's daily note if Phase 0 stored one."""
    try:
        result = subprocess.run(
            ["obsidian", "vault=Imperium-ENV", "read", f'path="Terra/Journal/Daily/{today}.md"'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            # Look for pattern: "Thread: ... (ID <snowflake>)"
            m = re.search(r"Thread.*?ID\s+(\d{17,20})", result.stdout)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


def build_observation(state: dict, active: list, session_summary: str | None) -> str:
    mode = state.get("timer_mode", "unknown")
    work_mode = state.get("work_mode", "unknown")
    break_min = int(state.get("break_time_remaining_min", 0))
    work_min = int(state.get("work_time_earned_min", 0))
    location = state.get("location", "unknown")
    habits = state.get("habits_today", {})
    habits_done = habits.get("completed", 0)
    habits_total = habits.get("total", 0)

    n = len(active)
    if n == 0:
        instance_line = "No active instances."
    elif n == 1:
        name = active[0].get("tab_name", "unnamed")
        instance_line = f"1 active instance: **{name}**."
    else:
        names = ", ".join(f"**{i['tab_name']}**" for i in active[:4])
        suffix = f" (+{n-4} more)" if n > 4 else ""
        instance_line = f"{n} active instances: {names}{suffix}."

    lines = [
        f"Mode: {mode} | Work: {work_mode} | Location: {location}",
        f"Break bank: {break_min}m | Work earned: {work_min}m | Habits: {habits_done}/{habits_total}",
        instance_line,
    ]
    if session_summary:
        lines.append(f"Context: {session_summary}")

    return "\n".join(lines)


def post_to_discord(observation: str, thread_id: str | None, today: str):
    if thread_id:
        result = subprocess.run(
            ["discord", "thread", "send", thread_id, observation],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            print(f"  Posted to thread {thread_id}")
            return
        print(f"  Thread post failed ({result.returncode}), falling back to #briefing")

    # No thread or thread failed — post header + observation to #briefing
    header = f"**Custodes — {today}**"
    subprocess.run(["discord", "send", "briefing", f"{header}\n{observation}"],
                   capture_output=True, text=True, timeout=15)
    print("  Posted to #briefing")


def append_to_daily_note(observation: str, today: str, timestamp: str):
    note_path = f"Terra/Journal/Daily/{today}.md"
    entry = f"\n## Custodes — {timestamp}\n\n{observation}\n"
    result = subprocess.run(
        ["obsidian", "vault=Imperium-ENV", "append", f'path="{note_path}"', f'content="{entry}"'],
        capture_output=True, text=True, timeout=10
    )
    if result.returncode == 0:
        print(f"  Appended to {note_path}")
    else:
        print(f"  Warning: daily note append failed: {result.stderr.strip()}")


def main():
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    timestamp = now.strftime("%H:%M")

    # 1. Load state
    try:
        state = get_state()
        instances = get_instances()
    except Exception as e:
        print(f"ERROR: Could not reach token-api: {e}", file=sys.stderr)
        sys.exit(1)

    active = active_instances(instances)
    print(f"State loaded: {len(active)} active instances")

    # 2. Try session doc from most recent active instance
    session_summary = None
    for inst in active:
        session_summary = get_session_doc(inst)
        if session_summary:
            break

    # 3. Build observation
    observation = build_observation(state, active, session_summary)
    print(f"\nObservation:\n{observation}\n")

    # 4. Find thread from daily note
    thread_id = get_daily_thread_id(today)
    if thread_id:
        print(f"Found thread ID: {thread_id}")

    # 5. Post to Discord
    post_to_discord(observation, thread_id, today)

    # 6. Append to daily note
    append_to_daily_note(observation, today, timestamp)

    print("\nDone.")


if __name__ == "__main__":
    main()
