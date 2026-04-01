#!/usr/bin/env python3
"""Morning Session Orchestrator.

Triggered by POST /api/morning/start (from phone macro after alarm dismiss).
Gathers context, spawns a Custodes Claude session, sends briefing via TTS,
and enters a follow-up loop that resumes the session with state updates.

Uses the cron engine's session persistence pattern (--session-id / --resume)
but is triggered on-demand rather than by schedule.
"""
import asyncio
import json
import os
import subprocess
import uuid
from datetime import datetime, timedelta
from pathlib import Path

BASE = "http://localhost:7777"
DISCORD_DAEMON = "http://localhost:7779"
VAULT = "Imperium-ENV"
PROMPT_PATH = "~/.claude/prompts/custodes-morning-session.md"
MODEL = "claude-sonnet-4-6"
FOLLOW_UP_INTERVAL_SECONDS = 60   # 1 minute between pings (timer starts when Claude stops)
MORNING_PHASE_MAX_MINUTES = 90   # Auto-end after 90 min
MORNING_DISCORD_WINDOW_MINUTES = 45  # Route Discord messages into session for first 45 min
MORNING_END_MIN_ELAPSED = 20     # Don't allow morning-end before this many minutes (prevents self-trigger)
SESSION_DIR = Path("/tmp/custodes_morning_sessions")


def _get(path: str) -> dict | list | str:
    """GET from Token-API."""
    import urllib.request
    try:
        with urllib.request.urlopen(f"{BASE}{path}", timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def _post(path: str, data: dict = None) -> dict:
    """POST to Token-API."""
    import urllib.request
    body = json.dumps(data or {}).encode()
    req = urllib.request.Request(
        f"{BASE}{path}", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def _obsidian_read(path: str) -> str:
    """Read a note from the default vault (Imperium-ENV)."""
    return _obsidian_read_vault(VAULT, path)


def _obsidian_read_vault(vault: str, path: str) -> str:
    """Read a note from a specific vault."""
    try:
        result = subprocess.run(
            ["obsidian", f"vault={vault}", "read", f"path={path}"],
            capture_output=True, text=True, timeout=15,
        )
        return result.stdout.strip() if result.returncode == 0 else f"(could not read {vault}/{path})"
    except Exception as e:
        return f"(error reading {vault}/{path}: {e})"


def _run_shell(cmd: str, timeout: int = 10) -> str:
    """Run a shell command and return output."""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout,
        )
        return result.stdout.strip() or result.stderr.strip() or "(no output)"
    except Exception as e:
        return f"(error: {e})"


def gather_context() -> dict:
    """Gather all morning context from vault, API, and git."""
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    ctx = {"today": today, "yesterday": yesterday}

    # Yesterday's daily notes (both vaults)
    ctx["yesterday_daily_note"] = _obsidian_read(
        f"Terra/Journal/Daily/{yesterday}.md"
    )

    # Pax-ENV (work vault) — different path structure, no Terra/ prefix
    pax_yesterday = _obsidian_read_vault(
        "Pax-ENV", f"Journal/Daily/{yesterday}.md"
    )
    ctx["yesterday_pax_note"] = pax_yesterday

    # Yesterday's timer data (shifts)
    timer_shifts = _get("/api/timer/shifts")
    if isinstance(timer_shifts, dict) and "error" not in timer_shifts:
        total_work = timer_shifts.get("total_work_minutes", 0)
        total_break = timer_shifts.get("total_break_minutes", 0)
        shifts = timer_shifts.get("shifts", [])
        ctx["yesterday_timer_summary"] = (
            f"Total work: {total_work}min, Total break: {total_break}min, "
            f"Shift count: {len(shifts)}"
        )
    else:
        ctx["yesterday_timer_summary"] = "(timer data unavailable)"

    # Active worktrees
    worktrees = []
    for repo_path in [
        "~/AskCivic/askcivic",
        "~/Scripts/cli-tools/token-api",
    ]:
        expanded = os.path.expanduser(repo_path)
        if os.path.isdir(expanded):
            wt_output = _run_shell(f"cd {expanded} && git worktree list --porcelain 2>/dev/null")
            if wt_output and "worktree" in wt_output:
                lines = wt_output.strip().split("\n")
                # Filter to non-main worktrees
                current_wt = {}
                for line in lines:
                    if line.startswith("worktree "):
                        if current_wt and current_wt.get("path") != expanded:
                            worktrees.append(current_wt)
                        current_wt = {"path": line.split(" ", 1)[1], "repo": repo_path}
                    elif line.startswith("branch "):
                        current_wt["branch"] = line.split(" ", 1)[1]
                if current_wt and current_wt.get("path") != expanded:
                    worktrees.append(current_wt)

    ctx["active_worktrees"] = (
        "\n".join(f"- {wt['repo']}: {wt.get('branch', 'detached')} at {wt['path']}" for wt in worktrees)
        if worktrees else "No active worktrees"
    )

    # Stale/active sessions — instances that were active yesterday or are still active
    instances = _get("/api/instances?sort=recent_activity")
    if isinstance(instances, list):
        active = [i for i in instances if i.get("status") in ("active", "idle")]
        recent = [
            i for i in instances
            if i.get("status") == "stopped"
            and i.get("last_activity", "") > yesterday
        ][:5]
        session_lines = []
        for i in active:
            name = i.get("name") or i.get("id", "unknown")[:8]
            session_lines.append(f"- ACTIVE: {name} ({i.get('working_dir', '?')})")
        for i in recent:
            name = i.get("name") or i.get("id", "unknown")[:8]
            session_lines.append(f"- STOPPED (recent): {name}")
        ctx["stale_sessions"] = "\n".join(session_lines) if session_lines else "No active or recent sessions"
    else:
        ctx["stale_sessions"] = "(instance data unavailable)"

    # Rollover tasks — extract from yesterday's daily note
    rollover_lines = []
    for line in ctx["yesterday_daily_note"].split("\n"):
        stripped = line.strip()
        if stripped.startswith("- [ ]"):
            rollover_lines.append(stripped)
    ctx["rollover_tasks"] = (
        "\n".join(rollover_lines) if rollover_lines else "No rollover tasks from yesterday"
    )

    # Habits
    habits = _get("/api/habits/today")
    if isinstance(habits, dict) and "error" not in habits:
        summary = habits.get("summary", {})
        ctx["habits_state"] = (
            f"Completed: {summary.get('completed', 0)}/{summary.get('total', 0)}"
        )
    else:
        ctx["habits_state"] = "(habits data unavailable)"

    # Fleet state
    fleet = _get("/api/fleet/state")
    if isinstance(fleet, dict) and "error" not in fleet:
        last_fg = fleet.get("last_fg_run", "unknown")
        notes = fleet.get("notes", [])
        ctx["fleet_state"] = f"Last FG run: {last_fg}"
        if notes:
            ctx["fleet_state"] += f"\nFleet notes: {notes[0][:200]}"
    else:
        ctx["fleet_state"] = "(fleet data unavailable)"

    return ctx


def build_prompt(ctx: dict) -> str:
    """Read the prompt template and inject context."""
    prompt_path = os.path.expanduser(PROMPT_PATH)
    with open(prompt_path, "r") as f:
        template = f.read()

    replacements = {
        "{YESTERDAY_DAILY_NOTE}": ctx["yesterday_daily_note"][:3000],
        "{YESTERDAY_PAX_NOTE}": ctx["yesterday_pax_note"][:2000],
        "{YESTERDAY_TIMER_SUMMARY}": ctx["yesterday_timer_summary"],
        "{ACTIVE_WORKTREES}": ctx["active_worktrees"],
        "{STALE_SESSIONS}": ctx["stale_sessions"],
        "{ROLLOVER_TASKS}": ctx["rollover_tasks"],
        "{HABITS_STATE}": ctx["habits_state"],
        "{FLEET_STATE}": ctx["fleet_state"],
        "{TODAY}": ctx["today"],
    }

    for placeholder, value in replacements.items():
        template = template.replace(placeholder, value)

    return template


def spawn_claude(prompt_text: str, session_id: str, is_resume: bool = False) -> str:
    """Spawn or resume a Claude session and return the output."""
    # Write prompt to temp file (avoids shell escaping issues)
    prompt_file = SESSION_DIR / f"prompt_{session_id[:8]}.md"
    prompt_file.write_text(prompt_text)

    if is_resume:
        cmd = [
            "claude", "-p", prompt_text,
            "--resume", session_id,
            "--output-format", "text",
            "--dangerously-skip-permissions",
        ]
    else:
        cmd = [
            "claude", "--model", MODEL,
            "-p", prompt_text,
            "--session-id", session_id,
            "--output-format", "text",
            "--dangerously-skip-permissions",
        ]

    env = dict(os.environ)
    # Ensure Claude can find tools
    extra_paths = [
        os.path.expanduser("~/Scripts/cli-tools/bin"),
        os.path.expanduser("~/.local/bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    for p in reversed(extra_paths):
        if p not in env.get("PATH", ""):
            env["PATH"] = f"{p}:{env.get('PATH', '')}"

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=300, env=env,
            cwd=os.path.expanduser("~/Imperium-ENV"),
        )
        return result.stdout.strip() if result.returncode == 0 else f"(Claude error: {result.stderr[:500]})"
    except subprocess.TimeoutExpired:
        return "(Claude session timed out after 300s)"
    except Exception as e:
        return f"(Claude spawn error: {e})"


def send_tts(message: str):
    """Send a message via TTS to the Emperor's phone."""
    _post("/api/notify/tts", {"message": message})


def _discord_read(channel: str, since_iso: str) -> list[dict]:
    """Read non-bot messages from a Discord channel or thread since a given ISO time."""
    import urllib.parse
    import urllib.request as ureq
    params = urllib.parse.urlencode({"channel": channel, "limit": 20, "since": since_iso})
    try:
        with ureq.urlopen(f"{DISCORD_DAEMON}/read?{params}", timeout=10) as resp:
            data = json.loads(resp.read())
            return [m for m in data.get("messages", []) if not m.get("author", {}).get("bot")]
    except Exception:
        return []


def get_daily_thread_id(today: str) -> str | None:
    """Read thread_id from today's daily note frontmatter (set by Aspirants pipeline)."""
    import re
    try:
        result = subprocess.run(
            ["obsidian", "vault=Imperium-ENV", "read", f"path=Terra/Journal/Daily/{today}.md"],
            capture_output=True, text=True, timeout=15,
        )
        m = re.search(r'^thread_id:\s*(.+)$', result.stdout, re.MULTILINE)
        if m:
            tid = m.group(1).strip().strip('"\'')
            return tid if tid and tid != "null" else None
    except Exception:
        pass
    return None


def _discord_post(channel: str, content: str, bot: str = "custodes") -> dict:
    """Send a message to a Discord channel via the daemon. Returns message data."""
    import urllib.request as ureq
    body = json.dumps({"channel": channel, "content": content, "bot": bot}).encode()
    req = ureq.Request(f"{DISCORD_DAEMON}/send", data=body, headers={"Content-Type": "application/json"})
    try:
        with ureq.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def _discord_create_thread(channel: str, name: str, bot: str = "custodes") -> str | None:
    """Create a thread in a channel. Returns thread_id or None on failure."""
    import urllib.request as ureq
    body = json.dumps({"channel": channel, "name": name, "bot": bot}).encode()
    req = ureq.Request(f"{DISCORD_DAEMON}/thread/create", data=body, headers={"Content-Type": "application/json"})
    try:
        with ureq.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
            return data.get("thread_id")
    except Exception as e:
        print(f"Warning: could not create daily thread: {e}")
        return None


def create_daily_thread(today: str, briefing_text: str) -> str | None:
    """Create today's Discord thread in #briefing, post briefing, store thread_id in note."""
    thread_name = f"Daily — {today}"
    thread_id = _discord_create_thread("briefing", thread_name)
    if thread_id:
        # Post briefing as the first message in the thread
        import urllib.request as ureq
        body = json.dumps({"thread_id": thread_id, "content": briefing_text[:1900], "bot": "custodes"}).encode()
        req = ureq.Request(f"{DISCORD_DAEMON}/thread/send", data=body,
                           headers={"Content-Type": "application/json"})
        try:
            with ureq.urlopen(req, timeout=15):
                pass
        except Exception as e:
            print(f"Warning: could not post briefing to thread: {e}")
        # Write thread_id into daily note frontmatter
        try:
            subprocess.run(
                ["obsidian", "vault=Imperium-ENV", "property:set",
                 f"path=Terra/Journal/Daily/{today}.md",
                 "property=thread_id", f"value={thread_id}"],
                capture_output=True, text=True, timeout=15,
            )
            print(f"Daily thread created: {thread_id}")
        except Exception as e:
            print(f"Warning: could not write thread_id to note: {e}")
    return thread_id


def _discord_reply(thread_id: str | None, channel: str, content: str):
    """Send Claude's response to the daily thread (if exists) and channel."""
    # Truncate to Discord's 2000-char limit
    text = content[:1900]
    if thread_id:
        try:
            import urllib.request as ureq
            body = json.dumps({"thread_id": thread_id, "content": text, "bot": "custodes"}).encode()
            req = ureq.Request(f"{DISCORD_DAEMON}/thread/send", data=body,
                               headers={"Content-Type": "application/json"})
            with ureq.urlopen(req, timeout=15):
                pass
            return
        except Exception:
            pass
    # Fallback: post to channel directly
    try:
        subprocess.run(
            ["discord", "send", channel, "--bot", "custodes", text],
            capture_output=True, timeout=15,
        )
    except Exception:
        pass


def get_current_state() -> dict:
    """Get current state snapshot for follow-up evaluation."""
    return _get("/api/state")


def evaluate_state_change(prev_state: dict, curr_state: dict) -> str | None:
    """Compare states and generate a follow-up message if something changed.

    Returns a message to inject into the Claude session, or None if nothing
    worth reporting.
    """
    changes = []

    # Instance count changed
    prev_instances = prev_state.get("active_instances", 0)
    curr_instances = curr_state.get("active_instances", 0)
    if curr_instances != prev_instances:
        changes.append(f"Active instances changed: {prev_instances} → {curr_instances}")

    # Timer mode changed
    prev_mode = prev_state.get("timer_mode", "unknown")
    curr_mode = curr_state.get("timer_mode", "unknown")
    if curr_mode != prev_mode:
        changes.append(f"Timer mode changed: {prev_mode} → {curr_mode}")

    # Processing started (Emperor doing something)
    if curr_state.get("is_processing") and not prev_state.get("is_processing"):
        changes.append("Emperor started active work (processing detected)")

    if not changes:
        return None

    now = datetime.now().strftime("%H:%M")
    return f"State update at {now}: " + ". ".join(changes)


def ensure_daily_notes():
    """Create today's daily notes in both vaults via Obsidian CLI.

    The `obsidian vault=X daily` command triggers Obsidian to open/create
    the daily note, which runs Templater to apply the template. If the note
    already exists, this is a no-op.
    """
    for vault in ["Imperium-ENV", "Pax-ENV"]:
        try:
            result = subprocess.run(
                ["obsidian", f"vault={vault}", "daily"],
                capture_output=True, text=True, timeout=10,
            )
            # The command may exit 1 ("unknown command") but still creates the note
            if "Opened:" in result.stdout or "Opened:" in result.stderr:
                print(f"Daily note created/opened in {vault}")
            elif "Failed" in (result.stdout + result.stderr):
                print(f"Warning: daily note creation may have failed in {vault}")
            else:
                print(f"Daily note triggered in {vault}")
        except Exception as e:
            print(f"Warning: could not create daily note in {vault}: {e}")


async def run_morning_session():
    """Main morning session loop."""
    SESSION_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    session_id = str(uuid.uuid4())
    state_file = SESSION_DIR / f"morning_{today}.json"

    # Prevent double-trigger
    if state_file.exists():
        data = json.loads(state_file.read_text())
        if data.get("status") == "active":
            print("Morning session already active, skipping")
            return {"status": "already_active", "session_id": data.get("session_id")}

    # Save session state
    state_file.write_text(json.dumps({
        "session_id": session_id,
        "started_at": datetime.now().isoformat(),
        "status": "active",
    }))

    print(f"Morning session starting: {session_id[:8]}")

    # Phase 0: Ensure NAS is mounted — vault reads/writes depend on it
    try:
        from nas_mount import ensure_mounted
        for share in ["/Volumes/Imperium"]:
            ok, msg = ensure_mounted(share)
            if not ok:
                send_tts(f"Morning session could not start: {msg}")
                state_file.write_text(json.dumps({
                    "session_id": session_id,
                    "started_at": datetime.now().isoformat(),
                    "status": "nas_unavailable",
                    "error": msg,
                }))
                return {"status": "nas_unavailable", "error": msg}
    except ImportError:
        pass  # nas_mount not available, proceed and let failures surface naturally

    # Phase 1: Create daily notes in both vaults
    ensure_daily_notes()

    # Phase 1: Gather context and generate briefing
    ctx = gather_context()
    prompt = build_prompt(ctx)

    briefing = spawn_claude(prompt, session_id, is_resume=False)
    print(f"Briefing generated ({len(briefing)} chars)")

    # Send briefing via TTS
    send_tts(briefing)

    # Create daily Discord thread from briefing; store thread_id for routing
    daily_thread_id = get_daily_thread_id(today)
    if not daily_thread_id:
        daily_thread_id = create_daily_thread(today, briefing)

    # Phase 2: Follow-up loop
    # Timer starts when Claude stops (spawn_claude is blocking).
    # Each iteration: sleep 1 min → check Discord → if messages use them,
    # else send timestamp ping. Discord input resets priority naturally.
    prev_state = get_current_state()
    start_time = datetime.now()
    morning_ended = False
    last_discord_check = datetime.now().isoformat()
    _enforce_acknowledged = False  # Track whether we've cleared the enforce loop

    while not morning_ended:
        # Timer starts here — 1 min after Claude last responded
        await asyncio.sleep(FOLLOW_UP_INTERVAL_SECONDS)

        elapsed = (datetime.now() - start_time).total_seconds() / 60
        if elapsed > MORNING_PHASE_MAX_MINUTES:
            wrap_up = spawn_claude(
                f"Morning phase ending after {int(elapsed)} minutes. "
                f"Generate a final regiment score based on what you know. "
                f"Write it to the daily note.",
                session_id, is_resume=True,
            )
            send_tts(wrap_up)
            _discord_reply(daily_thread_id, "briefing", wrap_up)
            morning_ended = True
            break

        # Check Discord — Emperor's messages take priority over system pings
        discord_input = None
        if elapsed < MORNING_DISCORD_WINDOW_MINUTES:
            msgs = _discord_read("briefing", last_discord_check)
            if daily_thread_id:
                msgs += _discord_read(daily_thread_id, last_discord_check)
            last_discord_check = datetime.now().isoformat()
            if msgs:
                discord_input = "\n".join(
                    f"[Emperor via Discord]: {m['content']}" for m in msgs
                )
                print(f"Discord input: {len(msgs)} messages")
                # Emperor responded — clear the enforce loop
                if not _enforce_acknowledged:
                    _enforce_acknowledged = True
                    try:
                        _post("/api/morning/acknowledge", {})
                    except Exception as e:
                        print(f"Warning: could not auto-acknowledge morning enforce: {e}")

        # Build next message: Discord reply OR timestamp ping
        if discord_input:
            next_msg = discord_input
        else:
            curr_state = get_current_state()
            state_update = evaluate_state_change(prev_state, curr_state)
            prev_state = curr_state
            now_str = datetime.now().strftime("%H:%M")
            next_msg = f"It's {now_str}."
            if state_update:
                next_msg += f" {state_update}."
            next_msg += " What are you doing right now?"

        response = spawn_claude(next_msg, session_id, is_resume=True)
        if response and not response.startswith("("):
            send_tts(response)
            _discord_reply(daily_thread_id, "briefing", response)

        # Morning-end: Emperor actively working, but only after min elapsed
        # and only if triggered by a system ping (not our own spawn)
        if elapsed >= MORNING_END_MIN_ELAPSED and not discord_input:
            curr_state = get_current_state()
            if curr_state.get("timer_mode") == "working":
                await asyncio.sleep(60)
                confirm_state = get_current_state()
                if confirm_state.get("timer_mode") == "working":
                    wrap_up = spawn_claude(
                        "Emperor is actively working on the treadmill. Morning phase complete. "
                        "Generate final regiment score and write to daily note. "
                        "Brief wrap-up — what's the focus for today.",
                        session_id, is_resume=True,
                    )
                    send_tts(wrap_up)
                    _discord_reply(daily_thread_id, "briefing", wrap_up)
                    morning_ended = True

    # Update state file
    state_file.write_text(json.dumps({
        "session_id": session_id,
        "started_at": start_time.isoformat(),
        "ended_at": datetime.now().isoformat(),
        "status": "completed",
    }))

    print(f"Morning session completed: {session_id[:8]}")
    return {"status": "completed", "session_id": session_id}


def start_morning_session_background():
    """Entry point for Token-API to start the morning session in background."""
    loop = asyncio.get_event_loop()
    if loop.is_running():
        asyncio.create_task(run_morning_session())
    else:
        asyncio.run(run_morning_session())


if __name__ == "__main__":
    asyncio.run(run_morning_session())
