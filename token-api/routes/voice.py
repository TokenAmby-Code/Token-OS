"""
Voice management route module — extracted from main.py.

Owns:
- Voice profile listing and assignment (/api/voices)
- Instance voice change with collision handling (/api/instances/{id}/voice)
- TTS mode switching (/api/instances/{id}/tts-mode)
- Voice chat session toggling (/api/instances/{id}/voice-chat)
- Dictation state management (/api/dictation)

Does NOT own:
- TTS speech/queue/notification (routes/tts.py)
- Pedal endpoints (main.py — tightly coupled to pedal state machine)
"""

import logging
import random
from datetime import datetime

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from instance_mutation import sanctioned_update_instance
from shared import (
    DB_PATH,
    DICTATION_STATE,
    FALLBACK_VOICES,
    PEDAL_BUFFER_MS,
    PEDAL_STATE,
    PROFILES,
    VOICE_CHAT_SESSIONS,
    get_next_available_profile,
    log_event,
)

logger = logging.getLogger("token_api")

router = APIRouter()


# ============ Late-bound Dependencies ============
# Functions from main.py that haven't been extracted yet.
# Set by init_deps() called from main.py after import.

_schedule_pedal_enter = None
_observe_work_signal = None


def init_deps(*, schedule_pedal_enter=None, observe_work_signal=None):
    """Receive dependencies from main.py to avoid circular imports.

    Called once during app startup, before any requests are served.
    """
    global _schedule_pedal_enter, _observe_work_signal
    if schedule_pedal_enter is not None:
        _schedule_pedal_enter = schedule_pedal_enter
    if observe_work_signal is not None:
        _observe_work_signal = observe_work_signal


# ============ Pydantic Models ============


class VoiceChangeRequest(BaseModel):
    voice: str


# ============ Voice Profile Helpers ============


def find_voice_linear_probe(used_voices: set) -> str | None:
    """Find an available WSL voice using random offset + linear probe.

    Picks a random starting index in PROFILES (foreign accents), then iterates
    circularly until finding a voice not in used_voices. Falls back to
    FALLBACK_VOICES, then returns None if everything is taken.
    """
    n = len(PROFILES)
    if n > 0:
        start = random.randint(0, n - 1)
        for i in range(n):
            idx = (start + i) % n
            voice = PROFILES[idx]["wsl_voice"]
            if voice not in used_voices:
                return voice

    # Try fallback voices
    for fb in FALLBACK_VOICES:
        if fb["wsl_voice"] not in used_voices:
            return fb["wsl_voice"]

    return None


# ============ Voice Management Endpoints ============


@router.get("/api/voices")
async def list_voices():
    """List all available TTS voices from the profile pool."""
    all_profiles = PROFILES + FALLBACK_VOICES
    voices = []
    for profile in all_profiles:
        wsl_voice = profile["wsl_voice"]
        short_name = wsl_voice.replace("Microsoft ", "")
        is_fallback = profile in FALLBACK_VOICES
        voices.append(
            {
                "voice": wsl_voice,
                "mac_voice": profile["mac_voice"],
                "short_name": short_name,
                "profile_name": profile["name"],
                "fallback": is_fallback,
            }
        )
    return {"voices": voices}


@router.patch("/api/instances/{instance_id}/voice")
async def change_instance_voice(instance_id: str, request: VoiceChangeRequest):
    """Change an instance's TTS voice with collision handling.

    If the target voice is already in use by another instance, that instance
    gets bumped using random offset + linear probe to find an open slot.
    No cascade - bumped instance just finds the next available voice.
    """
    all_voices = {p["wsl_voice"] for p in PROFILES + FALLBACK_VOICES}
    if request.voice not in all_voices:
        raise HTTPException(
            status_code=400, detail=f"Invalid voice. Available: {', '.join(sorted(all_voices))}"
        )

    async with aiosqlite.connect(DB_PATH) as db:
        # Get all instances and their voices
        cursor = await db.execute("SELECT id, tts_voice, tab_name FROM claude_instances")
        rows = await cursor.fetchall()

        instance_to_voice = {row[0]: row[1] for row in rows}
        instance_to_name = {row[0]: row[2] for row in rows}
        voice_to_instance = {row[1]: row[0] for row in rows if row[1]}

        if instance_id not in instance_to_voice:
            raise HTTPException(status_code=404, detail="Instance not found")

        original_voice = instance_to_voice[instance_id]
        if original_voice == request.voice:
            return {"status": "no_change", "instance_id": instance_id, "voice": request.voice}

        # Changes to apply: [(instance_id, old_voice, new_voice), ...]
        changes = [(instance_id, original_voice, request.voice)]

        # Check for collision
        holder = voice_to_instance.get(request.voice)
        if holder and holder != instance_id:
            # Collision! Bump the holder to a new voice
            holder_old_voice = instance_to_voice[holder]

            # Build set of voices that will be in use after our change
            # (exclude original_voice since we're freeing it, include request.voice since we're taking it)
            used_after = set(voice_to_instance.keys())
            used_after.discard(original_voice)  # We're freeing this
            used_after.add(request.voice)  # We're taking this

            # Find new voice for bumped instance via linear probe
            new_voice_for_holder = find_voice_linear_probe(used_after)
            if not new_voice_for_holder:
                # All voices in use, give them the voice we just freed
                new_voice_for_holder = original_voice

            changes.append((holder, holder_old_voice, new_voice_for_holder))

        # Apply all changes to database
        for iid, _, new_voice in changes:
            await sanctioned_update_instance(
                db,
                instance_id=iid,
                updates={"tts_voice": new_voice},
                mutation_type="instance_updated",
                write_source="api",
                actor="voice-assignment",
            )
        await db.commit()

    # Log events for each change
    for iid, old_v, new_v in changes:
        name = instance_to_name.get(iid, iid[:8])
        await log_event(
            "instance_voice_changed",
            instance_id=iid,
            details={"old_voice": old_v, "new_voice": new_v, "bumped": iid != instance_id},
        )

    # Build response
    bumps = [
        {"instance_id": iid, "name": instance_to_name.get(iid, iid[:8]), "old": old_v, "new": new_v}
        for iid, old_v, new_v in changes
    ]

    return {
        "status": "voice_changed",
        "instance_id": instance_id,
        "voice": request.voice,
        "changes": bumps,
    }


# ============ TTS Mode Endpoints ============


@router.patch("/api/instances/{instance_id}/tts-mode")
async def set_instance_tts_mode(instance_id: str, request: Request):
    """Set TTS mode for an instance: verbose, muted, or silent."""
    body = await request.json()
    mode = body.get("mode", "verbose")
    if mode not in ("verbose", "muted", "silent", "voice-chat"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid mode: {mode}. Must be verbose, muted, silent, or voice-chat",
        )

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, tts_voice, notification_sound, tts_mode FROM claude_instances WHERE id = ?",
            (instance_id,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")

        old_mode = row["tts_mode"] or "verbose"
        old_voice = row["tts_voice"]
        old_sound = row["notification_sound"]

        if mode == "silent":
            # Release voice slot
            await sanctioned_update_instance(
                db,
                instance_id=instance_id,
                updates={"tts_mode": mode, "tts_voice": None, "notification_sound": None},
                mutation_type="instance_updated",
                write_source="api",
                actor="tts-mode",
            )
        elif mode in ("verbose", "voice-chat") and not old_voice:
            # Re-assign voice from pool
            cursor2 = await db.execute(
                "SELECT tts_voice FROM claude_instances WHERE status IN ('processing', 'idle') AND tts_voice IS NOT NULL"
            )
            rows = await cursor2.fetchall()
            used_voices = {r[0] for r in rows}
            profile, _ = get_next_available_profile(used_voices)
            await sanctioned_update_instance(
                db,
                instance_id=instance_id,
                updates={
                    "tts_mode": mode,
                    "tts_voice": profile["wsl_voice"],
                    "notification_sound": profile["notification_sound"],
                },
                mutation_type="instance_updated",
                write_source="api",
                actor="tts-mode",
            )
        else:
            await sanctioned_update_instance(
                db,
                instance_id=instance_id,
                updates={"tts_mode": mode},
                mutation_type="instance_updated",
                write_source="api",
                actor="tts-mode",
            )
        await db.commit()

    # Manage voice chat session based on mode transition
    if mode == "voice-chat":
        VOICE_CHAT_SESSIONS[instance_id] = {
            "active": True,
            "started_at": datetime.now().isoformat(),
        }
        logger.info(f"Voice chat STARTED for {instance_id[:12]} (via tts_mode)")
    elif old_mode == "voice-chat" and mode != "voice-chat":
        VOICE_CHAT_SESSIONS.pop(instance_id, None)
        logger.info(f"Voice chat ENDED for {instance_id[:12]} (via tts_mode)")

    await log_event("tts_mode_changed", instance_id=instance_id, details={"mode": mode})
    return {"status": "ok", "instance_id": instance_id, "mode": mode}


# ============ Voice Chat Session Endpoints ============


@router.post("/api/instances/{instance_id}/voice-chat")
async def toggle_voice_chat(instance_id: str, active: bool = True, tmux_pane: str = ""):
    """Toggle voice chat mode for an instance. Sets tts_mode='voice-chat' or restores to 'verbose'.

    Args:
        tmux_pane: Target tmux pane for send-keys (e.g., 'main:grid.2').
                   If empty, AHK script will use default.
    """
    if active:
        VOICE_CHAT_SESSIONS[instance_id] = {
            "active": True,
            "started_at": datetime.now().isoformat(),
            "tmux_pane": tmux_pane or "",
        }
        logger.info(f"Voice chat STARTED for {instance_id[:12]} (pane: {tmux_pane or 'default'})")
    else:
        VOICE_CHAT_SESSIONS.pop(instance_id, None)
        logger.info(f"Voice chat ENDED for {instance_id[:12]}")
    # Keep tts_mode column in sync
    new_mode = "voice-chat" if active else "verbose"
    async with aiosqlite.connect(DB_PATH) as db:
        await sanctioned_update_instance(
            db,
            instance_id=instance_id,
            updates={"tts_mode": new_mode},
            mutation_type="instance_updated",
            write_source="api",
            actor="voice-chat",
        )
        await db.commit()
    return {"instance_id": instance_id, "voice_chat": active, "tmux_pane": tmux_pane}


@router.get("/api/instances/{instance_id}/voice-chat")
async def get_voice_chat_status(instance_id: str):
    """Check if instance is in voice chat mode."""
    session = VOICE_CHAT_SESSIONS.get(instance_id)
    return {"active": session is not None, "session": session}


@router.post("/api/instances/{instance_id}/voice-chat/listening")
async def toggle_listening(instance_id: str, active: bool = True):
    """Toggle listening (dictation/mic) state. Delegates to global dictation state."""
    DICTATION_STATE["active"] = active
    DICTATION_STATE["updated_at"] = datetime.now().isoformat()
    logger.info(
        f"Dictation {'ON' if active else 'OFF'} (via voice-chat/listening for {instance_id[:12]})"
    )
    if active and _observe_work_signal:
        # Dictation start is live work — DEFER: it stalls enforcement and holds
        # the Pavlok while active, without resolving any ack.
        await _observe_work_signal(source="voice-chat", kind="dictation", instance_id=instance_id)
    return {"instance_id": instance_id, "listening": active}


# ============ Dictation Endpoints ============


@router.post("/api/dictation")
async def set_dictation_state(active: bool):
    """Set global dictation (Wispr Flow) state. Called by AHK on every toggle."""
    DICTATION_STATE["active"] = active
    DICTATION_STATE["updated_at"] = datetime.now().isoformat()
    logger.info(f"Dictation {'ON' if active else 'OFF'}")

    if active and _observe_work_signal:
        # Dictation start is live work — DEFER: it stalls enforcement and holds
        # the Pavlok while active, without resolving any ack.
        await _observe_work_signal(source="dictation", kind="dictation")

    # When dictation ends, flush any queued pedal enter after buffer delay
    if not active and PEDAL_STATE["enter_queued"] and _schedule_pedal_enter:
        _schedule_pedal_enter(PEDAL_BUFFER_MS)

    return {"active": active}


@router.get("/api/dictation")
async def get_dictation_state():
    """Get current dictation state. Used by AHK for explicit on/off decisions."""
    # Also report if any voice chat session is active
    voice_chat_instance = None
    for instance_id, session in VOICE_CHAT_SESSIONS.items():
        if session.get("active"):
            voice_chat_instance = instance_id
            break
    return {
        "active": DICTATION_STATE["active"],
        "updated_at": DICTATION_STATE["updated_at"],
        "voice_chat_instance": voice_chat_instance,
    }
