#!/usr/bin/env python3
"""Custodes Phase 4 — MiniMax heartbeat with session context + break monitoring.
Polls Token-API state, evaluates via guardsman whether the state is interesting.
INTERESTING: reads active session doc, generates contextual observation, posts to #briefing.
ROUTINE: appends quietly to daily note.
BREAK NUDGE: independently fires when break balance is deeply negative or manual BREAK too long.
"""
import datetime
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE = "http://localhost:7777"
BRIEFING_CHANNEL = "briefing"


def _get(path: str) -> dict | list:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as resp:
        return json.loads(resp.read())


def collect_state() -> tuple[dict, list]:
    timer = _get("/api/timer")
    instances = _get("/api/instances")
    if isinstance(instances, dict):
        instances = instances.get("instances", [])
    return timer, instances


def extract_metrics(timer: dict, instances: list) -> dict:
    effective_mode = timer.get("current_mode", "unknown").upper()
    break_minutes = int(timer.get("break_balance_ms", 0) / 60000)
    active_count = sum(
        1 for i in instances
        if i.get("status") in ("active", "processing", "idle") and not i.get("is_subagent")
    )
    processing_count = sum(1 for i in instances if i.get("is_processing") == 1)
    return {
        "effective_mode": effective_mode,
        "break_minutes": break_minutes,
        "active_count": active_count,
        "processing_count": processing_count,
    }


def evaluate_with_guardsman(metrics: dict) -> bool:
    summary = (
        f"mode={metrics['effective_mode']}, "
        f"break_balance={metrics['break_minutes']}min, "
        f"active_instances={metrics['active_count']}, "
        f"processing={metrics['processing_count']}"
    )
    # Guardsman returns PASS/FAIL. PASS = state matches interesting criteria.
    assertion = (
        "PASS if: break_balance > 60min AND active_instances > 0, "
        "OR active_instances == 0, "
        "OR mode is DISTRACTED. "
        "Otherwise FAIL."
    )
    result = subprocess.run(
        ["guardsman", f"echo '{summary}' | {assertion}"],
        capture_output=True, text=True, timeout=30
    )
    output = result.stdout.strip()
    print(f"  guardsman: {output}")
    return output.upper().startswith("PASS")


def get_active_session_doc() -> str | None:
    """Return file_path of session doc for the most recently active non-subagent instance."""
    result = subprocess.run(
        [
            "agents-db", "--json", "query",
            "SELECT sd.file_path FROM claude_instances ci "
            "JOIN session_documents sd ON ci.session_doc_id = sd.id "
            "WHERE ci.status='active' AND ci.is_subagent=0 AND ci.session_doc_id IS NOT NULL "
            "ORDER BY ci.last_activity DESC LIMIT 1",
        ],
        capture_output=True, text=True, timeout=10,
    )
    try:
        rows = json.loads(result.stdout)
        return rows[0].get("file_path") if rows else None
    except Exception:
        return None


def get_session_context(file_path: str | None) -> str | None:
    """Read session doc and return last 500 chars of body (after frontmatter)."""
    if not file_path:
        return None
    try:
        with open(os.path.expanduser(file_path)) as f:
            content = f.read()
        parts = content.split("---", 2)
        body = parts[2].strip() if len(parts) >= 3 else content
        return body[-500:] if len(body) > 500 else body or None
    except OSError:
        return None


def generate_observation(summary: str, session_ctx: str | None) -> str:
    """Generate a contextual observation sentence via openclaw (MiniMax free tier).

    Uses `openclaw agent` for freeform text generation — guardsman is PASS/FAIL only.
    Response JSON: .payloads[0].text
    """
    ctx_snippet = (session_ctx or "unavailable")[:500]
    prompt = (
        f"You are Custodes, the watchful AI of the Emperor's forge. "
        f"Write exactly ONE observation sentence synthesizing this system state and session context. "
        f"Be specific and useful. No preamble.\n"
        f"State: {summary}\n"
        f"Session context: {ctx_snippet}"
    )
    import time
    session_id = f"custodes-obs-{int(time.time())}"
    result = subprocess.run(
        ["openclaw", "agent", "--agent", "main", "--session-id", session_id,
         "-m", prompt, "--local", "--json"],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 0:
        try:
            data = json.loads(result.stdout)
            text = data.get("payloads", [{}])[0].get("text", "").strip()
            if text:
                return text.split("\n")[0]
        except Exception:
            pass
    return f"State: {summary}"


def build_comment(metrics: dict) -> str:
    mode = metrics["effective_mode"]
    break_min = metrics["break_minutes"]
    active = metrics["active_count"]

    if mode == "DISTRACTED":
        return "Distracted mode detected — intervention may be warranted."
    if active == 0:
        return "No active instances — Emperor may have stepped away."
    if break_min > 60 and active > 0:
        return f"Break account is {break_min}m with {active} instance(s) still running — consider clearing the queue."
    return f"{active} instance(s) active in {mode} mode."


def send_discord(message: str, channel: str = BRIEFING_CHANNEL):
    result = subprocess.run(
        ["discord", "send", channel, "--bot", "custodes", message],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode == 0:
        print(f"  Posted to #{channel} via custodes bot")
    else:
        print(f"  Discord send failed: {result.stderr.strip()}")


def check_break_nudge(metrics: dict) -> str | None:
    """Return a nudge message if break situation warrants it, else None."""
    mode = metrics.get("effective_mode", "WORKING")
    break_min = metrics.get("break_minutes", 0)

    # Deep debt: more than 60 min in the red — always nudge regardless of mode
    if break_min < -60:
        return f"Break balance is {break_min:.0f} min — significant debt. Consider wrapping up soon."

    # Manual BREAK with meaningful debt — prompt to resume or keep resting
    if mode == "BREAK" and break_min < -30:
        return f"In BREAK mode with {break_min:.0f} min balance. Still recovering or time to resume?"

    return None


def check_instance_zero(metrics: dict) -> tuple[str | None, str | None]:
    """Return (message, channel) if instance count crossed zero boundary, else (None, None)."""
    FLAG = Path("/tmp/custodes-zero-sent")
    active_count = metrics.get("active_count", -1)

    if active_count < 0:
        return None, None  # metrics unavailable

    if active_count == 0:
        if not FLAG.exists():
            FLAG.touch()
            return "Forge is silent — no active Claude instances. Emperor has gone offline.", "fleet"
        return None, None  # already sent, suppress

    # Instances are running — clear flag if set
    if FLAG.exists():
        FLAG.unlink()
        return f"Emperor is back online. {active_count} active instance(s).", "fleet"
    return None, None


def log_to_daily_note(summary: str):
    today = datetime.date.today().isoformat()
    note_path = os.path.expanduser(
        f"~/Token-ENV/Journal/Daily/{today}.md"
    )
    timestamp = datetime.datetime.now().strftime("%H:%M")
    line = f"\n- [{timestamp}] Custodes heartbeat: {summary} — ROUTINE\n"
    try:
        with open(note_path, "a") as f:
            f.write(line)
        print(f"  Logged to {note_path}")
    except OSError as e:
        print(f"  Warning: could not write daily note: {e}", file=sys.stderr)


def main():
    # 1. Collect state
    try:
        timer, instances = collect_state()
    except Exception as e:
        print(f"ERROR: Could not reach token-api: {e}", file=sys.stderr)
        sys.exit(1)

    metrics = extract_metrics(timer, instances)
    summary = (
        f"mode={metrics['effective_mode']}, "
        f"break_balance={metrics['break_minutes']}min, "
        f"active_instances={metrics['active_count']}, "
        f"processing={metrics['processing_count']}"
    )
    print(f"State: {summary}")

    # 2. Break nudge — unconditional, fires independently of INTERESTING evaluation
    nudge = check_break_nudge(metrics)
    if nudge:
        print(f"  Break nudge: {nudge}")
        send_discord(f"Custodes: {nudge}", channel="fleet")

    # 3. Instance-zero check — deduped via flag file, routes to #fleet
    zero_msg, zero_ch = check_instance_zero(metrics)
    if zero_msg:
        print(f"  Instance zero: {zero_msg}")
        send_discord(f"Custodes: {zero_msg}", channel=zero_ch)
        if metrics.get("active_count", 0) == 0:
            log_to_daily_note(summary)
            print("Done.")
            return

    # 4. Evaluate
    is_interesting = evaluate_with_guardsman(metrics)
    print(f"Decision: {'INTERESTING' if is_interesting else 'ROUTINE'}")

    # 5. Act
    if is_interesting:
        session_doc = get_active_session_doc()
        session_ctx = get_session_context(session_doc)
        if session_doc:
            print(f"  Session doc: {session_doc}")
        else:
            print("  No linked session doc found")
        observation = generate_observation(summary, session_ctx)
        print(f"  Observation: {observation}")
        send_discord(f"Custodes observes: {observation}")
    else:
        log_to_daily_note(summary)

    print("Done.")


if __name__ == "__main__":
    main()
