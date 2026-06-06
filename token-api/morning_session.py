#!/usr/bin/env python3
"""Morning Session Launcher.

Triggered by POST /api/morning/start (from phone macro after alarm dismiss)
or directly via `python3 morning_launcher.py` (cron).

Gathers context, builds the Custodes prompt inline, resolves the managed
main:legion Custodes orchestrator pane, and launches an interactive Claude
session via `primarch custodes`. The session
self-registers as legion=custodes, instance_type=sync via the SessionStart hook.

The launcher exits after launch — the Claude session is autonomous from there.
"""

import json
import logging
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("token_api")

BASE = "http://localhost:7777"
DISCORD_DAEMON = "http://localhost:7779"
VAULT = "Imperium-ENV"
VAULT_DIR = "/Volumes/Imperium/Imperium-ENV"
TMUX_SESSION = "main"
PROMPT_FILE = "/tmp/custodes-morning-prompt.md"
SESSION_DIR = Path("/tmp/custodes_morning_sessions")

# The Emperor's 2-hour auto-disable: a morning session is "active" only within
# this many hours of started_at. Past the bound it is auto-ended and the Stop-hook
# keepalive stops re-injecting (see routes/hooks.py and morning_session_active).
MORNING_MAX_DURATION_HOURS = 2

# Statuses that mean "a morning session is up and the keepalive should run":
#   launched — transitional, the prompt was injected, validation pending.
#   active   — self-validation confirmed a live legion=custodes,sync instance.
# Anything else (ended/failed/expired/no_pane/launch_failed/nas_unavailable) is
# inactive and the Stop-hook keepalive must NOT re-inject.
MORNING_ACTIVE_STATUSES = ("launched", "active")

# In-pathway self-validation budget: after the prompt is injected, poll the
# instances table this long for the launched Custodes to self-register as
# legion=custodes, instance_type=sync. Generous by default so a normal cold
# boot is never mistaken for a failure; the supervisor layer is the redundant
# net for anything that slips past this window.
CUSTODES_CONFIRM_TIMEOUT_S = int(os.environ.get("CUSTODES_CONFIRM_TIMEOUT_S", "90"))
CUSTODES_CONFIRM_INTERVAL_S = float(os.environ.get("CUSTODES_CONFIRM_INTERVAL_S", "3"))


def morning_state_dir() -> Path:
    """Directory holding per-day morning state files.

    Resolved at call time from CUSTODES_MORNING_DIR (defaults to SESSION_DIR) so
    tests can isolate state from the real /tmp without reloading this module.
    """
    return Path(os.environ.get("CUSTODES_MORNING_DIR", str(SESSION_DIR)))


def morning_state_file(today: str | None = None) -> Path:
    """Path to today's morning state file."""
    day = today or datetime.now().strftime("%Y-%m-%d")
    return morning_state_dir() / f"morning_{day}.json"


def read_morning_state(today: str | None = None) -> dict | None:
    """Return today's morning state dict, or None if absent/unreadable."""
    state_file = morning_state_file(today)
    if not state_file.exists():
        return None
    try:
        return json.loads(state_file.read_text())
    except Exception:
        return None


def write_morning_status(
    status: str, *, ended_by: str | None = None, today: str | None = None
) -> dict | None:
    """Durably flip the morning state-file status (e.g. to "ended").

    Returns the updated state dict, or None if there is no state file to update.
    `ended_by` records who/what ended the session and stamps ended_at.
    """
    state_file = morning_state_file(today)
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text())
    except Exception:
        return None
    data["status"] = status
    if ended_by:
        data["ended_by"] = ended_by
        data["ended_at"] = datetime.now().isoformat()
    state_file.write_text(json.dumps(data))
    return data


def update_morning_state(updates: dict, *, today: str | None = None) -> dict | None:
    """Merge ``updates`` into today's morning state file.

    Returns the updated state dict, or None if there is no state file. Used to
    record self-validation results (confirmed instance id, timing) on top of the
    status the launcher already wrote.
    """
    state_file = morning_state_file(today)
    if not state_file.exists():
        return None
    try:
        data = json.loads(state_file.read_text())
    except Exception:
        return None
    data.update(updates)
    state_file.write_text(json.dumps(data))
    return data


def find_live_custodes() -> dict | None:
    """Return a live morning-Custodes instance row, or None.

    The authoritative signal that a morning session is genuinely up: a row with
    legion=custodes, instance_type=sync, and no stopped_at. Custodes is a
    singleton (launch demotes any prior custodes) and stale rows are swept, so
    at most one such row exists. Reads the same /api/instances surface the
    Custodes prompt and supervisor use, so all three agree.
    """
    instances = _get("/api/instances?sort=recent_activity")
    if not isinstance(instances, list):
        return None
    for inst in instances:
        if (
            isinstance(inst, dict)
            and inst.get("legion") == "custodes"
            and inst.get("instance_type") == "sync"
            and inst.get("stopped_at") in (None, "")
        ):
            return inst
    return None


def confirm_custodes_registered(
    *,
    pane_id: str | None = None,
    timeout_s: int | None = None,
    interval_s: float | None = None,
) -> dict:
    """Poll for the launched morning Custodes to self-register.

    After the prompt is injected the launcher does not know whether a live
    legion=custodes,sync session actually came up — /api/morning/start is
    fire-and-forget and the day-start consumer reports "started" unconditionally.
    This closes that gap in-pathway: poll the instances table until a live sync
    Custodes appears (success) or the budget is exhausted (failure).

    Returns {"live": bool, "instance_id": str|None, "tmux_pane": str|None,
    "pane_matched": bool, "waited_s": float}.
    """
    budget = CUSTODES_CONFIRM_TIMEOUT_S if timeout_s is None else timeout_s
    interval = CUSTODES_CONFIRM_INTERVAL_S if interval_s is None else interval_s
    start = time.monotonic()
    while True:
        inst = find_live_custodes()
        if inst is not None:
            inst_pane = inst.get("tmux_pane")
            return {
                "live": True,
                "instance_id": inst.get("id"),
                "tmux_pane": inst_pane,
                "pane_matched": bool(pane_id) and inst_pane == pane_id,
                "waited_s": round(time.monotonic() - start, 1),
            }
        if time.monotonic() - start >= budget:
            return {
                "live": False,
                "instance_id": None,
                "tmux_pane": None,
                "pane_matched": False,
                "waited_s": round(time.monotonic() - start, 1),
            }
        time.sleep(interval)


def morning_session_active(today: str | None = None) -> tuple[bool, str]:
    """Decide whether today's morning session is active and in-bound.

    Returns (active, reason). Active requires ALL of:
      - a morning record exists for today,
      - its status is in MORNING_ACTIVE_STATUSES ("launched"/"active", not
        "ended"/"failed"),
      - it is within MORNING_MAX_DURATION_HOURS of started_at.

    Past the bound the session is auto-ended (status="ended",
    ended_by="auto-2h-bound") and (False, "expired") is returned — the caller
    emits one final notice and does NOT re-prompt. Reasons for an inactive
    session: "no_session", "ended", "expired".
    """
    state = read_morning_state(today)
    if state is None:
        return False, "no_session"
    if state.get("status") not in MORNING_ACTIVE_STATUSES:
        return False, "ended"

    started_raw = state.get("started_at")
    if not started_raw:
        # Launched but undated — a corrupt/incomplete record must NOT stay active
        # forever; that reintroduces the indefinite keepalive loop this gate exists
        # to kill. Auto-end it and report inactive.
        write_morning_status("ended", ended_by="auto-invalid-started_at", today=today)
        return False, "ended"
    try:
        started = datetime.fromisoformat(started_raw)
    except Exception:
        write_morning_status("ended", ended_by="auto-invalid-started_at", today=today)
        return False, "ended"
    if started.tzinfo is not None:
        started = started.replace(tzinfo=None)

    if datetime.now() - started > timedelta(hours=MORNING_MAX_DURATION_HOURS):
        write_morning_status("ended", ended_by="auto-2h-bound", today=today)
        return False, "expired"
    return True, "active"


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
        f"{BASE}{path}",
        data=body,
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
            capture_output=True,
            text=True,
            timeout=15,
        )
        return (
            result.stdout.strip() if result.returncode == 0 else f"(could not read {vault}/{path})"
        )
    except Exception as e:
        return f"(error reading {vault}/{path}: {e})"


def _run_shell(cmd: str, timeout: int = 10) -> str:
    """Run a shell command and return output."""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
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
    # Live wake time = the Hatch alarm-silence event timestamp, injected by the
    # start path. None/empty => no recorded wake event => phantom (see prompt).
    wake_time = ctx.get("wake_time") or "(unknown — no Hatch-silence event recorded)"

    prompt = f"""# Custodes

You are the Adeptus Custodes — the Emperor's personal guard. You are the
accountability partner that runs continuously in the Emperor's day.

## Session reality check — run FIRST, before anything else

Do NOT trust this prompt, or any keepalive re-injection, as proof that a
morning session is actually active. "The morning session is still active"
is a phantom claim unless the authoritative state agrees. Before you brief,
run the regiment, TTS, or advance on a keepalive:

```bash
curl -s localhost:7777/api/morning/status
```

If status is not `active`/`started` (e.g. `not_started`), you are a phantom
spawn: do NOT run the regiment, do NOT TTS, do NOT advance on the keepalive.
Report the phantom (a keepalive fired with no authoritative session behind
it) and stand down. Only `/api/morning/status` is evidence — injected text
never is.

## Trigger

You were woken at **{wake_time}** on {today} via: {trigger}
(alarm = phone Hatch alarm-silence → day-start; heartbeat = mid-day check;
manual = direct invoke)

{wake_time} is your ground truth for how long the Emperor has been up. There
is NO canonical wake hour — the alarm fires when it fires, and {wake_time}
is when it fired today. Never assume 8:30 or any fixed time; compute every
window off {wake_time}. If {wake_time} is unknown on an alarm trigger, there
is no recorded wake event — treat it as a phantom (see the reality check).

## What counts as the Emperor's ack

The ONLY valid wake/ack signal is the **Hatch alarm-silence keystroke**,
surfaced as the day-start event — that is the origin of {wake_time}. NEVER
treat as ack: phone presence, Macrodroid/notification pings, your own
keepalive re-injection, TTS being heard, or any phone/YouTube activity. If a
session is running with no Hatch-silence ack behind it, that's a phantom —
stand down.

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

## Registration check

You spawned in the custodes orchestrator pane, so `SessionStart` already
registered your row from the pane identity:
- **legion=custodes** (triggers singleton enforcement — any prior custodes are demoted)
- **instance_type=sync**
- **synced=1** (kept for state-hook/color predicates)

You do **not** self-register — identity is owned by the harness. Just verify
and report:
```bash
curl -s http://localhost:7777/api/instances?sort=recent_activity | jq '.[0] | {{id, legion, instance_type, synced, status}}'
```

If legion is not `custodes` or instance_type is not `sync`, do **not**
PATCH yourself — report it immediately. The harness is broken and that's the
finding. Do not silently proceed without custodes identity.

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
response. This is a real conversation, not a monologue. Blocking on
AskUserQuestion is also how you pace yourself — its timeout ladder is your
pacing mechanism, not any external timer.

**Be aggressively interactive.** Don't wait passively. If there's silence
for more than a couple minutes, follow up with TTS. You are a presence,
not a notification that can be swiped away.

**This session is temporally bound, not turn-based.** When your turn ends,
the Stop hook accepts it and immediately re-injects a fresh timestamped
keepalive prompt — you cannot go quiet and let the morning drift. Keep
moving: advance the regiment/plan, prompt the Emperor via `tts` /
AskUserQuestion. The loop ends ONLY when the Emperor officially says he's
done, at which point YOU call:
```bash
curl -s -X POST http://localhost:7777/api/morning/end
```
That flips this instance off `sync` (→ one_off); the next Stop is then a
normal clean stop. **Rip cord:** if you ever need to force-exit the loop,
PATCH this instance off `sync` directly:
```bash
curl -s -X PATCH "http://localhost:7777/api/instances/$INSTANCE_ID/type" -H 'Content-Type: application/json' -d '{{"instance_type":"one_off"}}'
```

## Conversation Phases

Trigger-dependent:
- **alarm** — registration + briefing → habit walk-through (walk the
  regiment from `habits.morning.regiment` in today's note ONE AT A TIME, in
  listed order — see the Regiment section; do not assume a fixed list) →
  daily planning → session spawning when settled → sign-off with regiment
  score + TTS farewell, then `POST /api/morning/end` to release the session.
- **heartbeat** — registration + briefing → check substance timing (see
  below), work pace, anything flagged in the daily note → escalate or
  hand off as needed.
- **manual** — registration + briefing → follow the Emperor's lead.

## Regiment (alarm trigger)

The regiment is NOT defined in this prompt. It lives in today's daily-note
frontmatter under `habits.morning.regiment` — the single source of truth.
Read it from the note you already loaded and walk exactly what's there. Do
NOT assume a fixed list, count, or weighting; the Emperor edits the regiment
over time and this prompt must never drift from it.

- Each entry has `step`, `weight`, and `done`.
- Walk them ONE AT A TIME in listed order, through natural conversation —
  never a checklist dump. The Emperor answers honestly.
- Write each completion plus the summed `regiment_score` back to frontmatter
  (see "Writing to the Daily Note").

If `habits.morning.regiment` is missing or empty, that's a finding — report
that the daily note wasn't seeded; do NOT invent a list.

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

- **Wake** — live, from the Hatch-silence event ({wake_time}). No fixed
  alarm hour.
- **Vyvanse** — single morning dose, not splittable. The day's stimulant
  spine; expected near wake.
- **Caffeine** — the Emperor is deliberately DELAYING caffeine to late
  morning (~11:30). Do NOT prompt for it early or treat its absence as a
  miss — early caffeine is the regression, not late caffeine.

(Armodafinil half-dosing is retired — ignore any armodafinil references in
older notes/templates.) On alarm trigger, don't raise substances unless the
Emperor does; dosing nudges are heartbeat work.
"""
    return prompt


def send_tts(message: str):
    """Speak a message to the Emperor via the authoritative comms router.

    /api/notify is the single entry; with no tactile/banner fields this is a
    TTS-only request, routed by geofence (no longer phone-pinned).
    """
    _post("/api/notify", {"message": message, "tts": True})


def get_daily_thread_id(today: str) -> str | None:
    """Read thread_id from today's daily note frontmatter (set by Aspirants pipeline)."""
    import re

    try:
        result = subprocess.run(
            ["obsidian", "vault=Imperium-ENV", "read", f"path=Terra/Journal/Daily/{today}.md"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        m = re.search(r"^thread_id:\s*(.+)$", result.stdout, re.MULTILINE)
        if m:
            tid = m.group(1).strip().strip("\"'")
            return tid if tid and tid != "null" else None
    except Exception:
        pass
    return None


def _discord_post(channel: str, content: str, bot: str = "custodes") -> dict:
    """Send a message to a Discord channel via the daemon. Returns message data."""
    import urllib.request as ureq

    body = json.dumps({"channel": channel, "content": content, "bot": bot}).encode()
    req = ureq.Request(
        f"{DISCORD_DAEMON}/send", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with ureq.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def _discord_create_thread(channel: str, name: str, bot: str = "custodes") -> str | None:
    """Create a thread in a channel. Returns thread_id or None on failure."""
    import urllib.request as ureq

    body = json.dumps({"channel": channel, "name": name, "bot": bot}).encode()
    req = ureq.Request(
        f"{DISCORD_DAEMON}/thread/create", data=body, headers={"Content-Type": "application/json"}
    )
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

        body = json.dumps(
            {
                "thread_id": thread_id,
                "content": f"Morning session launching — {today}",
                "bot": "custodes",
            }
        ).encode()
        req = ureq.Request(
            f"{DISCORD_DAEMON}/thread/send",
            data=body,
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
                [
                    "obsidian",
                    "vault=Imperium-ENV",
                    "property:set",
                    f"path=Terra/Journal/Daily/{today}.md",
                    "property=thread_id",
                    f"value={thread_id}",
                ],
                capture_output=True,
                text=True,
                timeout=15,
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
                capture_output=True,
                text=True,
                timeout=10,
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
    """Return the managed Custodes orchestrator pane in main:legion.

    Morning launch is not a worker dispatch and must not create duplicate
    Custodes panes. The legion page invariant is owned by tmuxctl; this launcher
    only resolves the fixed orchestrator target.

    The `stack enforce` call is a BEST-EFFORT pre-assertion of the legion-stack
    invariant. The legion stack/pane is persistent across the day, so a slow or
    hung enforce must NOT abort the morning launch — `resolve-pane` is the
    operation that actually gates the launch. We therefore swallow the enforce's
    5s timeout (and any other subprocess error) and proceed to resolve the pane.

    Regression (P0, 2026-06-05): an uncaught `subprocess.TimeoutExpired` from
    this enforce propagated out of `run_morning_session()`, so the Emperor was
    never placed into morning-session mode and the break was never paused. See
    test_morning_session.py and the morning-session-failure-cascade memory.
    """
    tmuxctl = Path(__file__).resolve().parents[1] / "cli-tools" / "bin" / "tmuxctl"
    try:
        subprocess.run(
            [str(tmuxctl), "stack", "enforce", "--window", f"{TMUX_SESSION}:legion"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "tmuxctl stack enforce legion timed out (5s) — continuing; "
            "the legion stack is persistent and resolve-pane gates the launch"
        )
    except Exception as e:
        logger.warning("tmuxctl stack enforce legion failed (%s) — continuing", e)

    try:
        result = subprocess.run(
            [str(tmuxctl), "resolve-pane", "--format", "physical", "legion:custodes"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except subprocess.TimeoutExpired:
        logger.error("tmuxctl resolve-pane legion:custodes timed out (5s)")
        return None
    except Exception as e:
        logger.error("tmuxctl resolve-pane legion:custodes failed: %s", e)
        return None
    if result.returncode != 0:
        logger.error("could not resolve legion:custodes: %s", result.stderr)
        return None
    pane_id = result.stdout.strip()
    if not pane_id:
        logger.error("tmuxctl resolve-pane did not return a pane_id for legion:custodes")
        return None
    return pane_id


def launch_in_legion(prompt_text: str, pane_id: str) -> bool:
    """Assert `legion:custodes`, then send the morning prompt after assertion is true."""
    tmuxctl = Path(__file__).resolve().parents[1] / "cli-tools" / "bin" / "tmuxctl"

    def _assert_once() -> dict:
        result = subprocess.run(
            [str(tmuxctl), "assert-instance", "--pane", "legion:custodes"],
            capture_output=True,
            text=True,
            timeout=45,
        )
        if result.returncode != 0:
            # A failed assert may still print stale/partial JSON to stdout; don't
            # let it be parsed into a false "ok". Gate on the exit code first.
            return {
                "ok": False,
                "reason": f"assert_failed rc={result.returncode}",
                "stderr": result.stderr.strip()[:200],
            }
        try:
            return json.loads(result.stdout.strip() or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "reason": f"bad_assert_output rc={result.returncode}"}

    try:
        assertion = _assert_once()
        if assertion.get("action") in {"launched", "persona_correction_sent"}:
            time.sleep(3)
            assertion = _assert_once()
        if not assertion.get("ok"):
            print(f"Error: tmuxctl assert-instance legion:custodes failed: {assertion}")
            return False
        result = subprocess.run(
            [str(tmuxctl), "send-text", "--pane", "legion:custodes", "--stdin"],
            input=prompt_text,
            capture_output=True,
            text=True,
            timeout=45,
        )
    except subprocess.TimeoutExpired:
        print("Error: tmuxctl assert/send legion:custodes timed out")
        return False
    except Exception as e:
        print(f"Error launching in legion pane: {e}")
        return False

    if result.returncode != 0:
        print(
            f"Error: tmuxctl send-text legion:custodes rc={result.returncode}: "
            f"stdout={result.stdout.strip()[:200]} stderr={result.stderr.strip()[:200]}"
        )
        return False
    print(f"Morning session: sent via assert-instance {assertion}")
    return True


def run_morning_session() -> dict:
    """Main morning session launcher."""
    today = datetime.now().strftime("%Y-%m-%d")
    state_file = morning_state_file(today)
    state_file.parent.mkdir(parents=True, exist_ok=True)

    # Prevent double-trigger AND resurrection of an already-completed day. A bare
    # /api/morning/start (phone macro re-fire) must not relaunch a morning that is
    # already launched OR already ended — the latter produced the evening misfires:
    # the phone re-POSTed hours after the real morning ended, and an "ended" record
    # (the only guarded status before) sailed past this gate and relaunched Custodes
    # into the legion pane in the evening. Failure statuses (nas_unavailable, no_pane,
    # launch_failed) are intentionally NOT guarded so a genuine retry can proceed.
    if state_file.exists():
        data = json.loads(state_file.read_text())
        status = data.get("status")
        if status == "launched":
            print("Morning session already launched, skipping")
            return {"status": "already_launched"}
        if status == "ended":
            print("Morning session already ended for today, skipping relaunch")
            return {"status": "already_ended"}

    print(f"Morning session launcher starting: {today}")

    # Phase 0: Ensure NAS is mounted
    try:
        from nas_mount import ensure_mounted

        for share in ["/Volumes/Imperium"]:
            ok, msg = ensure_mounted(share)
            if not ok:
                send_tts(f"Morning session could not start: {msg}")
                state_file.write_text(
                    json.dumps(
                        {
                            "started_at": datetime.now().isoformat(),
                            "status": "nas_unavailable",
                            "error": msg,
                        }
                    )
                )
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
        state_file.write_text(
            json.dumps(
                {
                    "started_at": datetime.now().isoformat(),
                    "status": "no_pane",
                }
            )
        )
        return {"status": "no_pane"}

    launched = launch_in_legion(prompt, pane_id)
    if not launched:
        send_tts("Morning session failed: could not launch in legion pane")
        state_file.write_text(
            json.dumps(
                {
                    "started_at": datetime.now().isoformat(),
                    "status": "launch_failed",
                    "pane_id": pane_id,
                }
            )
        )
        return {"status": "launch_failed"}

    # Transitional state — the prompt is injected; the keepalive may start while
    # we confirm. The session is autonomous from here.
    state_file.write_text(
        json.dumps(
            {
                "started_at": datetime.now().isoformat(),
                "status": "launched",
                "pane_id": pane_id,
                "daily_thread_id": daily_thread_id,
            }
        )
    )
    print(f"Morning session launched in legion pane {pane_id}; confirming registration")

    # In-pathway self-validation. The launch is otherwise fire-and-forget: a
    # "launched" flag with no live Custodes behind it would get keepalive-injected
    # into a dead pane. Poll the instances table for a live legion=custodes,sync
    # row and flip the state file to a real active/failed (not just launched).
    confirmation = confirm_custodes_registered(pane_id=pane_id)
    if confirmation["live"]:
        update_morning_state(
            {
                "status": "active",
                "confirmed_at": datetime.now().isoformat(),
                "confirmed_instance_id": confirmation["instance_id"],
                "confirmed_pane": confirmation["tmux_pane"],
                "confirm_waited_s": confirmation["waited_s"],
            }
        )
        print(
            f"Morning session active: confirmed custodes {confirmation['instance_id']} "
            f"in pane {confirmation['tmux_pane']} after {confirmation['waited_s']}s"
        )
        return {
            "status": "active",
            "pane_id": pane_id,
            "instance_id": confirmation["instance_id"],
        }

    # The prompt was injected but no live sync Custodes registered within the
    # budget. Mark failed so the keepalive does NOT re-inject into a phantom, and
    # warn. The supervisor layer is the redundant net that also alerts the Emperor.
    update_morning_state(
        {
            "status": "failed",
            "failed_reason": "custodes_not_registered",
            "confirm_waited_s": confirmation["waited_s"],
        }
    )
    send_tts(
        "Morning session launch could not be confirmed — no live Custodes "
        "registered. Standing by for the supervisor check."
    )
    print(f"Morning session launch unconfirmed: no live custodes after {confirmation['waited_s']}s")
    return {"status": "failed", "pane_id": pane_id, "reason": "custodes_not_registered"}


def start_morning_session_background():
    """Entry point for Token-API to start the morning session in background."""
    import threading

    thread = threading.Thread(target=run_morning_session, daemon=True)
    thread.start()


if __name__ == "__main__":
    result = run_morning_session()
    print(json.dumps(result, indent=2))
