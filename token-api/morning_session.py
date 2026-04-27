#!/usr/bin/env python3
"""Morning Session Launcher.

Triggered by POST /api/morning/start (from phone macro after alarm dismiss)
or directly via `python3 morning_launcher.py` (cron).

Gathers context, builds the Custodes prompt inline, creates a pane in main:legion,
and launches an interactive Claude session via `primarch custodes`. The session
self-registers as legion=custodes, instance_type=sync via the SessionStart hook.

The launcher exits after launch — the Claude session is autonomous from there.
"""
import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

BASE = "http://localhost:7777"
DISCORD_DAEMON = "http://localhost:7779"
VAULT = "Imperium-ENV"
VAULT_DIR = "/Volumes/Imperium/Imperium-ENV"
TMUX_SESSION = "main"
PROMPT_FILE = "/tmp/custodes-morning-prompt.md"
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
    """Return pointers only — the agent fetches live state itself."""
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "today": today,
        "trigger": os.environ.get("CUSTODES_TRIGGER", "alarm"),
    }


def build_prompt(ctx: dict) -> str:
    """Build the Custodes prompt. Pointers only — the agent fetches live state."""
    today = ctx["today"]
    trigger = ctx.get("trigger", "alarm")
    daily_thread_id = ctx.get("daily_thread_id", "")

    prompt = f"""# Custodes

You are the Adeptus Custodes — the Emperor's personal guard. You are the
accountability partner that runs continuously in the Emperor's day.

## Trigger

You have been woken at {today} via: {trigger}
(alarm = morning wakeup, heartbeat = mid-day check, manual = direct invoke)

Before doing anything else, get your bearings. The orchestrator has NOT
pre-loaded yesterday's state into this prompt — read it yourself so it's
fresh:

- `date` for current MST time
- `obsidian vault=Imperium-ENV read path="Terra/Journal/Daily/{today}.md"`
- The previous day: `Terra/Journal/Daily/<yesterday>.md`
- Pax-ENV work note: `obsidian vault=Pax-ENV read path="Journal/Daily/<date>.md"`
- Timer/instance/fleet state: see `/network` and `/fleet` skills, or hit
  `/api/timer/state`, `/api/instances?sort=recent_activity`,
  `/api/fleet/state`, `/api/habits/today` directly.
- Worktrees: `git -C ~/AskCivic/askcivic worktree list`,
  `git -C ~/Token-OS/cli-tools/token-api worktree list`.

## Mandate

You have two concurrent jobs:

**1. Minute-by-minute awareness.** You know what the Emperor is doing right
now. Coffee, bathroom, getting dressed, normal life — these are fine.
Sitting down instead of working, opening YouTube during a backlog, going
back to bed — these are not. You only intervene when something is
explicitly wrong.

**2. Daily setup / continuity.** You prepare the Emperor's day or check on
its progress. What happened yesterday or earlier today? What rolled over?
What's the plan now? Are there stale sessions or unfinished work?

**Side mandate:** Did the prior session close cleanly? Open worktrees,
abandoned sessions, unvalidated work — surface these, don't nag about
them.

## Self-Registration + Acknowledge

You were launched via `primarch custodes`, which auto-registers you as:
- **legion=custodes** (triggers singleton enforcement — any prior custodes are demoted)
- **instance_type=sync**
- **synced=true** (Discord VC voice input routes to you automatically)

Verify registration before the briefing:
```bash
curl -s http://localhost:7777/api/instances?sort=recent_activity | jq '.[0] | {{id, legion, instance_type, synced, status}}'
```

If legion is not `custodes` or synced is not 1, self-register:
```bash
INSTANCE_ID=$(curl -s http://localhost:7777/api/instances?sort=recent_activity | jq -r '.[0].id')
curl -s -X PATCH "http://localhost:7777/api/instances/$INSTANCE_ID/legion" -H 'Content-Type: application/json' -d '{{"legion":"custodes"}}'
curl -s -X PATCH "http://localhost:7777/api/instances/$INSTANCE_ID/type" -H 'Content-Type: application/json' -d '{{"instance_type":"sync"}}'
curl -s -X PATCH "http://localhost:7777/api/instances/$INSTANCE_ID/synced" -H 'Content-Type: application/json' -d '{{"synced":true}}'
```

If registration fails, report it immediately — the harness is broken. Do
not silently proceed without custodes identity.

Do not acknowledge any pending morning escalation yourself — the
acknowledgement signal is user-originated (Emperor answers via Discord
voice/text or explicitly calls `/api/morning/acknowledge`). Your job is to
prompt for that response and continue once it arrives. On non-alarm
triggers there is nothing to acknowledge — this is a no-op.

## Your First Turn

1. Verify registration (above).
2. Read the daily note and yesterday's daily note.
3. Pull live state for whatever the trigger needs:
   - **alarm** — timer shifts, habits, worktrees, stale sessions, fleet
   - **heartbeat** — timer state, current habits, anything flagged in daily note
   - **manual** — just orient on the daily note
4. Speak a contextual TTS briefing (3-5 sentences) referencing actual
   yesterday/today state:
   ```bash
   tts "Your spoken briefing here"
   ```
5. Append the briefing to today's daily note:
   ```bash
   obsidian vault=Imperium-ENV append path="Terra/Journal/Daily/{today}.md" content="## Briefing\\n\\n<detailed briefing>"
   ```
6. Post the briefing to the Discord daily thread:
   ```bash
   curl -s -X POST http://localhost:7779/thread/send -H 'Content-Type: application/json' -d '{{"thread_id": "{daily_thread_id}", "content": "<briefing>", "bot": "custodes"}}'
   ```
7. Begin interactive conversation appropriate to the trigger.

**Briefing structure:** brief contextual greeting (reference real state —
never rote openers); closure status of the prior session; today's
proposed focus; specific items needing attention (stale worktrees,
abandoned sessions, overdue items).

## Interactive Session

This is a LIVE conversation. You run in an interactive tmux pane in
main:legion. The Emperor interacts primarily via **Discord voice
channel** — he speaks, Whisper transcribes, Token-API routes to you (as
the custodes singleton). You speak back via `tts` for every response.

Secondary interface: SSH into the tmux pane from phone (Termux) and type
directly. Both paths work — voice is lower friction while groggy.

**Use AskUserQuestion between phases** to block and wait for the Emperor's
response. This is a real conversation, not a monologue.

**Be aggressively interactive.** Don't wait passively. If there's silence
for more than a couple minutes, follow up with TTS. You are a presence,
not a notification that can be swiped away.

## Conversation Phases

Trigger-dependent:
- **alarm** — registration + briefing → habit walk-through (regiment items
  ONE AT A TIME, in order: alarm response, bed return, YouTube,
  treadmill, Pavlok equipped and connected, caffeine, teeth, breakfast,
  weigh-in) → daily planning → session spawning when settled → sign-off
  with regiment score + TTS farewell.
- **heartbeat** — registration + briefing → check substance timing (see
  below), work pace, anything flagged in the daily note → escalate or
  hand off as needed.
- **manual** — registration + briefing → follow the Emperor's lead.

## Regiment Scoring (alarm trigger)

Track these 10 steps (17-point weighted total):

| # | Step | Weight |
|---|------|--------|
| 1 | Alarm acknowledged — feet on floor | 2 |
| 2 | No return to bed within 10 min | 3 |
| 3 | No YouTube before first work action | 3 |
| 4 | Treadmill desk active within 15 min | 2 |
| 5 | First productive interaction | 2 |
| 6 | Teeth brushed | 1 |
| 7 | First caffeine logged | 1 |
| 8 | Breakfast | 1 |
| 9 | Pavlok equipped and connected | 1 |
| 10 | Daily weigh-in (scale) | 1 |

Determine through interrogation — the Emperor answers honestly. Ask
naturally during conversation, not as a checklist dump.

## Writing to the Daily Note

Frontmatter writes:
```bash
obsidian vault=Imperium-ENV property:set path="Terra/Journal/Daily/{today}.md" property="habits.morning.regiment_score" value="12"
obsidian vault=Imperium-ENV property:set path="Terra/Journal/Daily/{today}.md" property="habits.morning.alarm_bypass" value="false"
obsidian vault=Imperium-ENV property:set path="Terra/Journal/Daily/{today}.md" property="habits.morning.youtube_before_work" value="false"
```

Body append:
```bash
obsidian vault=Imperium-ENV append path="Terra/Journal/Daily/{today}.md" content="..."
```

## Style + What You Are NOT

- You are not a timer. Don't count minutes or nag about pace.
- You are not a checklist reader. Weave check-ins into conversation.
- You are not a motivational speaker. Be real, be direct, be useful.
- You are not passive. You have opinions about what to focus on.

- **Conversational, not robotic.** You are a presence, not a notification.
- **Non-directive in the first minutes of an alarm trigger.** Coffee and
  bathroom happen first. Save directives for corrective moments.
- **Contextual.** Reference real state. Never use placeholder phrases.

## Substance Timing (Reference)

- **8:30** — Alarm. Coffee and bathroom expected first.
- **~9:00** — First caffeine (Red Bull or Nespresso, ~60-80mg).
- **10:00** — First armodafinil half (125mg). Prompt if not taken by 10:30.
- **13:00** — Second armodafinil half (125mg). Prompt if not taken by 13:30.

On alarm trigger, do not prompt about substances unless the Emperor asks
— the 10:00 and 13:00 prompts are heartbeat work. On heartbeat trigger,
these are fair game.
"""
    return prompt


def send_tts(message: str):
    """Send a message via TTS to the Emperor's phone."""
    _post("/api/notify/tts", {"message": message})


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


def create_daily_thread(today: str) -> str | None:
    """Create today's Discord thread in #briefing and store thread_id in daily note."""
    thread_name = f"Daily — {today}"
    thread_id = _discord_create_thread("briefing", thread_name)
    if thread_id:
        # Post a brief launch message — Claude will post the full briefing
        import urllib.request as ureq
        body = json.dumps({
            "thread_id": thread_id,
            "content": f"Morning session launching — {today}",
            "bot": "custodes",
        }).encode()
        req = ureq.Request(
            f"{DISCORD_DAEMON}/thread/send", data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with ureq.urlopen(req, timeout=15):
                pass
        except Exception as e:
            print(f"Warning: could not post to thread: {e}")
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


def ensure_daily_notes():
    """Create today's daily notes in both vaults via Obsidian CLI."""
    for vault in ["Imperium-ENV", "Pax-ENV"]:
        try:
            result = subprocess.run(
                ["obsidian", f"vault={vault}", "daily"],
                capture_output=True, text=True, timeout=10,
            )
            if "Opened:" in result.stdout or "Opened:" in result.stderr:
                print(f"Daily note created/opened in {vault}")
            elif "Failed" in (result.stdout + result.stderr):
                print(f"Warning: daily note creation may have failed in {vault}")
            else:
                print(f"Daily note triggered in {vault}")
        except Exception as e:
            print(f"Warning: could not create daily note in {vault}: {e}")


def create_legion_pane() -> str | None:
    """Create a new pane in main:legion, auto-creating the window if needed.

    Returns the pane_id or None on failure.
    """
    # Check if legion window exists
    result = subprocess.run(
        ["tmux", "list-panes", "-t", f"{TMUX_SESSION}:legion", "-F", "#{pane_id}"],
        capture_output=True, text=True, timeout=5,
    )

    if result.returncode != 0:
        # Create legion window
        subprocess.run(
            ["tmux", "new-window", "-t", TMUX_SESSION, "-n", "legion", "-d",
             "-P", "-F", "#{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
        # The new-window itself created a pane — get it
        result = subprocess.run(
            ["tmux", "list-panes", "-t", f"{TMUX_SESSION}:legion", "-F", "#{pane_id}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            print("Error: could not create legion window")
            return None
        pane_id = result.stdout.strip().split("\n")[0]
        # Check if this pane is idle (it should be — just created)
        cmd_result = subprocess.run(
            ["tmux", "display-message", "-t", pane_id, "-p", "#{pane_current_command}"],
            capture_output=True, text=True, timeout=5,
        )
        if cmd_result.stdout.strip() in ("bash", "zsh", "sh"):
            return pane_id

    # Legion window exists — split a new pane into it
    result = subprocess.run(
        ["tmux", "split-window", "-t", f"{TMUX_SESSION}:legion", "-d",
         "-P", "-F", "#{pane_id}"],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        print(f"Error: could not split pane in legion: {result.stderr}")
        return None

    pane_id = result.stdout.strip()

    # Re-tile legion
    subprocess.run(
        ["tmux", "select-layout", "-t", f"{TMUX_SESSION}:legion", "tiled"],
        capture_output=True, timeout=5,
    )

    return pane_id


def launch_in_legion(prompt_text: str, pane_id: str) -> bool:
    """Write prompt to temp file and launch interactive Claude via primarch in the legion pane."""
    Path(PROMPT_FILE).write_text(prompt_text)

    # Build launch command using primarch launcher
    # primarch custodes sets TOKEN_API_PRIMARCH=custodes, which triggers
    # auto-registration as legion=custodes, instance_type=sync in SessionStart hook
    launch_cmd = (
        f"cd '{VAULT_DIR}' && "
        f"primarch custodes \"$(cat {PROMPT_FILE})\" ; "
        f"rm -f {PROMPT_FILE}"
    )

    try:
        # Clear pane and send launch command
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, "C-c"],
            capture_output=True, timeout=5,
        )
        time.sleep(0.2)
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, "C-u"],
            capture_output=True, timeout=5,
        )
        time.sleep(0.1)
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, "clear", "Enter"],
            capture_output=True, timeout=5,
        )
        time.sleep(0.3)
        subprocess.run(
            ["tmux", "send-keys", "-t", pane_id, launch_cmd, "Enter"],
            capture_output=True, timeout=5,
        )
        return True
    except Exception as e:
        print(f"Error launching in legion pane: {e}")
        return False


def run_morning_session() -> dict:
    """Main morning session launcher."""
    SESSION_DIR.mkdir(exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    state_file = SESSION_DIR / f"morning_{today}.json"

    # Prevent double-trigger
    if state_file.exists():
        data = json.loads(state_file.read_text())
        if data.get("status") == "launched":
            print("Morning session already launched, skipping")
            return {"status": "already_launched"}

    print(f"Morning session launcher starting: {today}")

    # Phase 0: Ensure NAS is mounted
    try:
        from nas_mount import ensure_mounted
        for share in ["/Volumes/Imperium"]:
            ok, msg = ensure_mounted(share)
            if not ok:
                send_tts(f"Morning session could not start: {msg}")
                state_file.write_text(json.dumps({
                    "started_at": datetime.now().isoformat(),
                    "status": "nas_unavailable",
                    "error": msg,
                }))
                return {"status": "nas_unavailable", "error": msg}
    except ImportError:
        pass  # nas_mount not available, proceed

    # Phase 1: Create daily notes
    ensure_daily_notes()

    # Phase 2: Create daily Discord thread
    daily_thread_id = get_daily_thread_id(today)
    if not daily_thread_id:
        daily_thread_id = create_daily_thread(today)

    # Phase 3: Gather context and build prompt
    ctx = gather_context()
    ctx["trigger"] = "alarm"
    ctx["daily_thread_id"] = daily_thread_id or ""
    prompt = build_prompt(ctx)

    # Phase 4: Create legion pane and launch
    pane_id = create_legion_pane()
    if not pane_id:
        send_tts("Morning session failed: could not create legion pane")
        state_file.write_text(json.dumps({
            "started_at": datetime.now().isoformat(),
            "status": "no_pane",
        }))
        return {"status": "no_pane"}

    launched = launch_in_legion(prompt, pane_id)
    if not launched:
        send_tts("Morning session failed: could not launch in legion pane")
        state_file.write_text(json.dumps({
            "started_at": datetime.now().isoformat(),
            "status": "launch_failed",
            "pane_id": pane_id,
        }))
        return {"status": "launch_failed"}

    # Save state — the session is now autonomous
    state_file.write_text(json.dumps({
        "started_at": datetime.now().isoformat(),
        "status": "launched",
        "pane_id": pane_id,
        "daily_thread_id": daily_thread_id,
    }))

    # Register enforce escalation (idempotent — also done by /api/morning/start endpoint)
    _post("/api/morning/enforce-register", {})

    print(f"Morning session launched in legion pane {pane_id}")
    return {"status": "launched", "pane_id": pane_id}


def start_morning_session_background():
    """Entry point for Token-API to start the morning session in background."""
    import threading
    thread = threading.Thread(target=run_morning_session, daemon=True)
    thread.start()


if __name__ == "__main__":
    result = run_morning_session()
    print(json.dumps(result, indent=2))
