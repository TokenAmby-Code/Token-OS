#!/usr/bin/env python3
"""Morning Session Launcher.

Two-phase launch:
  Phase 1 (headless): Gather context, build prompt, run initial Claude turn
                      that generates a briefing and speaks it via TTS.
  Phase 2 (interactive): Launch `claude --resume` in the mobile tmux pane
                         so the Emperor can continue the session from Termux.

Designed to be called by a cron job at 08:30 MST.
"""
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

# Reuse context gathering from the existing orchestrator
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from morning_session import gather_context, build_prompt, ensure_daily_notes

SESSION_DIR = Path("/tmp/custodes_morning_sessions")
MODEL = "claude-sonnet-4-6"
MOBILE_PANE = "main:mobile-1.2"  # Idle zsh pane visible from Termux
CWD = "/Volumes/Imperium/Imperium-ENV"


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def session_id_for_today() -> str:
    """Deterministic UUID from today's date — same ID all day, valid UUID format."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"custodes-morning-{today_str()}"))


def state_file() -> Path:
    SESSION_DIR.mkdir(exist_ok=True)
    return SESSION_DIR / f"morning_{today_str()}.json"


def already_running() -> bool:
    sf = state_file()
    if sf.exists():
        data = json.loads(sf.read_text())
        if data.get("status") == "active":
            print(f"Morning session already active: {data.get('session_id')}")
            return True
    return False


def write_state(status: str, **extra):
    sf = state_file()
    data = {
        "session_id": session_id_for_today(),
        "started_at": datetime.now().isoformat(),
        "status": status,
        **extra,
    }
    sf.write_text(json.dumps(data))


def build_interactive_prompt(base_prompt: str) -> str:
    """Wrap the base prompt with interactive/TTS instructions."""
    interactive_addendum = """

## Interactive Session Mode

This is a LIVE interactive session running in the Emperor's Termux terminal (phone).
He is still in bed when this starts. He will respond via voice dictation (Wispr).

### TTS is mandatory

You MUST call the `tts` CLI tool for EVERY response so the Emperor hears you through
his phone speaker. Do this BEFORE your text response, or as your primary output method.

```bash
tts "Your spoken message here"
```

Keep TTS messages conversational and concise — they are spoken aloud. You can include
more detail in your text response if needed, but the TTS message should stand alone.

### Conversation flow

1. **Opening briefing** — Speak a 3-5 sentence summary of yesterday + today's plan via TTS.
   Then write a more detailed text summary the Emperor can scroll through later.
2. **Habit walk-through** — Ask about each morning regiment item ONE AT A TIME via TTS.
   Wait for the Emperor's response before moving to the next item. Do NOT dump a checklist.
   Natural order: alarm (already up), bed return, YouTube, treadmill, caffeine, teeth, breakfast.
3. **Daily planning** — After habits, discuss today's focus. What rolled over? What's hot?
   Write the focus items to the daily note.
4. **Session spawning** — Once the Emperor is settled (on treadmill, working), offer to kick
   off Claude sessions in other terminals for specific tasks. You can do this via:
   ```bash
   tmux send-keys -t "main:bridge.2" "claude --dangerously-skip-permissions" Enter
   ```
   Then after Claude starts, send it an initial prompt:
   ```bash
   sleep 3 && tmux send-keys -t "main:bridge.2" "Work on <specific task from Mars/Tasks>" Enter
   ```
5. **Wrap-up** — When all habits are verified and focus is set, write the regiment score
   to the daily note and sign off via TTS.

### Regiment scoring

After walking through habits, write results to the daily note:
```bash
obsidian vault=Imperium-ENV property:set path="Terra/Journal/Daily/{TODAY}.md" property="habits.morning.regiment_score" value="<score>"
obsidian vault=Imperium-ENV property:set path="Terra/Journal/Daily/{TODAY}.md" property="habits.morning.alarm_bypass" value="<true|false>"
obsidian vault=Imperium-ENV property:set path="Terra/Journal/Daily/{TODAY}.md" property="habits.morning.youtube_before_work" value="<true|false>"
```

### Important

- Do NOT end the session yourself. The Emperor will close it when ready.
- Keep responses SHORT for TTS. Long responses should be split: TTS gets the summary,
  text gets the detail.
- You are Custodes — conversational, direct, not robotic. Vary your language.
- If the Emperor seems groggy or unresponsive, be persistent but not annoying.
  One follow-up after 2 minutes of silence is fine.
""".replace("{TODAY}", today_str())

    return base_prompt + interactive_addendum


def phase1_headless():
    """Gather context, run initial Claude turn, speak briefing via TTS."""
    print("Phase 1: Gathering context...")
    ctx = gather_context()
    base_prompt = build_prompt(ctx)
    full_prompt = build_interactive_prompt(base_prompt)

    # Write prompt to file for debugging
    prompt_file = SESSION_DIR / f"prompt_{today_str()}.md"
    prompt_file.write_text(full_prompt)
    print(f"Prompt written to {prompt_file} ({len(full_prompt)} chars)")

    sid = session_id_for_today()

    # Headless initial turn — Claude generates briefing + calls tts
    cmd = [
        os.path.expanduser("~/.local/bin/claude"),
        "--model", MODEL,
        "-p", full_prompt,
        "--session-id", sid,
        "--output-format", "text",
        "--dangerously-skip-permissions",
    ]

    env = dict(os.environ)
    extra_paths = [
        os.path.expanduser("~/Token-OS/cli-tools/bin"),
        os.path.expanduser("~/.local/bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    for p in reversed(extra_paths):
        if p not in env.get("PATH", ""):
            env["PATH"] = f"{p}:{env.get('PATH', '')}"

    print(f"Phase 1: Running headless Claude (session: {sid})...")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=180, env=env, cwd=CWD,
        )
        output = result.stdout.strip()
        if result.returncode != 0:
            print(f"Phase 1 error: {result.stderr[:500]}")
            return False
        print(f"Phase 1 complete: briefing generated ({len(output)} chars)")

        # Save briefing for reference
        briefing_file = SESSION_DIR / f"briefing_{today_str()}.txt"
        briefing_file.write_text(output)

        # Speak briefing via TTS directly — don't rely on model calling tts
        # Truncate for TTS: first ~600 chars is usually the spoken summary
        tts_text = output.strip()
        # Strip markdown formatting for speech
        for prefix in ("---", "##", "**", "- "):
            tts_text = tts_text.replace(prefix, "")
        # Limit to ~800 chars for TTS digestibility
        if len(tts_text) > 800:
            # Find a sentence boundary near 800
            cut = tts_text[:800].rfind(". ")
            if cut > 400:
                tts_text = tts_text[:cut + 1]
            else:
                tts_text = tts_text[:800]
        try:
            subprocess.run(
                ["tts", "--direct", tts_text],
                timeout=30, env=env,
            )
            print("Phase 1: TTS briefing sent")
        except Exception as e:
            print(f"Phase 1: TTS failed (non-fatal): {e}")

        return True
    except subprocess.TimeoutExpired:
        print("Phase 1: Claude timed out after 180s")
        return False
    except Exception as e:
        print(f"Phase 1 error: {e}")
        return False


def phase2_interactive():
    """Launch interactive Claude --resume in the mobile tmux pane."""
    sid = session_id_for_today()

    # Check pane is alive and idle
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", MOBILE_PANE, "-p", "#{pane_current_command}"],
            capture_output=True, text=True, timeout=5,
        )
        current_cmd = result.stdout.strip()
        if current_cmd not in ("zsh", "bash", "sh"):
            print(f"Warning: pane {MOBILE_PANE} is running '{current_cmd}', not idle shell")
            # Still attempt — worst case it'll fail visibly
    except Exception as e:
        print(f"Warning: could not check pane state: {e}")

    # Clear the pane and launch
    resume_cmd = (
        f"cd {CWD} && ~/.local/bin/claude "
        f"--resume {sid} "
        f"--dangerously-skip-permissions"
    )

    try:
        subprocess.run(["tmux", "send-keys", "-t", MOBILE_PANE, "C-u"], timeout=5)
        subprocess.run(
            ["tmux", "send-keys", "-t", MOBILE_PANE, resume_cmd, "Enter"],
            timeout=5,
        )
        print(f"Phase 2: Interactive session launched in {MOBILE_PANE}")
        return True
    except Exception as e:
        print(f"Phase 2 error: {e}")
        return False


def main():
    if already_running():
        return

    write_state("active")

    # Phase 0: Ensure daily notes exist
    print("Phase 0: Creating daily notes...")
    ensure_daily_notes()

    # Phase 1: Headless briefing + TTS
    if not phase1_headless():
        write_state("failed", error="phase1_headless failed")
        # Still try to speak an error
        try:
            subprocess.run(
                ["tts", "Morning session failed during setup. Check the logs."],
                timeout=10,
            )
        except Exception:
            pass
        return

    # Register escalation chain — TTS repeat +5, Discord DM +10, blocked +15
    try:
        import urllib.request
        req = urllib.request.Request(
            "http://localhost:7777/api/morning/enforce-register",
            data=b'{}',
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=5)
        print("Escalation chain registered (+5/+10/+15 min)")
    except Exception as e:
        print(f"Warning: escalation registration failed: {e}")

    # Phase 2: Interactive in mobile pane
    if not phase2_interactive():
        write_state("failed", error="phase2_interactive failed")
        return

    write_state("active", phase="interactive")
    print("Morning session launched successfully.")

    # Tag the morning session as legion=custodes, synced=true
    # Wait a few seconds for the instance to register with Token-API
    import time
    time.sleep(5)
    try:
        import urllib.request
        import json as _json
        # Find instance by tmux pane
        req = urllib.request.Request(
            f"http://localhost:7777/api/instances",
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=5)
        instances = _json.loads(resp.read())
        # Find the instance in our pane
        target = None
        for inst in instances:
            if inst.get("tmux_pane") == MOBILE_PANE and inst.get("status") in ("idle", "processing"):
                target = inst["id"]
                break
        if target:
            # Set legion=custodes
            req = urllib.request.Request(
                f"http://localhost:7777/api/instances/{target}/legion",
                data=_json.dumps({"legion": "custodes"}).encode(),
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            urllib.request.urlopen(req, timeout=5)
            # Set synced=true
            req = urllib.request.Request(
                f"http://localhost:7777/api/instances/{target}/synced",
                data=_json.dumps({"synced": True}).encode(),
                headers={"Content-Type": "application/json"},
                method="PATCH",
            )
            urllib.request.urlopen(req, timeout=5)
            print(f"Morning session tagged: legion=custodes, synced=true (instance={target[:12]})")
        else:
            print(f"Warning: could not find instance in pane {MOBILE_PANE} for legion tagging")
    except Exception as e:
        print(f"Warning: legion tagging failed (non-fatal): {e}")


if __name__ == "__main__":
    main()
