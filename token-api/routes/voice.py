"""
Voice management route module — extracted from main.py.

Owns:
- Voice profile listing and assignment (/api/voices)
- Instance voice/persona change (/api/instances/{id}/voice)
- TTS mode switching (/api/instances/{id}/tts-mode)
- Voice chat session toggling (/api/instances/{id}/voice-chat)
- Dictation state management (/api/dictation)

Does NOT own:
- TTS speech/queue/notification (routes/tts.py)
- Pedal endpoints (main.py — tightly coupled to pedal state machine)
"""

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from instance_mutation import update_instance
from personas import (
    assign_astartes_persona,
    astartes_persona_by_tts_voice,
    persona_to_profile,
    resolve_persona,
    selectable_astartes_personas,
)
from shared import (
    CUSTODES_PROFILE,
    DB_PATH,
    DICTATION_STATE,
    PEDAL_BUFFER_MS,
    PEDAL_STATE,
    VOICE_CHAT_SESSIONS,
    log_event,
)

logger = logging.getLogger("token_api")

router = APIRouter()


# ============ Late-bound Dependencies ============
# Functions from main.py that haven't been extracted yet.
# Set by init_deps() called from main.py after import.

_schedule_pedal_enter = None
_observe_work_signal = None


def init_deps(
    *,
    schedule_pedal_enter: Callable[..., None] | None = None,
    observe_work_signal: Callable[..., Awaitable[dict]] | None = None,
):
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




def _persona_response(profile: dict) -> dict:
    return {
        "slug": profile.get("name"),
        "display_name": profile.get("display_name"),
        "pane_tint": profile.get("pane_tint"),
        "chip_color": profile.get("chip_color"),
        "tts_voice": profile.get("wsl_voice"),
        "notification_sound": profile.get("notification_sound"),
    }

# ============ Voice Management Endpoints ============


@router.get("/api/voices")
async def list_voices():
    """List manually selectable persona-backed Astartes voices."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        personas = await selectable_astartes_personas(db)

    voices = []
    for persona in personas:
        profile = persona_to_profile(persona)
        wsl_voice = profile["wsl_voice"]
        short_name = wsl_voice.replace("Microsoft ", "")
        voices.append(
            {
                "voice": wsl_voice,
                "mac_voice": profile["mac_voice"],
                "short_name": short_name,
                "persona": _persona_response(profile),
                "fallback": profile["assignment_pool"] == "backup",
            }
        )
    # Surface the reserved Custodes voice (George) for display only. It is not
    # an Astartes persona, so PATCH .../voice cannot assign it to workers.
    voices.append(
        {
            "voice": CUSTODES_PROFILE["wsl_voice"],
            "mac_voice": CUSTODES_PROFILE["mac_voice"],
            "short_name": CUSTODES_PROFILE["wsl_voice"].replace("Microsoft ", ""),
            "persona": _persona_response(CUSTODES_PROFILE),
            "fallback": False,
            "reserved": "custodes",
        }
    )
    return {"voices": voices}


@router.patch("/api/instances/{instance_id}/voice")
async def change_instance_voice(instance_id: str, request: VoiceChangeRequest):
    """Change an instance's Astartes persona by selecting its seeded voice.

    Manual voice changes are persona changes: the requested voice must map to a
                   selectable seeded Astartes persona, and the instance persona is updated
    together. Collisions are rejected; this route never moves another instance.
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        target_persona = await astartes_persona_by_tts_voice(db, request.voice)
        selectable = await selectable_astartes_personas(db)
        all_voices = sorted({row["tts_voice"] for row in selectable if row.get("tts_voice")})
        if not target_persona:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid Astartes persona voice. Available: {', '.join(all_voices)}",
            )

        cursor = await db.execute(
            """
            SELECT ci.id, ci.persona_id,
                   p.tts_voice, p.notification_sound,
                   COALESCE(p.slug, 'astartes') AS profile_name, ci.name AS tab_name,
                   COALESCE(p.default_rank, 'astartes') AS current_rank
            FROM instances ci
            LEFT JOIN personas p ON p.id = ci.persona_id
            WHERE ci.id = ?
            """,
            (instance_id,),
        )
        row = await cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Instance not found")

        if row["current_rank"] != "astartes":
            raise HTTPException(
                status_code=400,
                detail="Manual voice changes are only valid for Astartes instances",
            )

        holder_cursor = await db.execute(
            """
            SELECT id, name AS tab_name
            FROM instances
            WHERE id != ?
              AND persona_id = ?
              AND status NOT IN ('stopped', 'archived')
            LIMIT 1
            """,
            (instance_id, target_persona["id"]),
        )
        holder = await holder_cursor.fetchone()
        if holder:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "voice_in_use",
                    "voice": request.voice,
                    "holder_instance_id": holder["id"],
                    "holder_name": holder["tab_name"] or holder["id"][:8],
                },
            )

        old_voice = row["tts_voice"]
        old_profile = row["profile_name"]
        profile = persona_to_profile(target_persona)
        if old_voice == request.voice and old_profile == profile["name"]:
            return {
                "status": "no_change",
                "instance_id": instance_id,
                "voice": request.voice,
                "persona": _persona_response(profile),
            }

        await update_instance(
            db,
            instance_id=instance_id,
            updates={"persona_id": target_persona["id"]},
            mutation_type="instance_updated",
            write_source="api",
            actor="voice-assignment",
        )
        await db.commit()

    await log_event(
        "instance_voice_changed",
        instance_id=instance_id,
        details={
            "old_voice": old_voice,
            "new_voice": request.voice,
            "old_profile": old_profile,
            "new_profile": profile["name"],
            "bumped": False,
        },
    )

    return {
        "status": "voice_changed",
        "instance_id": instance_id,
        "voice": request.voice,
        "persona": _persona_response(profile),
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
            """
            SELECT ci.id, ci.persona_id,
                   p.tts_voice, p.notification_sound,
                   CASE WHEN ci.interaction_mode = 'voice_chat'
                        THEN 'voice-chat' ELSE ci.notification_mode END AS tts_mode,
                   COALESCE(p.slug, 'astartes') AS profile_name,
                   p.default_rank
            FROM instances ci
            LEFT JOIN personas p ON p.id = ci.persona_id
            WHERE ci.id = ?
            """,
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
            await update_instance(
                db,
                instance_id=instance_id,
                updates={
                    "notification_mode": "silent",
                    "interaction_mode": "text",
                },
                mutation_type="instance_updated",
                write_source="api",
                actor="tts-mode",
            )
        elif mode in ("verbose", "voice-chat") and not old_voice:
            # Rehydrate from existing persona first; allocate only for persona-less rows.
            if row["persona_id"]:
                persona = await resolve_persona(db, row["persona_id"])
            else:
                persona, _ = await assign_astartes_persona(db)
            if not persona:
                persona, _ = await assign_astartes_persona(db)
            profile = persona_to_profile(persona)
            updates = {
                "notification_mode": "verbose" if mode == "voice-chat" else mode,
                "interaction_mode": "voice_chat" if mode == "voice-chat" else "text",
            }
            if not row["persona_id"]:
                updates["persona_id"] = persona["id"]
            await update_instance(
                db,
                instance_id=instance_id,
                updates=updates,
                mutation_type="instance_updated",
                write_source="api",
                actor="tts-mode",
            )
        else:
            await update_instance(
                db,
                instance_id=instance_id,
                updates={
                    "notification_mode": "verbose" if mode == "voice-chat" else mode,
                    "interaction_mode": "voice_chat" if mode == "voice-chat" else "text",
                },
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
async def toggle_voice_chat(instance_id: str, active: bool = True, pane_id: str = ""):
    """Toggle voice chat mode for an instance. Sets tts_mode='voice-chat' or restores to 'verbose'.

    Args:
        pane_id: Target pane id for send-keys. If empty, AHK script uses default.
    """
    if active:
        VOICE_CHAT_SESSIONS[instance_id] = {
            "active": True,
            "started_at": datetime.now().isoformat(),
            "pane_id": pane_id or "",
        }
        logger.info(f"Voice chat STARTED for {instance_id[:12]} (pane: {pane_id or 'default'})")
    else:
        VOICE_CHAT_SESSIONS.pop(instance_id, None)
        logger.info(f"Voice chat ENDED for {instance_id[:12]}")
    # Keep instance notification/interaction modes in sync with the legacy tts-mode API.
    updates = {
        "notification_mode": "verbose",
        "interaction_mode": "voice_chat" if active else "text",
    }
    async with aiosqlite.connect(DB_PATH) as db:
        await update_instance(
            db,
            instance_id=instance_id,
            updates=updates,
            mutation_type="instance_updated",
            write_source="api",
            actor="voice-chat",
        )
        await db.commit()
    return {"instance_id": instance_id, "voice_chat": active, "pane_id": pane_id}


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
        # the Pavlok while active, without resolving any ack. Guarded so a sink
        # failure can't 500 the high-frequency toggle after the state is set.
        try:
            await _observe_work_signal(
                source="voice-chat", kind="dictation", instance_id=instance_id
            )
        except Exception:
            logger.exception("observe_work_signal failed for dictation (voice-chat)")
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
        # the Pavlok while active, without resolving any ack. Guarded so a sink
        # failure can't 500 the high-frequency AHK toggle after the state is set.
        try:
            await _observe_work_signal(source="dictation", kind="dictation")
        except Exception:
            logger.exception("observe_work_signal failed for dictation")

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
