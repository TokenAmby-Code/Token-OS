#!/usr/bin/env python3
"""Custodes Phase 3 — MiniMax heartbeat with session context.
Polls Token-API state, evaluates via guardsman whether the state is interesting.
INTERESTING: reads active session doc, generates contextual observation, posts to #briefing.
ROUTINE: appends quietly to daily note.
"""
import datetime
import json
import os
import subprocess
import sys
import urllib.request

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


def send_discord(message: str):
    result = subprocess.run(
        ["discord", "send", BRIEFING_CHANNEL, "--bot", "custodes", message],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode == 0:
        print("  Posted to #briefing via custodes bot")
    else:
        print(f"  Discord send failed: {result.stderr.strip()}")


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

    # 2. Evaluate
    is_interesting = evaluate_with_guardsman(metrics)
    print(f"Decision: {'INTERESTING' if is_interesting else 'ROUTINE'}")

    # 3. Act
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
