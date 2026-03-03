# Voice Chat Phase 1 — Implementation Plan

> **Status: IMPLEMENTED 2026-03-03** — Phase 1 is live. Architecture diverged from original plan (see notes below). Phase 2 (Discord voice) remains planned.

**Goal:** Enable continuous voice conversation with Claude Code — Claude speaks via TTS, user responds via Wispr Flow dictation, turn-taking managed through AskUserQuestion with hook-triggered AHK keystroke injection.

**Architecture (Actual):** Hook-driven, no polling. When Claude calls AskUserQuestion, the hook chain fires: `generic-hook.sh` → Mac token-api `/api/hooks/PreToolUse` → Token-API extracts question text for TTS + returns `local_exec` command → `generic-hook.sh` eval's command on WSL → AHK.exe runs one-shot script. **No satellite proxy needed** — `local_exec` pattern eliminated the middleman.

**Tech Stack:** Claude Code skills (markdown), AHK v2.0 (persistent + one-shot), token-api hook handlers, Bash hooks, `wslpath` for path conversion

---

## Context for Implementer

### Existing Infrastructure
- **TTS**: Token-API on Mac (`100.95.109.23:7777`). WSL satellite SAPI voices (primary) with Mac `say` fallback. Queue-based, 9 accent profiles assigned per instance.
- **Dictation**: Wispr Flow on Windows, activated via `Ctrl+Win+Space`. Bluetooth ring right button toggles it. Left double-tap sends Enter. See `~/Scripts/ahk/ring-remap.ahk`.
- **Hooks**: All hooks dispatch through `~/.claude/hooks/generic-hook.sh` → `POST http://100.95.109.23:7777/api/hooks/{ActionType}`. PreToolUse is synchronous; all others fire-and-forget.
- **Stop hook TTS**: Already extracts transcript tail and speaks a summary via TTS on turn end.
- **Satellite**: `token-satellite.py` on WSL port 7777. Already executes Windows commands via `CMD_EXE` and `POWERSHELL_EXE` subprocess calls. Pattern for AHK execution is identical.
- **AHK v2.0**: Installed at `/mnt/c/Program Files/AutoHotkey/v2/AutoHotkey.exe`. Scripts at `~/Scripts/ahk/`.

### Key Files
- `~/.claude/settings.json` — Hook configuration (lines 15-129)
- `~/.claude/hooks/generic-hook.sh` — Unified hook dispatcher
- `~/Scripts/ahk/ring-remap.ahk` — Ring button handlers + Wispr integration (791 lines)
- `~/Scripts/token-api/main.py` — Token-API server (~5000 lines)
- `~/Scripts/token-api/token-satellite.py` — WSL satellite server (~800 lines)
- `~/.claude/skills/` — Skills directory

### Execution Chain (Actual — Implemented 2026-03-03)

```
Claude calls AskUserQuestion
  → PreToolUse hook fires (AskUserQuestion matcher in settings.json)
  → generic-hook.sh forwards to Mac token-api
  → Mac token-api /api/hooks/PreToolUse handler
  → Sees tool_name == "AskUserQuestion" + session_id in VOICE_CHAT_SESSIONS
  → Extracts question text from tool_input["questions"] → queue_tts()
  → Returns {local_exec: 'AHK_EXE "$(wslpath -w script)"'} in response
  → generic-hook.sh parses local_exec with jq, eval's in background
  → WSL runs: /mnt/c/.../AutoHotkey.exe \\wsl.localhost\...\voice-select-other.ahk
  → AHK: WinActivate terminal, Down 6 + Up 1 (navigate to "Other")
  → AHK: enables scoped $Enter hotkey, stays resident
  → User dictates via Wispr Flow (already on passively)
  → User presses Enter → AHK: stop Wispr → wait → submit → restart Wispr → disable hotkey
```

No fallback needed — AskUserQuestion DOES trigger PreToolUse hooks (confirmed).

---

### Task 1: ~~Add `/ahk/execute` Endpoint to WSL Satellite~~ → SKIPPED (local_exec replaced satellite)

> **Not needed.** The `local_exec` pattern eliminated the satellite proxy entirely. Token-API returns the AHK command in the PreToolUse response, and `generic-hook.sh` runs it directly on WSL.

**Files:**
- Modify: `~/Scripts/token-api/token-satellite.py`
- Create: `~/Scripts/ahk/voice-select-other.ahk`

**Step 1: Create the one-shot AHK script**

Create `~/Scripts/ahk/voice-select-other.ahk` — a minimal AHK v2 script that sends keystrokes to select "Other" in AskUserQuestion and exits:

```ahk
#Requires AutoHotkey v2.0
#SingleInstance Force

; One-shot: select "Other" in Claude Code AskUserQuestion prompt
; Called by token-satellite when voice chat is active
; AskUserQuestion shows 2 options + "Other" at bottom
; Down x2 → Other, Enter → select it

Sleep(300)          ; Wait for AskUserQuestion UI to render
Send("{Down 2}")    ; Move past 2 options to "Other"
Sleep(50)
Send("{Enter}")     ; Select "Other"
ExitApp
```

**Step 2: Verify the AHK script runs standalone**

From WSL, execute:
```bash
"/mnt/c/Program Files/AutoHotkey/v2/AutoHotkey.exe" /home/token/Scripts/ahk/voice-select-other.ahk
```
Expected: Sends keystrokes to the focused window. No errors.

**Step 3: Add the satellite endpoint**

Add to `token-satellite.py` after the existing `/tts/skip` endpoint (around line 755):

```python
AHK_EXE = "/mnt/c/Program Files/AutoHotkey/v2/AutoHotkey.exe"
AHK_SCRIPTS_DIR = Path.home() / "Scripts" / "ahk"

class AhkRequest(BaseModel):
    script: str  # Script filename (e.g., "voice-select-other.ahk")
    args: list[str] = []  # Optional arguments

@app.post("/ahk/execute")
async def execute_ahk(req: AhkRequest):
    """Execute a one-shot AHK v2 script. AHK is a dumb executor — token-api decides when to call."""
    script_path = AHK_SCRIPTS_DIR / req.script
    if not script_path.exists():
        raise HTTPException(status_code=404, detail=f"AHK script not found: {req.script}")
    # Security: only allow scripts in the ahk directory
    if not script_path.resolve().is_relative_to(AHK_SCRIPTS_DIR.resolve()):
        raise HTTPException(status_code=403, detail="Script path escapes ahk directory")
    try:
        cmd = [AHK_EXE, str(script_path)] + req.args
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return {"ok": True, "exit_code": result.returncode, "stderr": result.stderr[:200] if result.stderr else None}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

**Step 4: Test the endpoint**

```bash
# From WSL, test locally
curl -X POST http://localhost:7777/ahk/execute \
  -H "Content-Type: application/json" \
  -d '{"script": "voice-select-other.ahk"}'
# Expected: {"ok": true, "exit_code": 0, "stderr": null}
```

**Step 5: Commit**

```bash
cd ~/Scripts
git add ahk/voice-select-other.ahk token-api/token-satellite.py
git commit -m "feat: add /ahk/execute satellite endpoint + voice-select-other script"
```

---

### Task 2: Add Voice Chat Hook Handler to Mac Token-API — DONE ✓

> **Divergence:** No satellite proxy. Handler does two things: (1) extracts question text → `queue_tts()`, (2) returns `local_exec` with AHK command. See `main.py` around line 7996.

**Files:**
- Modify: `~/Scripts/token-api/main.py` (hook handler + voice chat state)

**Step 1: Add voice chat state tracking**

Near the top of main.py, after other state dicts:

```python
# Voice chat state — tracks which instances are in voice conversation mode
VOICE_CHAT_SESSIONS = {}  # instance_id -> {"active": True, "started_at": str}
```

**Step 2: Add voice chat toggle endpoint**

```python
@app.post("/api/instances/{instance_id}/voice-chat")
async def toggle_voice_chat(instance_id: str, active: bool = True):
    """Toggle voice chat mode for an instance."""
    if active:
        VOICE_CHAT_SESSIONS[instance_id] = {
            "active": True,
            "started_at": datetime.now().isoformat()
        }
    else:
        VOICE_CHAT_SESSIONS.pop(instance_id, None)
    return {"instance_id": instance_id, "voice_chat": active}

@app.get("/api/instances/{instance_id}/voice-chat")
async def get_voice_chat_status(instance_id: str):
    """Check if instance is in voice chat mode."""
    session = VOICE_CHAT_SESSIONS.get(instance_id)
    return {"active": session is not None, "session": session}
```

**Step 3: Add AskUserQuestion handling in the PreToolUse hook handler**

Find the existing `/api/hooks/PreToolUse` handler in main.py. Add a check for AskUserQuestion tool calls from voice-chat-active instances:

```python
# Inside the PreToolUse hook handler, after existing logic:

tool_name = payload.get("tool_name", "")
if tool_name == "AskUserQuestion":
    # Find instance by session_id or PID from the hook payload
    instance_id = _resolve_instance_from_hook(payload)
    if instance_id and instance_id in VOICE_CHAT_SESSIONS:
        # Proxy to satellite to execute AHK keystroke injection
        try:
            satellite_url = f"http://{DESKTOP_CONFIG['ip']}:{DESKTOP_CONFIG['port']}/ahk/execute"
            resp = httpx.post(satellite_url, json={"script": "voice-select-other.ahk"}, timeout=5)
            logger.info(f"Voice chat: triggered AHK select-other for {instance_id}: {resp.status_code}")
        except Exception as e:
            logger.warning(f"Voice chat: satellite AHK call failed: {e}")
```

The implementer will need to read the existing PreToolUse hook handler to understand the exact structure and where to insert this logic. The key function is `_resolve_instance_from_hook(payload)` which maps the hook's `session_id` or `pid` field to an instance ID — this pattern likely already exists for other hook handling.

**Step 4: Test with curl**

```bash
# First, enable voice chat for a test instance
curl -X POST "http://localhost:7777/api/instances/test-123/voice-chat?active=true"

# Then simulate a PreToolUse hook for AskUserQuestion
curl -X POST http://localhost:7777/api/hooks/PreToolUse \
  -H "Content-Type: application/json" \
  -d '{"tool_name": "AskUserQuestion", "session_id": "test", "pid": 12345}'
# Expected: 200 OK, satellite receives /ahk/execute call
```

**Step 5: Commit**

```bash
cd ~/Scripts/token-api
git add main.py
git commit -m "feat: voice chat state + AskUserQuestion hook → satellite AHK dispatch"
```

---

### Task 3: Create the Voice Chat Skill — DONE ✓

> **Divergence:** Skill no longer instructs Claude to make manual TTS curl calls — TTS is hook-driven and automatic. Skill focuses on conversation loop behavior and voice-chat registration.

**Files:**
- Create: `~/.claude/skills/voice-chat.md`

**Step 1: Write the skill file**

```markdown
---
name: voice-chat
description: Enter voice conversation mode with TTS responses and dictation input. Use when the user wants to have a spoken back-and-forth conversation.
---

# Voice Conversation Mode

You are in voice conversation mode. The user hears your responses via TTS and speaks their replies via dictation (Wispr Flow + Bluetooth ring).

## Setup (run once at start)

Register this instance as voice-chat-active so the hook system auto-selects "Other" on AskUserQuestion:

\`\`\`bash
curl -sf -X POST "http://100.95.109.23:7777/api/instances/$TOKEN_API_INSTANCE_ID/voice-chat?active=true"
\`\`\`

## Rules

1. **Speak every response**: Before each AskUserQuestion, call TTS with a concise spoken version (1-3 sentences):

\`\`\`bash
curl -sf -X POST http://100.95.109.23:7777/api/notify/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "YOUR_SPOKEN_RESPONSE", "priority": true}'
\`\`\`

2. **Use AskUserQuestion for every turn**: After speaking, ask a follow-up question. Design with exactly 2 options:
   - Option 1: A contextual suggestion (e.g., "Tell me more about X")
   - Option 2: A pivot option (e.g., "Switch topics")
   - The user will dictate freely via auto-selected "Other" (handled by hooks)

3. **Keep spoken responses concise**: 1-3 sentences for TTS. Details go in terminal text.

4. **Conversational tone**: No markdown in TTS text. No code. Use contractions. Speak naturally.

5. **Silent tool use**: If you need to read files or run commands, do it without TTS narration. Only speak your conversational response.

6. **End signal**: When the user says "end voice chat", "stop talking", or similar — deregister and stop:

\`\`\`bash
curl -sf -X POST "http://100.95.109.23:7777/api/instances/$TOKEN_API_INSTANCE_ID/voice-chat?active=false"
\`\`\`

## Conversation Loop

Every turn:
1. Process user input (silently)
2. Formulate spoken response (1-3 sentences)
3. Call TTS via Bash curl
4. Call AskUserQuestion with follow-up (hooks handle the "Other" auto-select)
5. Receive user's dictated response
6. Back to step 1

## Starting

Speak a greeting, then ask what the user wants to discuss:

\`\`\`bash
curl -sf -X POST http://100.95.109.23:7777/api/notify/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "Voice chat is active. What would you like to talk about?", "priority": true}'
\`\`\`

Then AskUserQuestion: "What would you like to discuss?" with options like "Current project" / "Brainstorm something new".
```

**Step 2: Verify**

```bash
head -5 ~/.claude/skills/voice-chat.md
```
Expected: frontmatter with `name: voice-chat`

**Step 3: Commit**

```bash
git add ~/.claude/skills/voice-chat.md
git commit -m "feat: add voice-chat skill for TTS conversation loop"
```

---

### Task 4: Add PreToolUse Hook Matcher for AskUserQuestion — DONE ✓

> **Confirmed working.** AskUserQuestion DOES trigger PreToolUse hooks. No fallback needed.

**Files:**
- Modify: `~/.claude/settings.json`

**Step 1: Add AskUserQuestion matcher**

Add to the `PreToolUse` array in `~/.claude/settings.json` (after the "Task" matcher, line ~103):

```json
{
  "matcher": "AskUserQuestion",
  "hooks": [
    {
      "type": "command",
      "command": "HOOK_ACTION_TYPE=PreToolUse bash ~/.claude/hooks/generic-hook.sh"
    }
  ]
}
```

This uses the same generic-hook.sh as all other PreToolUse hooks. The Mac server's hook handler (Task 2) will see `tool_name: "AskUserQuestion"` and dispatch to the satellite.

**Step 2: Test**

In a Claude Code session, call AskUserQuestion and check hook debug log:

```bash
tail -5 ~/.claude/logs/hook-debug.log
```

Expected: Entry showing `PreToolUse: {"tool_name":"AskUserQuestion",...}` if hooks trigger.

**Step 3: If hooks don't trigger for AskUserQuestion**

Add a fallback to the skill: before each AskUserQuestion, Claude also calls the satellite directly:

```bash
curl -sf -X POST "http://100.66.10.74:7777/ahk/execute" \
  -H "Content-Type: application/json" \
  -d '{"script": "voice-select-other.ahk"}'
```

Update the skill's conversation loop to include this step. The timing will need tuning — the AHK script has a 300ms sleep to wait for the UI to render, but if called before AskUserQuestion, that delay needs to account for Claude Code's rendering time.

**Step 4: Commit**

```bash
git add ~/.claude/settings.json
git commit -m "feat: add AskUserQuestion PreToolUse hook matcher for voice chat"
```

---

### Task 5: Wire Voice Chat TTS to Instance Voice Profile — DONE ✓ (via hook)

> **Divergence:** Done automatically. `queue_tts(session_id, text)` already looks up the instance's `tts_voice` column. No endpoint changes needed.

**Files:**
- Modify: `~/Scripts/token-api/main.py` (TTS endpoint)

**Step 1: Read existing TTS endpoint**

Read the `/api/notify/tts` handler in main.py. Understand how it currently selects voices and routes to the TTS queue.

**Step 2: Add optional instance_id parameter**

If the endpoint doesn't already accept `instance_id` for voice lookup, add it. When provided, look up the instance's `tts_voice` column and use that voice profile:

```python
# In TTS request model, add:
instance_id: Optional[str] = None

# In handler, if instance_id provided:
# Look up instance's tts_voice → derive profile → use that voice for this TTS call
```

**Step 3: Update skill curl command**

```bash
curl -sf -X POST http://100.95.109.23:7777/api/notify/tts \
  -H "Content-Type: application/json" \
  -d "{\"text\": \"YOUR_RESPONSE\", \"instance_id\": \"$TOKEN_API_INSTANCE_ID\", \"priority\": true}"
```

**Step 4: Test**

```bash
agents-db query "SELECT id, tab_name, tts_voice FROM claude_instances WHERE status='active' LIMIT 3"
```

Then send TTS with an instance_id that has a voice assigned. Verify it speaks in the correct voice.

**Step 5: Commit**

```bash
git add main.py
git commit -m "feat: wire voice chat TTS to instance voice profile"
```

---

### Task 6: End-to-End Integration Test — DONE ✓

> **Tested live in session 2026-03-03.** Iterative testing with user. Key findings below.

**Step 1: Restart services**

```bash
token-restart --wsl-only   # Restart satellite with new /ahk/execute endpoint
token-restart --mac-only   # Restart Mac server with voice chat handlers
```

**Step 2: Invoke the skill**

In Claude Code, type: `/voice-chat` or "let's have a voice conversation"

**Step 3: Verify the full chain**

Expected sequence:
1. Skill registers instance as voice-chat-active (curl to Mac)
2. Claude speaks greeting via TTS
3. Claude calls AskUserQuestion
4. Hook fires → Mac → satellite → AHK auto-selects "Other"
5. User dictates via Wispr Flow (ring right button)
6. User submits via ring left double-tap (Enter)
7. Claude processes → TTS → AskUserQuestion → hook → AHK → repeat

**Step 4: Test edge cases**

- **Timing**: Does the 300ms AHK sleep give enough time for AskUserQuestion UI to render?
- **Early Enter**: User presses Enter before AskUserQuestion appears — existing ring-remap buffer handles this.
- **Hook fallback**: If AskUserQuestion doesn't trigger PreToolUse, does the skill's inline satellite call work?
- **End voice chat**: Say "end voice chat" — verify instance deregisters and loop stops.

**Step 5: Document findings**

Update `docs/plans/2026-03-02-voice-chat-design.md` activity log with results.

---

## Open Items for Phase 2 (Discord Voice — Mechanicus Spec)

Not part of this plan. Spec for autonomous overnight implementation:

1. Add `@discordjs/voice` + `@discordjs/opus` + `sodium-native` to discord-daemon
2. Add `VoiceStates` gateway intent to discord-client.js
3. Voice channel join/leave commands
4. Whisper STT pipeline (local whisper.cpp or API)
5. Audio file generation from SAPI/Mac TTS for Discord playback
6. TUI voice chat panel (new info panel page)
7. Multi-persona voice routing (Mechanicus/Custodes/Inquisition bot accounts)
