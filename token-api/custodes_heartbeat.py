#!/usr/bin/env python3
"""Custodes Phase 7 — Daily thread in #briefing for interesting observations.
Polls Token-API state, evaluates via guardsman whether the state is interesting.
INTERESTING: reads session docs for up to 2 processing instances, enriches observation
  with "Active work: <topic> (<project>), ..." suffix. Posts to daily thread in #briefing.
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


def get_recent_session_doc() -> tuple[str | None, str | None]:
    """Return (file_path, title) of the most recently active non-subagent instance with a session doc.

    Unlike get_active_session_doc(), includes stopped instances — used for instance_zero context
    when no active instances exist.
    """
    result = subprocess.run(
        [
            "agents-db", "--json", "query",
            "SELECT sd.file_path, sd.title FROM claude_instances ci "
            "JOIN session_documents sd ON ci.session_doc_id = sd.id "
            "WHERE ci.is_subagent=0 AND ci.session_doc_id IS NOT NULL "
            "ORDER BY ci.last_activity DESC LIMIT 1",
        ],
        capture_output=True, text=True, timeout=10,
    )
    try:
        rows = json.loads(result.stdout)
        if rows:
            return rows[0].get("file_path"), rows[0].get("title")
        return None, None
    except Exception:
        return None, None


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


def extract_session_topic(instance: dict) -> str:
    """Return short topic string for an instance: session doc title, or tab_name fallback.

    Queries session_documents table directly — no file I/O needed since title is stored in DB.
    Falls back gracefully to tab_name if no doc linked or query fails.
    """
    tab_name = instance.get("tab_name") or instance.get("id", "unknown")[:8]
    doc_id = instance.get("session_doc_id")
    if not doc_id:
        return tab_name
    try:
        result = subprocess.run(
            ["agents-db", "--json", "query",
             f"SELECT title FROM session_documents WHERE id={int(doc_id)}"],
            capture_output=True, text=True, timeout=5,
        )
        rows = json.loads(result.stdout)
        title = rows[0].get("title") if rows else None
        if title:
            return title
    except Exception:
        pass
    return tab_name


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


def get_or_create_daily_thread() -> str | None:
    """Return today's briefing thread ID, creating it if needed. Returns None on failure."""
    today = datetime.date.today().strftime("%Y-%m-%d")
    cache_path = Path(f"/tmp/custodes_thread_{today.replace('-', '')}.txt")

    if cache_path.exists():
        thread_id = cache_path.read_text().strip()
        if thread_id:
            return thread_id

    thread_name = f"Custodes — {today}"
    result = subprocess.run(
        ["discord", "thread", "create", BRIEFING_CHANNEL, thread_name, "--bot", "custodes"],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        print(f"  Thread create failed: {result.stderr.strip()}")
        return None

    # Parse thread ID from stdout (e.g. "Thread created: 123456789")
    output = result.stdout.strip()
    print(f"  Thread created: {output}")
    thread_id = None
    for part in output.split():
        if part.isdigit() and len(part) > 10:
            thread_id = part
            break

    if not thread_id:
        print(f"  Could not parse thread ID from: {output!r}")
        return None

    cache_path.write_text(thread_id)
    return thread_id


def send_discord_thread(message: str):
    """Post to today's daily briefing thread, falling back to main channel."""
    thread_id = get_or_create_daily_thread()
    if thread_id:
        result = subprocess.run(
            ["discord", "thread", "send", thread_id, "--bot", "custodes", message],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            print(f"  Posted to daily thread {thread_id} via custodes bot")
            return
        print(f"  Thread send failed: {result.stderr.strip()}, falling back to #{BRIEFING_CHANNEL}")
    send_discord(message)


def check_morning_habits() -> str | None:
    """After 10am Phoenix time: remind if morning habits appear unchecked in daily note.
    Returns a reminder message, or None if not applicable / already reminded today.
    Phoenix is MST = UTC-7 (no DST).
    """
    phoenix_now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=7)

    # Only check between 10am and 2pm Phoenix time
    if not (10 <= phoenix_now.hour < 14):
        return None

    # Only remind once per day
    today = datetime.date.today().isoformat()
    flag_file = Path(f"/tmp/custodes_habit_reminded_{today.replace('-', '')}.txt")
    if flag_file.exists():
        return None

    # Read today's daily note
    note_path = Path(os.path.expanduser(f"~/Token-ENV/Journal/Daily/{today}.md"))
    if not note_path.exists():
        return None

    content = note_path.read_text()

    # Only check if YAML frontmatter exists — conservative, skip if no structure
    if not content.startswith("---"):
        return None

    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    frontmatter = parts[1]

    # Morning habit fields: teeth_brush_morning and breakfast are the core AM indicators
    has_unchecked = (
        "teeth_brush_morning: false" in frontmatter
        or "breakfast: false" in frontmatter
    )
    has_checked = (
        "teeth_brush_morning: true" in frontmatter
        or "breakfast: true" in frontmatter
        or "movement: true" in frontmatter
    )

    if has_unchecked and not has_checked:
        flag_file.touch()
        return (
            f"It's {phoenix_now.strftime('%H:%M')} — morning habits in today's note "
            f"still unchecked. Worth a quick review before the day gets away."
        )

    return None


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

    # 3. Morning habit check — fires to daily thread if habits look unchecked (10am–2pm only)
    habit_reminder = check_morning_habits()
    if habit_reminder:
        print(f"  Habit reminder: {habit_reminder}")
        send_discord_thread(f"Custodes: {habit_reminder}")

    # 4. Instance-zero check — deduped via flag file, routes to #fleet
    zero_msg, zero_ch = check_instance_zero(metrics)
    if zero_msg:
        print(f"  Instance zero: {zero_msg}")
        # Enrich with session context for re-orientation
        if metrics.get("active_count", 0) == 0:
            # No instances — surface what the Emperor was last working on
            file_path, title = get_recent_session_doc()
            if title:
                ctx = get_session_context(file_path)
                excerpt = (ctx[:150].replace("\n", " ").strip() + "…") if ctx else ""
                zero_msg += f"\nLast active: *{title}*" + (f" — {excerpt}" if excerpt else "")
                print(f"  Context: {title}")
        else:
            # Back online — include what the returning instance is working on
            active_non_sub = [
                i for i in instances
                if i.get("status") == "active" and not i.get("is_subagent")
            ]
            if active_non_sub:
                topic = extract_session_topic(active_non_sub[0])
                zero_msg += f" Working on: {topic}."
                print(f"  Context: {topic}")
        send_discord(f"Custodes: {zero_msg}", channel=zero_ch)
        if metrics.get("active_count", 0) == 0:
            log_to_daily_note(summary)
            print("Done.")
            return

    # 5. Evaluate
    is_interesting = evaluate_with_guardsman(metrics)
    print(f"Decision: {'INTERESTING' if is_interesting else 'ROUTINE'}")

    # 6. Act
    if is_interesting:
        session_doc = get_active_session_doc()
        session_ctx = get_session_context(session_doc)
        if session_doc:
            print(f"  Session doc: {session_doc}")
        else:
            print("  No linked session doc found")
        observation = generate_observation(summary, session_ctx)
        print(f"  Observation: {observation}")

        # Build "Active work: <topic> (<project>), ..." from processing instances (up to 2)
        processing = [
            i for i in instances
            if i.get("is_processing") == 1 and not i.get("is_subagent")
        ][:2]
        active_work_suffix = ""
        if processing:
            parts = []
            for inst in processing:
                topic = extract_session_topic(inst)
                working_dir = inst.get("working_dir", "")
                project = os.path.basename(working_dir.rstrip("/")) if working_dir else ""
                if project and project != topic:
                    parts.append(f"{topic} ({project})")
                else:
                    parts.append(topic)
            active_work_suffix = f" Active work: {', '.join(parts)}."
            print(f"  Active work: {active_work_suffix.strip()}")

        send_discord_thread(f"Custodes observes: {observation}{active_work_suffix}")
    else:
        log_to_daily_note(summary)

    print("Done.")


if __name__ == "__main__":
    main()
