---
title: Voice Chat Design
project: token-api
status: active
created: 2026-03-02
---

# Voice Chat for Claude Code

## Problem

Claude Code's interaction model is text-in/text-out. The existing TTS system is notification-only (stop hooks, alerts). There's no conversational voice loop — Claude can't speak responses and receive spoken input in a continuous flow.

## User Setup

- **STT**: Wispr Flow (local, high-quality) + lavalier mic + Bluetooth ring dictation remote
- **TTS**: Token-API system — Windows SAPI (9 accent-profiled voices) with Mac `say` fallback, queue-based
- **Discord**: Fully operational message bot (discord.js v14, admin perms, MessageContent intent, SSE streaming, ask/wait/poll). NO voice capability currently (no @discordjs/voice, no opus).
- **Personas**: Mechanicus (autonomous), Custodes (conversational), Inquisition (Minimax fleet)

## Requirements

1. Claude speaks responses via TTS (conversational, not just notifications)
2. User responds by voice (dictation → text)
3. Continuous back-and-forth loop with natural turn-taking
4. Multi-persona support (different voices per persona)
5. Optional headless mode (no terminal required)
6. Chat history visible in TUI when needed

---

## Phase 1: AskUserQuestion + TTS Loop — IMPLEMENTED 2026-03-03

### Architecture (Actual)

```
┌─────────────────────────────────────────────────────────────────────┐
│ Claude Code Session                                                 │
│                                                                     │
│  1. Claude processes input (silent — no TTS during tool use)        │
│  2. Claude calls AskUserQuestion (blocking)                         │
│     ↓ PreToolUse hook fires                                         │
│     ↓ generic-hook.sh → POST token-api /api/hooks/PreToolUse       │
│     ↓ Token-API: extracts question text → queue_tts()               │
│     ↓ Token-API: returns {local_exec: "AHK command"} in response    │
│     ↓ generic-hook.sh: parses local_exec, eval's on WSL             │
│     ↓ AHK.exe runs voice-select-other.ahk (navigate to "Other")    │
│  3. User hears question via TTS, dictates via Wispr Flow            │
│  4. User presses Enter → AHK intercept: stop dictation → wait →    │
│     submit → restart dictation                                      │
│  5. → back to step 1                                                │
└─────────────────────────────────────────────────────────────────────┘
```

### Key Mechanics

**Hook-driven TTS (no manual curl needed):**
- PreToolUse handler extracts question text from `tool_input["questions"]`
- Calls `queue_tts(session_id, question_text)` directly — uses instance's assigned voice
- Claude can ALSO call `POST /api/notify/tts` for non-blocking narration between questions

**local_exec pattern (no satellite needed):**
- Token-API returns `local_exec` field in PreToolUse response
- `generic-hook.sh` parses it with `jq` and `eval`s on WSL
- WSL invokes Windows AHK.exe via `/mnt/c/` mount
- AHK.exe needs Windows paths — `wslpath -w` converts WSL paths

**AHK voice-select-other.ahk:**
- `WinActivate` focuses Windows Terminal (handles tabbed-out user)
- `Down 6` overshoots to bottom of option list (no wrap)
- `Up 1` lands on "Other" (second from bottom, above "Chat about this")
- Registers a scoped `$Enter` hotkey (active only during AskUserQuestion)
- On Enter: stop Wispr (`Ctrl+Win+Space` hold pattern) → wait 1.5s → submit → restart Wispr
- Hotkey disables itself after submit — normal Enter restored

**Wispr Flow integration:**
- Wispr stays on passively — always listening between questions
- AHK doesn't toggle Wispr on question arrival (it's already on)
- Enter remap handles the stop/submit/restart cycle
- `Ctrl+Win down` → `Sleep(250)` → `Space` → release (matches ring-remap tap pattern)

### What Was Built

| Component | Status | Notes |
|-----------|--------|-------|
| TTS system (SAPI + Mac) | Pre-existing | Queue-based, 9 voice profiles |
| Hook-driven TTS on AskUserQuestion | **Built** | PreToolUse extracts question text → queue_tts() |
| local_exec in PreToolUse response | **Built** | New generic pattern — Token-API returns commands, hook executes |
| generic-hook.sh local_exec parsing | **Built** | `jq -r '.local_exec'` → `eval` in background |
| AHK voice-select-other.ahk | **Built** | Navigation + scoped Enter remap with Wispr integration |
| Voice conversation skill | **Built** | `~/.claude/skills/voice-chat.md` drives the loop |
| AskUserQuestion PreToolUse matcher | **Built** | In `~/.claude/settings.json` |
| Voice chat state (VOICE_CHAT_SESSIONS) | **Built** | In-memory dict, keyed by instance_id |

### Key Architectural Decisions

1. **No satellite needed** — original plan routed through token-satellite on WSL. Eliminated by local_exec pattern: Token-API returns the command, generic-hook.sh runs it directly on WSL.
2. **Hook-driven TTS, not skill-driven** — original plan had Claude making manual TTS curl calls. Moved TTS into the PreToolUse handler so it's automatic and invisible to the agent.
3. **Scoped Enter remap** — Enter hotkey only active during AskUserQuestion, disables after submit. Prevents interfering with normal terminal use.
4. **wslpath -w for path conversion** — Token-API runs on Mac, `os.path.expanduser` gives Mac paths. AHK needs Windows paths. `wslpath -w` converts WSL → Windows UNC paths.

### Learnings for Phase 2 (Discord)

1. **Hook-driven TTS is the right pattern** — extracting text from tool payloads and auto-speaking eliminates agent-side boilerplate. Discord voice should reuse this: intercept responses and auto-speak them.
2. **local_exec is extensible** — any hook can now return side-effect commands. Discord bot could use this for audio playback triggers.
3. **Turn-taking via blocking prompts works** — AskUserQuestion's blocking nature IS the conversation loop. Discord equivalent: bot waits for voice activity end → transcribes → forwards.
4. **TTS CLI opportunity** — current pattern is `curl -sf -X POST .../api/notify/tts -d '{...}'`. A `talk "message"` CLI tool or `token-ping` integration would streamline agent-side TTS calls for narration between questions.

### Remaining TODO

- [ ] Auto-start dictation after voice chat activation (currently manual)
- [ ] `talk` CLI tool or token-ping integration for streamlined TTS
- [ ] Persist voice chat state across token-api restarts (currently in-memory)
- [ ] Handle multiple terminal tabs (WinActivate focuses terminal but can't switch tabs)

### Pros
- Ships fast — built in one session
- Wispr Flow handles all STT complexity
- Natural turn-taking via blocking AskUserQuestion
- Hook-driven = invisible to the agent (no boilerplate)
- local_exec = reusable pattern for any hook side-effects

### Cons
- Requires terminal focus (WinActivate helps but can't target specific tabs)
- Single-persona (instance voice profile)
- In-memory voice chat state lost on restart
- AHK timing is semi-fragile (500ms sleep for UI render)

---

## Approach 2: Discord Voice Channel (Phase 2 — Mechanicus Project)

### Architecture

```
┌──────────────┐     ┌─────────────────────┐     ┌──────────────────┐
│ Discord App  │────▶│ Discord Bot (daemon) │────▶│ Token-API        │
│ Voice Channel│◀────│ + @discordjs/voice   │◀────│ Conversation Eng │
│              │     │ + Whisper STT        │     │                  │
│ User speaks  │     │ Transcribe → forward │     │ Claude processes │
│ Bot speaks   │     │ Receive → TTS → play │     │ → response text  │
└──────────────┘     └─────────────────────┘     └──────────────────┘
```

### Key Mechanics

**Bot joins voice channel:**
- New `@discordjs/voice` dependency + opus codec + sodium
- Bot connects to designated voice channel
- Receives user audio stream → pipes to Whisper STT
- Transcribed text forwarded to Token-API conversation engine

**Bot speaks responses:**
- Receive response text from conversation engine
- Run through TTS (SAPI/Mac `say`) → audio file
- Play audio file into Discord voice channel via `AudioPlayer`

**Multi-persona:**
- Each persona (Mechanicus, Custodes, Inquisition) is a separate bot account
- Each has distinct voice profile (already mapped in TTS profiles)
- User talks to specific persona by joining their voice channel or @mentioning

**Headless advantage:**
- No terminal needed — user speaks into Discord from phone/desktop/anywhere
- Chat history appears in Discord text channel alongside voice
- TUI gets a "voice chat" panel showing recent transcript

### New Components

| Component | Complexity |
|-----------|------------|
| @discordjs/voice integration | Medium — well-documented library |
| Opus/sodium native deps | Low — npm install |
| VoiceStates gateway intent | Low — config change |
| Whisper STT pipeline | Medium — local Whisper or API |
| Audio file generation from TTS | Medium — pipe SAPI/say output to file |
| Conversation engine (API) | Medium — new endpoint managing conversation state |
| TUI voice chat panel | Low — new panel page |

### Pros
- Headless — talk from anywhere (phone, desktop, walking around)
- Multi-persona native — each bot has its own voice channel presence
- Chat history in Discord + TUI
- Thematically perfect (Mechanicus lives in Discord)
- Fun factor is high

### Cons
- Significant new code in discord-daemon
- STT latency (Whisper processing)
- Audio quality through Discord compression
- More moving parts (voice connection stability)

---

## Approach 3: Hybrid (Recommended)

Ship Phase 1 immediately. Write Phase 2 spec for autonomous agents.

### Phase 1 Deliverables (This Week)
1. **Voice conversation skill** — Claude Code skill that drives the TTS + AskUserQuestion loop
2. **TTS hook on AskUserQuestion** — hook config that speaks question text via TTS
3. **Auto-fill script** — background process handling the "Other" selection + Enter buffering
4. **Non-blocking TTS calls** — Claude narrates freely between questions

### Phase 2 Spec (Mechanicus Overnight)
1. **@discordjs/voice integration** — bot joins/leaves voice channels
2. **Whisper STT pipeline** — transcribe Discord audio stream
3. **Audio TTS output** — generate audio files from existing TTS for playback
4. **Conversation engine endpoint** — `POST /api/voice/conversation` managing state
5. **TUI voice panel** — transcript display in info panel rotation

### Shared Components
Both phases use:
- Same TTS voice profiles and queue
- Same conversation state management
- Same response generation (Claude API or Claude Code session)

### Minimax Token Sink
The Inquisition persona could use Minimax for:
- Secondary STT processing (redundancy/comparison)
- Voice synthesis (Minimax has TTS APIs — alternative voice generation)
- Conversation summarization (cheaper than Claude for transcript processing)

---

## Open Questions

1. **AHK or terminal-native?** The auto-fill script for Phase 1 — is AHK the right tool or should it be a terminal-native solution?
2. **Whisper deployment** — local whisper.cpp, OpenAI Whisper API, or Minimax STT for Phase 2?
3. **Conversation state** — should voice chat sessions persist as session documents, or a new `voice_conversations` table?
4. **Wake word** — should there be a "hey Claude" trigger or is the Bluetooth ring sufficient for Phase 1?
5. **Multiple simultaneous personas** — can user talk to Custodes and Mechanicus in same voice channel, or separate channels?

---

## Activity Log

### 2026-03-02 18:30 -- voice-chat-brainstorm
Initial brainstorming session. Explored existing Discord integration (message-only, no voice),
TTS system (mature, 9 SAPI voices + Mac fallback), and user's STT setup (Wispr Flow + lav mic + BT ring).
Proposed 3 approaches: TTS loop (immediate), Discord voice (autonomous project), hybrid (recommended).
Key insight: AskUserQuestion blocking behavior IS the turn-taking mechanism for Phase 1.
Key insight: Discord voice is architecturally correct long-term because personas already live there.

### 2026-03-03 12:40 -- phase-1-implementation
Built Phase 1 in a single session. Major architectural divergences from plan:
- **Eliminated satellite proxy** — original plan routed Mac → satellite → AHK. Replaced with `local_exec` pattern: Token-API returns command in PreToolUse response, generic-hook.sh eval's it on WSL.
- **Hook-driven TTS** — moved TTS from skill-side curl calls into PreToolUse handler. `queue_tts()` called directly from hook handler, invisible to agent.
- **wslpath -w discovery** — AHK.exe needs Windows paths but Token-API runs on Mac. `os.path.expanduser` gave Mac paths. Fixed with `wslpath -w` conversion in the local_exec command.
- **Scoped Enter hotkey** — AHK registers `$Enter` only during AskUserQuestion, disables after submit. Prevents global Enter interference.
- **Wispr hold pattern** — `^#{Space}` shortcut didn't reliably toggle Wispr. Fixed by matching ring-remap pattern: `{LCtrl down}{LWin down}` → Sleep(250) → `{Space}{LWin up}{LCtrl up}`.
- **"Other" navigation** — Down 6 + Up 1 reliably selects "Other" regardless of option count. "Other" is always second from bottom (above "Chat about this"). List doesn't wrap.
- User noted future `talk` CLI or token-ping integration to streamline TTS calls from agents.
