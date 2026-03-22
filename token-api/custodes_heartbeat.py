#!/usr/bin/env python3
"""Custodes Phase 8 — Token-API state commentary over habit spam.
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
import time as _time
import urllib.request
from pathlib import Path

BASE = "http://localhost:7777"
BRIEFING_CHANNEL = "briefing"
EMPEROR_DISCORD_ID = "229461055628115968"  # For @mentions that cut through thread suppression


def _get(path: str) -> dict | list:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as resp:
        return json.loads(resp.read())


def collect_state() -> tuple[dict, list, list]:
    timer = _get("/api/timer")
    instances = _get("/api/instances")
    if isinstance(instances, dict):
        instances = instances.get("instances", [])
    try:
        cron_data = _get("/api/cron/jobs")
        cron_jobs = cron_data if isinstance(cron_data, list) else cron_data.get("jobs", [])
    except Exception:
        cron_jobs = []
    return timer, instances, cron_jobs


def extract_metrics(timer: dict, instances: list, cron_jobs: list) -> dict:
    effective_mode = timer.get("current_mode", "unknown").upper()
    break_minutes = int(timer.get("break_balance_ms", 0) / 60000)
    work_minutes = int(timer.get("work_time_ms", timer.get("work_ms", 0)) / 60000)
    alive = [i for i in instances if i.get("status") in ("active", "processing", "idle") and not i.get("is_subagent")]
    cron_count = sum(1 for i in alive if i.get("origin_type") == "cron")
    manual_count = len(alive) - cron_count
    processing_count = sum(1 for i in instances if i.get("is_processing") == 1)

    # Cron job fleet stats
    active_cron_jobs = [j for j in cron_jobs if j.get("status") == "running"]
    recent_victories = _get_recent_cron_victories(cron_jobs)

    return {
        "effective_mode": effective_mode,
        "break_minutes": break_minutes,
        "work_minutes": work_minutes,
        "active_count": manual_count,       # Emperor's manual instances only
        "cron_count": cron_count,            # Mechanicus cron workers (instances)
        "processing_count": processing_count,
        "manual_mode": (timer.get("manual_mode") or "").upper(),
        "active_cron_jobs": len(active_cron_jobs),
        "recent_victories": recent_victories,
    }


def _get_recent_cron_victories(cron_jobs: list) -> list[str]:
    """Return names of cron jobs that completed with a victory signal in the last 15 minutes."""
    now = _time.time()
    fifteen_min_ago = now - 900
    victories = []
    for job in cron_jobs:
        last_run = job.get("last_run_at") or job.get("last_run")
        victory_conditions = job.get("victory_conditions")
        if not last_run or not victory_conditions:
            continue
        try:
            if isinstance(last_run, str):
                lr_dt = datetime.datetime.fromisoformat(last_run.replace("Z", "+00:00"))
                lr_ts = lr_dt.timestamp()
            else:
                lr_ts = float(last_run)
            if lr_ts >= fifteen_min_ago:
                victories.append(job.get("name") or job.get("id", "unknown"))
        except Exception:
            continue
    return victories


# ── Idle tracking ──────────────────────────────────────────────────────────────

IDLE_START_FLAG = "/tmp/custodes_idle_start_{date}.txt"


def check_extended_idle(metrics: dict) -> str | None:
    """Return a message if IDLE mode has persisted continuously for >45 minutes."""
    mode = metrics.get("effective_mode", "")
    today = datetime.date.today().isoformat().replace("-", "")
    start_flag = Path(IDLE_START_FLAG.format(date=today))

    if mode != "IDLE":
        start_flag.unlink(missing_ok=True)
        return None

    now = _time.time()
    if not start_flag.exists():
        try:
            start_flag.write_text(str(now))
        except Exception:
            pass
        return None

    try:
        idle_started_at = float(start_flag.read_text().strip())
    except Exception:
        try:
            start_flag.write_text(str(now))
        except Exception:
            pass
        return None

    elapsed_min = (now - idle_started_at) / 60
    if elapsed_min >= 45:
        return f"Extended idle: IDLE for {int(elapsed_min)} minutes continuously."
    return None


# ── Mode oscillation tracking ──────────────────────────────────────────────────

MODE_HISTORY_FILE = "/tmp/custodes_mode_history_{date}.json"


def track_and_check_oscillation(mode: str) -> str | None:
    """Track mode snapshots. Return message if >4 WORKING↔IDLE switches in last hour."""
    today = datetime.date.today().isoformat().replace("-", "")
    history_file = Path(MODE_HISTORY_FILE.format(date=today))

    now = _time.time()
    history = []
    if history_file.exists():
        try:
            history = json.loads(history_file.read_text())
        except Exception:
            history = []

    history.append({"mode": mode, "ts": now})
    one_hour_ago = now - 3600
    history = [h for h in history if h["ts"] >= one_hour_ago]

    try:
        history_file.write_text(json.dumps(history))
    except Exception:
        pass

    # Count WORKING↔IDLE transitions
    transitions = 0
    prev_mode = None
    for entry in history:
        m = entry["mode"]
        if m in ("WORKING", "IDLE"):
            if prev_mode and prev_mode != m:
                transitions += 1
            prev_mode = m

    if transitions > 4:
        return f"Mode oscillation: {transitions} WORKING↔IDLE switches in last hour."
    return None


# ── Guardsman evaluation ───────────────────────────────────────────────────────

def evaluate_with_guardsman(metrics: dict, extra_flags: dict) -> bool:
    """Guardsman PASS/FAIL based on state-commentary conditions (not habits).

    PASS if any of:
    - break_balance > 60min (deep break debt)
    - mode is IDLE and continuous idle >45min
    - processing_instances > 0 (active work with session docs)
    - fleet_victory detected (cron job just completed with victory)
    - mode oscillation >4 switches in last hour
    - mode is DISTRACTED
    """
    summary = (
        f"mode={metrics['effective_mode']}, "
        f"break_balance={metrics['break_minutes']}min, "
        f"work_time={metrics['work_minutes']}min, "
        f"emperor_instances={metrics['active_count']}, "
        f"cron_workers={metrics['cron_count']}, "
        f"processing={metrics['processing_count']}, "
        f"extended_idle={extra_flags.get('extended_idle', False)}, "
        f"fleet_victory={bool(metrics['recent_victories'])}, "
        f"mode_oscillation={extra_flags.get('oscillation', False)}"
    )
    assertion = (
        "PASS if: break_balance > 60min, "
        "OR mode is DISTRACTED, "
        "OR extended_idle is True, "
        "OR processing > 0, "
        "OR fleet_victory is True, "
        "OR mode_oscillation is True. "
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
    session_id = f"custodes-obs-{int(_time.time())}"
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
    work_min = metrics.get("work_minutes", 0)
    active = metrics["active_count"]      # Emperor's manual instances
    cron = metrics.get("cron_count", 0)
    processing = metrics.get("processing_count", 0)
    victories = metrics.get("recent_victories", [])
    active_cron_jobs = metrics.get("active_cron_jobs", 0)

    if mode == "DISTRACTED":
        return "Distracted mode detected — intervention may be warranted."
    if active == 0 and cron == 0:
        return "No active instances — Emperor may have stepped away."
    if active == 0 and cron > 0:
        return f"Emperor is offline. {cron} Mechanicus worker(s) running autonomously."

    cron_suffix = f" ({cron} cron)" if cron > 0 else ""
    timer_suffix = f" | work={work_min}m, break={break_min:+d}m"

    if victories:
        return f"Fleet victory: {', '.join(victories[:2])}.{timer_suffix}"
    if break_min > 60 and active > 0:
        return f"Break account is {break_min}m with {active} manual instance(s){cron_suffix} running — consider clearing the queue.{timer_suffix}"
    if processing > 0:
        return f"{processing} instance(s) actively processing in {mode} mode{cron_suffix}.{timer_suffix}"
    return f"{active} manual instance(s) in {mode} mode{cron_suffix}.{timer_suffix}"


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


BREAK_START_FLAG = "/tmp/custodes_break_start_{date}.txt"
BREAK_OBS_FLAG = "/tmp/custodes_break_obs_{date}.txt"


def check_break_observation(metrics: dict) -> str | None:
    """Post to daily thread when Emperor is in manual BREAK for >15 minutes.

    Uses a two-flag pattern:
    - break_start flag: records when manual BREAK was first detected
    - break_obs flag: debounce — prevents re-fire within 45 min of last post

    Clears break_start flag when break ends (auto-reset on next entry).
    """
    today = datetime.date.today().isoformat().replace("-", "")
    mode = metrics.get("effective_mode", "")
    manual_mode = metrics.get("manual_mode", "")

    start_flag = Path(BREAK_START_FLAG.format(date=today))
    obs_flag = Path(BREAK_OBS_FLAG.format(date=today))

    in_manual_break = mode == "BREAK" and manual_mode == "BREAK"

    if not in_manual_break:
        # Break ended — clear start flag so next break detects fresh
        start_flag.unlink(missing_ok=True)
        return None

    now = _time.time()

    # Record break start if this is the first time we see manual BREAK
    if not start_flag.exists():
        try:
            start_flag.write_text(str(now))
        except Exception:
            pass
        return None  # Just entered break — wait for duration threshold

    # Check elapsed time since break started
    try:
        break_started_at = float(start_flag.read_text().strip())
    except Exception:
        # Corrupt flag — reset
        try:
            start_flag.write_text(str(now))
        except Exception:
            pass
        return None

    elapsed_min = (now - break_started_at) / 60
    if elapsed_min < 15:
        return None

    # 45-minute debounce — don't re-fire within same break window
    if obs_flag.exists():
        try:
            if now - obs_flag.stat().st_mtime < 2700:  # 45 min
                return None
        except Exception:
            pass

    # Build message with session context
    file_path, title = get_recent_session_doc()
    ctx = get_session_context(file_path) if file_path else None

    if title:
        excerpt = (ctx[:120].replace("\n", " ").strip() + "…") if ctx else ""
        msg = f"You're on break. Last active: *{title}*" + (f" — {excerpt}" if excerpt else ".")
    else:
        msg = f"You're on break (manual BREAK, {int(elapsed_min)}m)."

    # Write debounce flag
    try:
        obs_flag.write_text(datetime.datetime.now().isoformat())
    except Exception:
        pass

    return msg


def check_instance_zero(metrics: dict, instances: list) -> tuple[str | None, str | None]:
    """Return (message, channel) if Emperor's manual instance count crossed zero boundary.
    Cron workers don't count — the Emperor is offline if no manual instances are running."""
    FLAG = Path("/tmp/custodes-zero-sent")
    active_count = metrics.get("active_count", -1)  # manual only
    cron_count = metrics.get("cron_count", 0)

    if active_count < 0:
        return None, None  # metrics unavailable

    if active_count == 0:
        if not FLAG.exists():
            FLAG.touch()
            cron_note = f" {cron_count} Mechanicus worker(s) continue autonomously." if cron_count > 0 else ""
            return f"Emperor has gone offline.{cron_note}", "fleet"
        return None, None  # already sent, suppress

    # Manual instances are running — clear flag if set
    if FLAG.exists():
        FLAG.unlink()
        cron_note = f" ({cron_count} cron)" if cron_count > 0 else ""
        return f"Emperor is back online. {active_count} manual instance(s){cron_note}.", "fleet"
    return None, None


def check_morning_greeting(metrics: dict) -> str | None:
    """Post a morning greeting on the first heartbeat of the day (9am+ Phoenix time).
    Creates the daily thread as a side effect. Only fires once per day via flag file.
    """
    phoenix_now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) - datetime.timedelta(hours=7)

    # Only fire from 9am onward (alarm 8:30, work starts 9)
    if phoenix_now.hour < 9:
        return None

    today = datetime.date.today().isoformat()
    flag_file = Path(f"/tmp/custodes_morning_greeting_{today.replace('-', '')}.txt")
    if flag_file.exists():
        return None

    # This is the first heartbeat of the day — create the daily thread
    get_or_create_daily_thread()

    # Build a brief morning status
    active = metrics.get("active_count", 0)
    cron = metrics.get("cron_count", 0)
    mode = metrics.get("effective_mode", "unknown")

    # Check overnight fleet activity
    try:
        fleet_state = _get("/api/fleet/state")
        last_fg = fleet_state.get("last_fg_run", "unknown")
        notes = fleet_state.get("notes", [])
        fleet_note = notes[0][:120] if notes else "No fleet notes."
    except Exception:
        last_fg = "unknown"
        fleet_note = "Fleet state unavailable."

    flag_file.touch()

    greeting = (
        f"Good morning, Emperor. Custodes online — {phoenix_now.strftime('%H:%M')} MST.\n\n"
        f"Fleet: {active} manual + {cron} cron instances. Mode: {mode}.\n"
        f"Last FG run: {last_fg[:16] if last_fg != 'unknown' else 'unknown'}\n"
    )
    if fleet_note:
        greeting += f"\nFleet note: {fleet_note}"

    return greeting


def log_to_daily_note(summary: str):
    today = datetime.date.today().isoformat()
    note_path = f"/Volumes/Imperium/Imperium-ENV/Terra/Journal/Daily/{today}.md"
    timestamp = datetime.datetime.now().strftime("%H:%M")
    line = f"\n- [{timestamp}] Custodes heartbeat: {summary} — ROUTINE\n"
    try:
        with open(note_path, "a") as f:
            f.write(line)
        print(f"  Logged to {note_path}")
    except OSError as e:
        print(f"  Warning: could not write daily note: {e}", file=sys.stderr)


def emperor_is_live(metrics: dict) -> bool:
    """Emperor is confirmed live if they have at least one manual (non-cron) instance active."""
    return metrics.get("active_count", 0) > 0


def main():
    # 1. Collect state
    try:
        timer, instances, cron_jobs = collect_state()
    except Exception as e:
        print(f"ERROR: Could not reach token-api: {e}", file=sys.stderr)
        sys.exit(1)

    metrics = extract_metrics(timer, instances, cron_jobs)
    summary = (
        f"mode={metrics['effective_mode']}, "
        f"break_balance={metrics['break_minutes']}min, "
        f"work_time={metrics['work_minutes']}min, "
        f"active_instances={metrics['active_count']}, "
        f"cron_workers={metrics.get('cron_count', 0)}, "
        f"processing={metrics['processing_count']}, "
        f"active_cron_jobs={metrics.get('active_cron_jobs', 0)}"
    )
    print(f"State: {summary}")

    is_live = emperor_is_live(metrics)
    if not is_live:
        print("  Emperor not live (no manual instances). Suppressing all commentary.")
        log_to_daily_note(summary)
        print("Done.")
        return

    # 1b. Morning greeting — first heartbeat of the day, creates daily thread
    morning = check_morning_greeting(metrics)
    if morning:
        print(f"  Morning greeting: posting to daily thread")
        send_discord_thread(f"Custodes: {morning}")
        send_discord(
            f"Good morning, Emperor. Daily thread is live. Overnight report in #fleet.",
            channel=BRIEFING_CHANNEL,
        )

    # 2. Break nudge — unconditional, fires independently of INTERESTING evaluation
    nudge = check_break_nudge(metrics)
    if nudge:
        print(f"  Break nudge: {nudge}")
        send_discord(f"Custodes: {nudge}", channel="fleet")

    # 3. Break mode observation — fires to daily thread when in manual BREAK >15 min
    break_obs = check_break_observation(metrics)
    if break_obs:
        print(f"  Break observation: {break_obs}")
        send_discord_thread(f"Custodes: {break_obs}")

    # 4. Instance-zero check — deduped via flag file, routes to #fleet
    zero_msg, zero_ch = check_instance_zero(metrics, instances)
    if zero_msg:
        print(f"  Instance zero: {zero_msg}")
        if metrics.get("active_count", 0) == 0:
            file_path, title = get_recent_session_doc()
            if title:
                ctx = get_session_context(file_path)
                excerpt = (ctx[:150].replace("\n", " ").strip() + "…") if ctx else ""
                zero_msg += f"\nLast active: *{title}*" + (f" — {excerpt}" if excerpt else "")
                print(f"  Context: {title}")
        else:
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

    # 5. State-commentary checks — compute extra flags for guardsman
    mode = metrics["effective_mode"]

    # Extended idle: >45 minutes in IDLE
    idle_msg = check_extended_idle(metrics)
    if idle_msg:
        print(f"  Extended idle: {idle_msg}")
        send_discord_thread(f"Custodes: {idle_msg}")

    # Mode oscillation: WORKING↔IDLE >4 switches/hr
    oscillation_msg = track_and_check_oscillation(mode)
    if oscillation_msg:
        print(f"  Mode oscillation: {oscillation_msg}")
        send_discord_thread(f"Custodes: {oscillation_msg}")

    # Fleet victories: cron job completed with victory signal in last 15 min
    if metrics["recent_victories"]:
        victory_names = ", ".join(metrics["recent_victories"][:3])
        victory_msg = f"Fleet victory: {victory_names} completed."
        print(f"  {victory_msg}")
        send_discord_thread(f"Custodes: {victory_msg}")

    extra_flags = {
        "extended_idle": idle_msg is not None,
        "oscillation": oscillation_msg is not None,
    }

    # 6. Evaluate via guardsman
    is_interesting = evaluate_with_guardsman(metrics, extra_flags)
    print(f"Decision: {'INTERESTING' if is_interesting else 'ROUTINE'}")

    # 7. Act
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
