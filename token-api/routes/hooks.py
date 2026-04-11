"""
Claude Code Hook Handlers — extracted from main.py.

Owns:
- Hook lifecycle handlers (SessionStart, Stop, PromptSubmit, etc.)
- Hook dispatch endpoint (/api/hooks/{action_type})
- Discord output mirroring

Uses lazy imports for main.py functions to avoid circular dependencies.
"""

import os
import re
import json
import time
import uuid
import asyncio
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiosqlite
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from shared import (
    DB_PATH, DEFAULT_SESSIONS_DIR, MARS_SESSIONS_DIR,
    PROFILES, FALLBACK_VOICES, ULTIMATE_FALLBACK,
    get_next_available_profile,
    DESKTOP_STATE, DISCORD_DAEMON_URL,
    log_event,
)
from routes.tts import queue_tts, play_sound

logger = logging.getLogger("token_api")

router = APIRouter()


# ============ Lazy Import ============
# main.py imports routes/hooks.py at startup (for the router).
# We import main at call-time to avoid circular imports.

_main_module = None


def _main():
    """Lazy import of main module — safe at request time."""
    global _main_module
    if _main_module is None:
        import main as m
        _main_module = m
    return _main_module


# ============ Hook Models ============

class HookResponse(BaseModel):
    """Standard response for hook handlers."""
    success: bool = True
    action: str
    details: Optional[dict] = None


class PreToolUseResponse(BaseModel):
    """Response for PreToolUse hooks that can block operations."""
    permissionDecision: Optional[str] = None  # "allow" or "deny"
    permissionDecisionReason: Optional[str] = None


# ============ Hook Handler State ============
# Debouncing for PostToolUse to avoid excessive API calls
_post_tool_debounce: dict = {}  # session_id -> last_call_time

# Tracks background Task subagents still awaiting result delivery.
_pending_background_tasks: dict = {}  # session_id -> count

# Tracks recent evaluator nudges to prevent re-evaluation loops on stop.
_recently_nudged: dict[str, float] = {}
NUDGE_COOLDOWN_SECONDS = 300  # 5 minutes

# Tracks stop-hook self-evaluation blocks. When a golden_throne or sync instance
# stops, StopValidate blocks once with a self-eval prompt. If the agent stops again
# (self-eval complete, chose not to continue), the second stop passes through.
_self_eval_pending: dict[str, float] = {}  # session_id -> timestamp of block
SELF_EVAL_TTL_SECONDS = 120  # expire stale blocks after 2 minutes


# Legion → Discord bot name mapping
_LEGION_BOT_MAP = {
    "custodes": "custodes",
    "mechanicus": "mechanicus",
    "astartes": "mechanicus",
    "inquisition": "inquisition",
}


# ============ Claude Code Hook Handlers ============
# Centralized handling for all Claude Code hooks
# Replaces shell scripts with Python for better reliability and debugging

async def handle_session_start(payload: dict) -> dict:
    """Handle SessionStart hook - register new Claude instance."""
    session_id = payload.get("session_id") or payload.get("conversation_id")
    if not session_id:
        session_id = f"claude-{int(time.time())}-{os.getpid()}"

    # Detect origin type from env vars in payload
    origin_type = "local"
    source_ip = None
    env = payload.get("env", {})
    if env.get("CRON_JOB_NAME"):
        origin_type = "cron"
    elif env.get("SSH_CLIENT"):
        origin_type = "ssh"
        source_ip = env["SSH_CLIENT"].split()[0]

    # Get working directory and tab name
    working_dir = payload.get("cwd") or os.getcwd()
    raw_tab_name = payload.get("env", {}).get("CLAUDE_TAB_NAME") or ""
    # Strip Claude Code's ✳ prefix and whitespace artifacts from pane titles
    tab_name = raw_tab_name.lstrip("✳ ").strip() if raw_tab_name else ""
    tab_name = tab_name or f"Claude {datetime.now().strftime('%H:%M')}"

    # Detect subagent from env var
    subagent_env = payload.get("env", {}).get("TOKEN_API_SUBAGENT", "")
    is_subagent = 1 if subagent_env else 0
    spawner = subagent_env or None

    # Capture tmux pane for Golden Throne transport and cross-machine dispatch
    # Claude Code strips $TMUX_PANE from hook env, so also check top-level payload
    # (hook resolves pane via PID walk and injects it directly)
    tmux_pane = env.get("TMUX_PANE") or payload.get("tmux_pane")

    # Auto-name subagents
    if is_subagent and not payload.get("env", {}).get("CLAUDE_TAB_NAME"):
        tab_name = f"sub: {spawner}"

    # Resolve device_id from HTTP client IP (where the instance actually runs)
    # SSH_CLIENT gives the SSH origin (Mac), not the instance's machine (WSL)
    client_ip = payload.get("_client_ip")
    if not source_ip:
        source_ip = client_ip
    device_id = _main().resolve_device_from_ip(client_ip) if client_ip else "Mac-Mini"

    # Detect primarch (env var) and transplant-from (file-based handoff injected by hook)
    primarch_name = env.get("TOKEN_API_PRIMARCH", "")
    transplant_from = payload.get("transplant_from", "")

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Check if already registered
        cursor = await db.execute(
            "SELECT * FROM claude_instances WHERE id = ?",
            (session_id,)
        )
        existing_row = await cursor.fetchone()

        # --- Supplant logic: reuse existing instance row instead of creating new ---
        # Priority: DB transplant marker > hook file handoff > primarch singleton
        supplant_id = None

        # 1. Check DB for pending transplant targeting this session (cross-device safe)
        cursor = await db.execute(
            "SELECT id FROM claude_instances WHERE transplant_target_session = ?",
            (session_id,)
        )
        db_transplant_row = await cursor.fetchone()
        if db_transplant_row:
            supplant_id = db_transplant_row["id"]
            # Clear the marker
            await db.execute(
                "UPDATE claude_instances SET transplant_target_session = NULL WHERE id = ?",
                (supplant_id,)
            )

        # 2. File-based handoff (local transplant — injected by generic-hook.sh)
        if not supplant_id and transplant_from:
            supplant_id = transplant_from

        # 3. Primarch singleton (reuse most recent instance with same primarch)
        if not supplant_id and primarch_name:
            # Primarch singleton: find most recent instance with this primarch name
            cursor = await db.execute(
                "SELECT id FROM claude_instances WHERE primarch = ? ORDER BY registered_at DESC LIMIT 1",
                (primarch_name,)
            )
            row = await cursor.fetchone()
            if row:
                supplant_id = row["id"]

        # --- Handle --continue (same session ID) with transplant ---
        # With --continue, the session ID doesn't change. If the row already exists
        # and there's a transplant signal, update the row in-place (new device, dir, pid).
        # If no transplant signal, it's a normal re-registration (no-op).
        if existing_row:
            if supplant_id and supplant_id == session_id:
                # Same-ID transplant (--continue): update the existing row in-place
                now = datetime.now().isoformat()
                await db.execute(
                    """UPDATE claude_instances
                       SET working_dir = ?, pid = ?, device_id = ?,
                           status = 'idle', tmux_pane = ?,
                           last_activity = ?,
                           stopped_at = NULL, victory_at = NULL, victory_reason = NULL,
                           input_lock = NULL, transplant_target_session = NULL,
                           primarch = ?
                       WHERE id = ?""",
                    (
                        working_dir,
                        payload.get("pid"),
                        device_id,
                        tmux_pane,
                        now,
                        primarch_name or existing_row["primarch"] if hasattr(existing_row, '__getitem__') else primarch_name,
                        session_id
                    )
                )
                await db.commit()

                # Resolve preserved profile for color
                cursor = await db.execute("SELECT * FROM claude_instances WHERE id = ?", (session_id,))
                updated_inst = await cursor.fetchone()
                cc_color = "default"
                hex_color = "#666666"
                if updated_inst and updated_inst["profile_name"]:
                    for p in PROFILES + FALLBACK_VOICES + [ULTIMATE_FALLBACK]:
                        if p["name"] == updated_inst["profile_name"]:
                            cc_color = p.get("cc_color", "default")
                            hex_color = p.get("color", "#666666")
                            break

                logger.info(f"Hook: SessionStart transplant-refresh {session_id[:12]}... ({working_dir}) [device:{device_id}]")
                return {
                    "success": True,
                    "action": "transplant_refreshed",
                    "instance_id": session_id,
                    "profile": updated_inst["profile_name"] if updated_inst else None,
                    "color": hex_color,
                    "cc_color": cc_color,
                    "session_doc_id": updated_inst["session_doc_id"] if updated_inst else None
                }
            else:
                # No transplant signal — normal re-registration, no-op
                return {"success": True, "action": "already_registered", "instance_id": session_id}

        if supplant_id:
            # Fetch the old instance to preserve its config
            cursor = await db.execute(
                "SELECT * FROM claude_instances WHERE id = ?",
                (supplant_id,)
            )
            old_inst = await cursor.fetchone()

            if old_inst:
                now = datetime.now().isoformat()
                internal_session_id = str(uuid.uuid4())

                # Update the old row with new session identity, preserve config
                await db.execute(
                    """UPDATE claude_instances
                       SET id = ?, session_id = ?, working_dir = ?, pid = ?,
                           status = 'idle', tmux_pane = ?, device_id = ?,
                           registered_at = ?, last_activity = ?,
                           stopped_at = NULL, victory_at = NULL, victory_reason = NULL,
                           input_lock = NULL, primarch = ?
                       WHERE id = ?""",
                    (
                        session_id,
                        internal_session_id,
                        working_dir,
                        payload.get("pid"),
                        tmux_pane,
                        device_id,
                        now,
                        now,
                        primarch_name or old_inst["primarch"],
                        supplant_id
                    )
                )

                # Auto-link primarch session doc if applicable
                session_doc_id = old_inst["session_doc_id"]
                if primarch_name and not session_doc_id:
                    cursor = await db.execute(
                        "SELECT session_doc_id FROM primarch_session_docs WHERE primarch_name = ? AND unlinked_at IS NULL",
                        (primarch_name,)
                    )
                    link_row = await cursor.fetchone()
                    if link_row and link_row[0]:
                        session_doc_id = link_row[0]
                        await db.execute(
                            "UPDATE claude_instances SET session_doc_id = ? WHERE id = ?",
                            (session_doc_id, session_id)
                        )

                await db.commit()

                if session_doc_id:
                    await _main()._update_doc_agents_list(db, session_doc_id)

                # Resolve cc_color from preserved profile
                preserved_profile = old_inst["profile_name"]
                cc_color = "default"
                hex_color = "#666666"
                if preserved_profile:
                    for p in PROFILES + FALLBACK_VOICES + [ULTIMATE_FALLBACK]:
                        if p["name"] == preserved_profile:
                            cc_color = p.get("cc_color", "default")
                            hex_color = p.get("color", "#666666")
                            break

                supplant_source = f"transplant:{transplant_from}" if transplant_from else f"primarch:{primarch_name}"
                logger.info(f"Hook: SessionStart supplanted {supplant_id[:12]}... → {session_id[:12]}... ({working_dir}) [{supplant_source}]")
                await log_event("instance_supplanted", instance_id=session_id, device_id=device_id,
                                details={"old_id": supplant_id, "tab_name": old_inst["tab_name"],
                                         "source": supplant_source, "primarch": primarch_name or None})

                return {
                    "success": True,
                    "action": "supplanted",
                    "instance_id": session_id,
                    "supplanted_from": supplant_id,
                    "profile": preserved_profile,
                    "color": hex_color,
                    "cc_color": cc_color,
                    "session_doc_id": session_doc_id
                }

        # --- Normal registration (no supplant) ---

        # Skip TTS profile assignment for subagents (headless, no voice needed)
        if is_subagent:
            profile = {"name": None, "wsl_voice": None, "notification_sound": None}
            pool_exhausted = False
        else:
            # Get WSL voices held by active instances
            cursor = await db.execute(
                "SELECT tts_voice FROM claude_instances WHERE status IN ('processing', 'idle')"
            )
            rows = await cursor.fetchall()
            used_wsl_voices = {row[0] for row in rows if row[0]}

            # Assign profile via linear probe
            profile, pool_exhausted = get_next_available_profile(used_wsl_voices)

        # Preserve discord settings from prior registration (--resume re-registers with same id)
        _prior_discord_hosted = 0
        _prior_discord_channel = None
        _prior_legion = None
        cursor = await db.execute(
            "SELECT discord_hosted, discord_channel, legion FROM claude_instances WHERE id = ?",
            (session_id,)
        )
        _prior_row = await cursor.fetchone()
        if _prior_row:
            _prior_discord_hosted = _prior_row[0] or 0
            _prior_discord_channel = _prior_row[1]
            _prior_legion = _prior_row[2]
            # Delete old row so INSERT succeeds (id is PRIMARY KEY)
            await db.execute("DELETE FROM claude_instances WHERE id = ?", (session_id,))

        # Insert instance
        now = datetime.now().isoformat()
        internal_session_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO claude_instances
               (id, session_id, tab_name, working_dir, origin_type, source_ip, device_id,
                profile_name, tts_voice, notification_sound, pid, status,
                is_subagent, spawner, tmux_pane, primarch,
                discord_hosted, discord_channel,
                registered_at, last_activity)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                internal_session_id,
                tab_name,
                working_dir,
                origin_type,
                source_ip,
                device_id,
                profile["name"],
                profile["wsl_voice"],
                profile["notification_sound"],
                payload.get("pid"),
                is_subagent,
                spawner,
                tmux_pane,
                primarch_name or None,
                _prior_discord_hosted,
                _prior_discord_channel,
                now,
                now
            )
        )
        # Auto-link primarch instance to its active session doc
        session_doc_id = None
        if primarch_name:
            cursor = await db.execute(
                "SELECT session_doc_id FROM primarch_session_docs WHERE primarch_name = ? AND unlinked_at IS NULL",
                (primarch_name,)
            )
            link_row = await cursor.fetchone()
            if link_row and link_row[0]:
                session_doc_id = link_row[0]
                await db.execute(
                    "UPDATE claude_instances SET session_doc_id = ? WHERE id = ?",
                    (session_doc_id, session_id)
                )

        # Auto-create session doc for top-level sessions that don't have one yet
        auto_created_doc = False
        if not session_doc_id and not is_subagent:
            today = datetime.now().strftime("%Y-%m-%d")
            now_ts = datetime.now().isoformat()

            if origin_type == "cron":
                # Cron agents: reuse existing doc for same job, or create one
                cron_job_id = env.get("CRON_JOB_ID")
                cron_job_name = env.get("CRON_JOB_NAME", "cron")
                if cron_job_id:
                    cursor = await db.execute(
                        "SELECT id FROM session_documents WHERE cron_job_id = ? AND status = 'active'",
                        (cron_job_id,)
                    )
                    existing_cron_doc = await cursor.fetchone()
                    if existing_cron_doc:
                        session_doc_id = existing_cron_doc[0]
                    else:
                        # Create new cron session doc in Mars/Sessions/
                        doc_title = cron_job_name
                        slug = doc_title.lower().replace(" ", "-")[:50]
                        fp = MARS_SESSIONS_DIR / f"{today}-{slug}.md"
                        # Avoid collision
                        counter = 1
                        while fp.exists():
                            fp = MARS_SESSIONS_DIR / f"{today}-{slug}-{counter}.md"
                            counter += 1
                        cursor = await db.execute(
                            """INSERT INTO session_documents (title, file_path, project, cron_job_id, status, created_at, updated_at)
                               VALUES (?, ?, ?, ?, 'active', ?, ?)""",
                            (doc_title, str(fp), None, cron_job_id, now_ts, now_ts)
                        )
                        session_doc_id = cursor.lastrowid
                        _main().create_session_doc_file(fp, doc_title, session_doc_id)
                        auto_created_doc = True
            else:
                # Interactive sessions: create doc with "{cwd_basename} {date}"
                cwd_basename = Path(working_dir).name if working_dir else "session"
                doc_title = f"{cwd_basename} {today}"
                slug = doc_title.lower().replace(" ", "-")[:50]
                fp = DEFAULT_SESSIONS_DIR / f"{today}-{slug}.md"
                counter = 1
                while fp.exists():
                    fp = DEFAULT_SESSIONS_DIR / f"{today}-{slug}-{counter}.md"
                    counter += 1
                cursor = await db.execute(
                    """INSERT INTO session_documents (title, file_path, project, status, created_at, updated_at)
                       VALUES (?, ?, ?, 'active', ?, ?)""",
                    (doc_title, str(fp), None, now_ts, now_ts)
                )
                session_doc_id = cursor.lastrowid
                _main().create_session_doc_file(fp, doc_title, session_doc_id)
                auto_created_doc = True

            if session_doc_id:
                await db.execute(
                    "UPDATE claude_instances SET session_doc_id = ? WHERE id = ?",
                    (session_doc_id, session_id)
                )

        # Auto-detect legion from context
        auto_legion = None
        if origin_type == "cron":
            # Cron jobs: look up legion from cron_jobs table, default to mechanicus
            cron_job_id = env.get("CRON_JOB_ID")
            if cron_job_id:
                cursor = await db.execute(
                    "SELECT legion FROM cron_jobs WHERE id = ?", (cron_job_id,)
                )
                cron_row = await cursor.fetchone()
                auto_legion = (cron_row[0] if cron_row and cron_row[0] else "mechanicus")
            else:
                auto_legion = "mechanicus"
        elif working_dir and ("pax-env" in working_dir.lower() or "/pax/" in working_dir.lower()):
            auto_legion = "civic"

        # Restore prior legion if no auto-detect, or apply auto-detect
        if auto_legion:
            await db.execute(
                "UPDATE claude_instances SET legion = ? WHERE id = ?",
                (auto_legion, session_id)
            )
        elif _prior_legion and _prior_legion != "astartes":
            await db.execute(
                "UPDATE claude_instances SET legion = ? WHERE id = ?",
                (_prior_legion, session_id)
            )

        await db.commit()

        # Update frontmatter if we linked a session doc
        if session_doc_id:
            await _main()._update_doc_agents_list(db, session_doc_id)

    logger.info(f"Hook: SessionStart registered {session_id[:12]}... ({working_dir}){' [subagent]' if is_subagent else ''}{f' [primarch:{primarch_name}]' if primarch_name else ''}{f' [legion:{auto_legion}]' if auto_legion else ''}")
    await log_event("instance_registered", instance_id=session_id, device_id=device_id,
                    details={"tab_name": tab_name, "origin_type": origin_type, "source": "hook",
                             "is_subagent": is_subagent, "spawner": spawner,
                             "primarch": primarch_name or None})

    return {
        "success": True,
        "action": "registered",
        "instance_id": session_id,
        "profile": profile["name"] if not is_subagent else None,
        "color": profile.get("color") if not is_subagent else None,
        "cc_color": profile.get("cc_color") if not is_subagent else None,
        "session_doc_id": session_doc_id
    }


async def handle_session_end(payload: dict) -> dict:
    """Handle SessionEnd hook - deregister Claude instance."""
    session_id = payload.get("session_id") or payload.get("conversation_id")
    if not session_id:
        return {"success": False, "action": "no_session_id"}

    _pending_background_tasks.pop(session_id, None)

    now = datetime.now().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id, device_id, COALESCE(is_subagent, 0), session_doc_id FROM claude_instances WHERE id = ?",
            (session_id,)
        )
        row = await cursor.fetchone()

        if not row:
            return {"success": False, "action": "not_found", "instance_id": session_id}

        is_subagent = row[2]
        session_doc_id = row[3]

        # Count non-subagent active instances BEFORE stopping
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle') AND COALESCE(is_subagent, 0) = 0"
        )
        count_row = await cursor.fetchone()
        was_active = count_row[0] if count_row else 0

        await db.execute(
            "UPDATE claude_instances SET status = 'stopped', synced = 0, stopped_at = ? WHERE id = ?",
            (now, session_id)
        )
        await db.commit()

        # Check remaining active instances
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle')"
        )
        count_row = await cursor.fetchone()
        remaining_active = count_row[0] if count_row else 0

        # Count remaining non-subagent active instances
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle') AND COALESCE(is_subagent, 0) = 0"
        )
        count_row = await cursor.fetchone()
        remaining_non_sub = count_row[0] if count_row else 0

    # Golden Throne: cancel any pending follow-up (session terminated)
    try:
        _main().scheduler.remove_job(f"golden-throne-{session_id}")
        logger.info(f"Golden Throne: cancelled follow-up for {session_id[:12]} (session end)")
    except Exception:
        pass

    logger.info(f"Hook: SessionEnd stopped {session_id[:12]}...")
    await log_event("instance_stopped", instance_id=session_id, device_id=row[1],
                    details={"source": "hook"})

    # Instance count Pavlok signals (skip subagents)
    if not is_subagent:
        await _main().check_instance_count_pavlok(remaining_non_sub, was_active)

    # Spawn stop_hook.py to generate transcript + wikilink (session doc or daily note fallback)
    if not is_subagent:
        stop_hook_script = Path(__file__).parent / "stop_hook.py"
        if stop_hook_script.exists():
            try:
                subprocess.Popen(
                    ["python3", str(stop_hook_script), session_id],
                    stdout=subprocess.DEVNULL,
                    stderr=open("/tmp/stop_hook.log", "a"),
                    start_new_session=True
                )
                logger.info(f"Hook: SessionEnd spawned stop_hook for {session_id[:12]}... (doc {session_doc_id or 'none, daily note fallback'})")
            except Exception as e:
                logger.warning(f"Hook: SessionEnd failed to spawn stop_hook: {e}")

    # Handle productivity enforcement if needed
    result = {"success": True, "action": "stopped", "instance_id": session_id}
    if remaining_active == 0 and DESKTOP_STATE.get("current_mode") == "video":
        enforce_result = _main().close_distraction_windows()
        result["enforcement_triggered"] = True
        result["enforcement_result"] = enforce_result

    return result


async def handle_prompt_submit(payload: dict) -> dict:
    """Handle UserPromptSubmit hook - mark instance as processing."""
    session_id = payload.get("session_id")
    if not session_id:
        return {"success": False, "action": "no_session_id"}

    # Each UserPromptSubmit for a session with pending tasks = one background task result delivered.
    if session_id in _pending_background_tasks:
        _pending_background_tasks[session_id] -= 1
        if _pending_background_tasks[session_id] <= 0:
            del _pending_background_tasks[session_id]
        logger.info(f"PromptSubmit: background task returned for {session_id[:12]} (pending: {_pending_background_tasks.get(session_id, 0)})")

    now = datetime.now().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT id FROM claude_instances WHERE id = ?",
            (session_id,)
        )
        if not await cursor.fetchone():
            return {"success": False, "action": "not_found"}

        # Also resurrect stopped instances - activity means they're active
        # Backfill PID if payload contains one and DB value is NULL
        await db.execute(
            """UPDATE claude_instances
               SET status = 'processing', last_activity = ?, stopped_at = NULL,
                   pid = COALESCE(pid, ?)
               WHERE id = ?""",
            (now, payload.get("pid"), session_id)
        )
        await db.commit()

    # Signal productivity — sets prod active, exits IDLE if needed
    now_ms = int(time.monotonic() * 1000)
    old_mode = _main().timer_engine.current_mode.value
    result = _main().timer_engine.set_productivity(True, now_ms)
    exited_idle = _main().TimerEvent.MODE_CHANGED in result.events
    if exited_idle:
        new_mode = _main().timer_engine.current_mode.value
        await _main().timer_log_shift(old_mode, new_mode, trigger="prompt_submit", source="hook")
        logger.info(f"Hook: PromptSubmit exited {old_mode} → {new_mode}")

    # Golden Throne: cancel any pending follow-up (user is active)
    try:
        _main().scheduler.remove_job(f"golden-throne-{session_id}")
        logger.info(f"Golden Throne: cancelled follow-up for {session_id[:12]} (user prompt)")
    except Exception:
        pass

    logger.info(f"Hook: PromptSubmit {session_id[:12]}... -> processing (resurrected if stopped)")
    return {"success": True, "action": "processing", "instance_id": session_id, "exited_idle": exited_idle}


async def handle_post_tool_use(payload: dict) -> dict:
    """Handle PostToolUse hook - heartbeat with debouncing, ensures status='processing'."""
    session_id = payload.get("session_id")
    if not session_id:
        return {"success": False, "action": "no_session_id"}

    # Debounce: only update every 2 seconds per session
    current_time = time.time()
    last_call = _post_tool_debounce.get(session_id, 0)
    if current_time - last_call < 2:
        return {"success": True, "action": "debounced"}

    _post_tool_debounce[session_id] = current_time

    # Update last_activity as heartbeat AND ensure status='processing'
    # This catches cases where prompt_submit was missed (e.g., after context clear)
    # Also resurrect stopped instances - activity means they're active
    # Backfill PID if payload contains one and DB value is NULL
    now = datetime.now().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """UPDATE claude_instances
               SET status = 'processing', last_activity = ?, stopped_at = NULL,
                   pid = COALESCE(pid, ?)
               WHERE id = ?""",
            (now, payload.get("pid"), session_id)
        )
        await db.commit()

    # Signal productivity — active tool use = real work
    now_ms = int(time.monotonic() * 1000)
    _main().timer_engine.set_productivity(True, now_ms)

    return {"success": True, "action": "heartbeat", "instance_id": session_id}


# Legion → Discord bot name mapping
_LEGION_BOT_MAP = {
    "custodes": "custodes",
    "mechanicus": "mechanicus",
    "astartes": "mechanicus",
    "inquisition": "inquisition",
}


async def _post_discord_mirror(channel: str, bot: str, content: str):
    """Mirror instance output to its Discord channel/thread."""
    is_thread = channel.isdigit()
    payload = {
        "channel": "aspirants" if is_thread else channel,
        "bot": bot,
        "content": content[:2000],
    }
    if is_thread:
        payload["thread_id"] = channel
    try:
        import urllib.request as _urllib_req
        data = json.dumps(payload).encode()
        req = _urllib_req.Request(
            f"{DISCORD_DAEMON_URL}/send",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        await asyncio.get_event_loop().run_in_executor(
            None, lambda: _urllib_req.urlopen(req, timeout=10)
        )
        logger.info(f"Discord mirror: {channel} ({bot}) — {len(content)} chars")
    except Exception as e:
        logger.warning(f"Discord mirror failed for {channel}: {e}")


async def handle_stop(payload: dict) -> dict:
    """Handle Stop hook - response completed, trigger TTS/notifications."""
    session_id = payload.get("session_id")
    if not session_id:
        return {"success": False, "action": "no_session_id"}

    # Prevent infinite loops
    if payload.get("stop_hook_active"):
        return {"success": True, "action": "skipped_recursive"}

    # Get instance info
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM claude_instances WHERE id = ?",
            (session_id,)
        )
        instance = await cursor.fetchone()

    if not instance:
        return {"success": False, "action": "instance_not_found"}

    instance = dict(instance)
    device_id = instance.get("device_id", "Mac-Mini")
    tab_name = instance.get("tab_name", "Claude")
    tts_voice = instance.get("tts_voice", "Microsoft David")
    notification_sound = instance.get("notification_sound", "chimes.wav")

    # Update last_activity but DON'T set idle yet — that's the evaluators' job.
    # Sync instances never go idle (permanent processing until SessionEnd).
    # Golden throne / one-off: evaluators write idle on pass, or stay processing on nudge.
    now = datetime.now().isoformat()
    instance_type = instance.get("instance_type", "one_off")
    is_sync_instance = instance_type == "sync"
    is_subagent_instance_quick = bool(instance.get("is_subagent"))
    has_pending_background = _pending_background_tasks.get(session_id, 0) > 0

    # Determine if evaluators will run (they own the idle transition)
    will_evaluate = (not is_subagent_instance_quick and not has_pending_background and not is_sync_instance)

    async with aiosqlite.connect(DB_PATH) as db:
        if will_evaluate or is_sync_instance:
            # Evaluators (or sync retrigger) will handle status — just update timestamp
            await db.execute(
                "UPDATE claude_instances SET last_activity = ? WHERE id = ?",
                (now, session_id)
            )
        else:
            # Subagents, intermediate stops, no evaluators — go idle immediately
            await db.execute(
                "UPDATE claude_instances SET status = 'idle', last_activity = ? WHERE id = ?",
                (now, session_id)
            )
        await db.commit()

    # Fire async stop evaluators (action_validator, plan_auditor, etc.)
    # Skips subagents, sync instances, and intermediate stops.
    if will_evaluate:
        session_doc_id = instance.get("session_doc_id")
        stop_context = payload.get("transcript_tail", "")[:4000] if payload.get("transcript_tail") else ""
        # Signal TUI that evaluators are running for this instance
        _tui_signal_dir = Path.home() / ".claude" / "tui-signals"
        _tui_signal_dir.mkdir(exist_ok=True)
        (_tui_signal_dir / f"evaluating-{session_id}").touch()
        asyncio.create_task(_main()._run_stop_evaluators(
            session_id, session_doc_id, stop_context, tab_name
        ))
        # Auto-name: generate kebab-case name if not explicitly named by our pipeline
        # Also sends /rename + /color to Claude Code UI in concert with instance profile
        transcript_path = payload.get("transcript_path", "")
        asyncio.create_task(_main()._auto_name_instance(dict(instance), stop_context, transcript_path))

    result = {
        "success": True,
        "action": "stop_processed",
        "instance_id": session_id,
        "device_id": device_id
    }

    # ── Subagent detection: skip all notifications for subagents ──
    # DB flag covers subagent-CLI spawned instances; PID check covers Task tool subagents.
    pid = payload.get("pid")
    is_subagent_instance = bool(instance.get("is_subagent")) or bool(pid and _main().is_subagent_pid(pid))
    if is_subagent_instance:
        result["action"] = "stop_processed_subagent"
        logger.info(f"Hook: Stop {session_id[:12]}... subagent — state updated, skipping notifications")
        return result

    # Intermediate stop: background subagents still pending. Update state but skip notifications.
    if _pending_background_tasks.get(session_id, 0) > 0:
        result["action"] = "stop_processed_intermediate"
        logger.info(f"Hook: Stop {session_id[:12]}... intermediate ({_pending_background_tasks[session_id]} background tasks pending) — skipping notifications")
        return result

    # ── Instance lifecycle retrigger ──
    # Golden Throne and sync retriggering is now handled by StopValidate
    # (synchronous gate in stop-validator.sh). The agent self-evaluates and
    # decides whether to continue or allow the stop. No timer scheduling needed.
    # This async Stop handler only processes notifications/TTS for stopped instances.

    # Sync instances that passed through StopValidate don't need notifications
    # (the self-eval prompt already gave them a chance to continue).
    instance_type = instance.get("instance_type", "one_off")
    if instance_type == "sync" and not is_subagent_instance:
        result["action"] = "stop_processed_sync"
        await log_event("hook_stop", instance_id=session_id, details={"sync": True})
        return result

    # Extract TTS text from transcript (prefer embedded tail for remote access,
    # fall back to direct file read if local). Used by both mobile and desktop paths.
    transcript_tail = payload.get("transcript_tail")
    transcript_path = payload.get("transcript_path")
    tts_text = None

    # Determine lines to parse: embedded tail (from hook shim) or direct file read
    transcript_lines = None
    if transcript_tail:
        transcript_lines = transcript_tail.splitlines()
    elif transcript_path and os.path.exists(transcript_path):
        try:
            with open(transcript_path, 'r') as f:
                transcript_lines = f.readlines()
        except Exception as e:
            logger.warning(f"Failed to read transcript: {e}")

    if transcript_lines:
        for line in reversed(transcript_lines):
            if '"role":"assistant"' in line:
                try:
                    data = json.loads(line)
                    content = data.get("message", {}).get("content")
                    if isinstance(content, str) and content.strip():
                        tts_text = content
                    elif isinstance(content, list):
                        # Extract text from content array (skip tool_use-only messages)
                        texts = [c.get("text", "") for c in content if c.get("type") == "text" and c.get("text", "").strip()]
                        if texts:
                            tts_text = "\n".join(texts)
                    elif isinstance(content, dict) and content.get("text", "").strip():
                        tts_text = content["text"]
                    if tts_text:
                        break
                except json.JSONDecodeError:
                    continue

    # Discord output mirroring — fire before TTS sanitization (Discord renders markdown)
    if tts_text and instance.get("discord_hosted") and instance.get("discord_channel"):
        discord_bot = _LEGION_BOT_MAP.get(instance.get("legion", ""), "mechanicus")
        asyncio.create_task(_post_discord_mirror(
            instance["discord_channel"], discord_bot, tts_text
        ))

    # Sanitize TTS text (remove markdown formatting and normalize whitespace)
    if tts_text:
        # Strip markdown headers (must be before newline conversion)
        tts_text = re.sub(r'^#{1,6}\s*', '', tts_text, flags=re.MULTILINE)
        # Strip markdown bold/italic
        tts_text = re.sub(r'\*\*([^*]+)\*\*', r'\1', tts_text)  # **bold**
        tts_text = re.sub(r'\*([^*]+)\*', r'\1', tts_text)      # *italic*
        tts_text = re.sub(r'__([^_]+)__', r'\1', tts_text)      # __bold__
        tts_text = re.sub(r'_([^_]+)_', r'\1', tts_text)        # _italic_
        # Strip inline code
        tts_text = re.sub(r'`([^`]+)`', r'\1', tts_text)
        # Strip code blocks
        tts_text = re.sub(r'```[\s\S]*?```', '', tts_text)
        # Strip bullet points and list markers
        tts_text = re.sub(r'^[\s]*[-*+]\s+', '', tts_text, flags=re.MULTILINE)
        tts_text = re.sub(r'^[\s]*\d+\.\s+', '', tts_text, flags=re.MULTILINE)
        # Convert newlines to spaces
        tts_text = tts_text.replace('\n', ' ')
        # Normalize multiple spaces
        tts_text = re.sub(r' +', ' ', tts_text)
        tts_text = tts_text.strip()

    # Mobile path: v3 /notify with TTS + banner + vibe
    if device_id == "Token-S24":
        notify_params = {
            "banner_text": f"[{tab_name}] finished",
            "vibe": 30,
        }
        if tts_text:
            notify_params["tts_text"] = tts_text[:300]
        phone_result = await asyncio.to_thread(_main()._send_to_phone, "/notify", notify_params)
        result["notification"] = phone_result
        logger.info(f"Hook: Stop {session_id[:12]}... -> mobile v3 notify ({len(tts_text or '')} chars)")
        return result

    # Desktop path: TTS and notification
    # Check TTS config
    tts_config_file = Path.home() / ".claude" / ".tts-config.json"
    tts_enabled = True

    if tts_config_file.exists():
        try:
            with open(tts_config_file) as f:
                config = json.load(f)
                tts_enabled = config.get("enabled", True)
        except Exception:
            pass

    # Queue TTS if enabled and we have text
    if tts_enabled and tts_text:
        logger.info(f"Hook: Stop queuing TTS, {len(tts_text)} chars: {tts_text[:80]}...")
        tts_result = await queue_tts(session_id, tts_text)
        logger.info(f"Hook: Stop queue_tts result: {json.dumps(tts_result)}")
        result["tts"] = tts_result
    else:
        # Just play notification sound without TTS
        logger.info(f"Hook: Stop no TTS text (tts_enabled={tts_enabled}, has_text={bool(tts_text)})")
        play_sound(notification_sound)
        result["sound"] = {"played": notification_sound}

    # Pavlok vibe notification (skip for subagents)
    if not instance.get("is_subagent"):
        vibe_result = _main().send_pavlok_stimulus(
            stimulus_type="vibe",
            value=30,
            reason="claude_finished",
            respect_cooldown=False,
        )
        result["pavlok_vibe"] = vibe_result

    logger.info(f"Hook: Stop {session_id[:12]}... -> desktop notification")
    await log_event("hook_stop", instance_id=session_id, details={"tts_enabled": tts_enabled, "tts_length": len(tts_text) if tts_text else 0})

    return result


async def handle_pre_tool_use(payload: dict) -> dict:
    """Handle PreToolUse hook - marks processing, can block operations like 'make deploy'."""
    session_id = payload.get("session_id")
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    # Mark instance as processing (catches cases where prompt_submit was missed)
    # Also resurrect stopped instances - activity means they're active
    if session_id:
        now = datetime.now().isoformat()
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                """UPDATE claude_instances
                   SET status = 'processing', last_activity = ?, stopped_at = NULL
                   WHERE id = ?""",
                (now, session_id)
            )
            await db.commit()

    # Track background Task subagents so Stop hooks can detect intermediate vs final stops.
    if tool_name == "Task" and tool_input.get("run_in_background"):
        _pending_background_tasks[session_id] = _pending_background_tasks.get(session_id, 0) + 1
        logger.info(f"PreToolUse: Task background launched for {session_id[:12]} (pending: {_pending_background_tasks[session_id]})")
        return {"success": True, "action": "allowed"}

    # Voice chat: when AskUserQuestion fires for a voice-chat-active instance,
    # 1) TTS the question text so the user hears it spoken
    # 2) trigger AHK to auto-select "Other" and start dictation
    if tool_name == "AskUserQuestion" and session_id and session_id in _main().VOICE_CHAT_SESSIONS:
        # Extract and speak question text
        questions = tool_input.get("questions", [])
        if questions:
            tts_parts = []
            for q in questions:
                question_text = q.get("question", "")
                if question_text:
                    tts_parts.append(question_text)
            if tts_parts:
                tts_message = " ".join(tts_parts)
                try:
                    await queue_tts(session_id, tts_message)
                    logger.info(f"PreToolUse: Voice chat TTS queued for {session_id[:12]}: {tts_message[:80]}")
                except Exception as e:
                    logger.warning(f"PreToolUse: Voice chat TTS failed for {session_id[:12]}: {e}")

        # Return local_exec so generic-hook.sh runs AHK on WSL
        # voice-send-keys.ahk uses tmux send-keys — no WinActivate needed
        vc_session = _main().VOICE_CHAT_SESSIONS.get(session_id, {})
        tmux_pane = vc_session.get("tmux_pane", "")
        pane_arg = f' "{tmux_pane}"' if tmux_pane else ""
        logger.info(f"PreToolUse: Voice chat local_exec for {session_id[:12]} (pane: {tmux_pane or 'default'})")
        return {
            "success": True,
            "action": "allowed",
            "local_exec": f'"/mnt/c/Program Files/AutoHotkey/v2/AutoHotkey.exe" "//Token-NAS/Imperium/Token-OS/ahk/voice-send-keys.ahk"{pane_arg} --navigate',
        }

    # Discord-hosted: post AskUserQuestion to Discord channel and notify phone
    _ask_handled_by_discord = False
    if tool_name == "AskUserQuestion" and session_id:
        async with aiosqlite.connect(DB_PATH) as db:
            cursor = await db.execute(
                "SELECT discord_hosted, discord_channel, legion FROM claude_instances WHERE id = ?",
                (session_id,)
            )
            dh_row = await cursor.fetchone()
        if dh_row and dh_row[0] and dh_row[1]:
            _ask_handled_by_discord = True
            discord_channel = dh_row[1]
            discord_bot = _LEGION_BOT_MAP.get(dh_row[2] or "", "mechanicus")
            questions = tool_input.get("questions", [])
            if questions:
                q_parts = [q.get("question", "") for q in questions if q.get("question")]
                if q_parts:
                    q_text = "\n".join(q_parts)
                    asyncio.create_task(_post_discord_mirror(
                        discord_channel, discord_bot,
                        f"**Question:** {q_text}"
                    ))
                    # Also phone notify so Emperor knows to check Discord
                    asyncio.create_task(asyncio.to_thread(_main()._send_to_phone, "/notify", {
                        "vibe": 40,
                        "tts_text": f"Claude is asking a question in Discord.",
                        "banner_text": q_parts[0][:80],
                    }))
                    logger.info(f"PreToolUse: AskUserQuestion posted to Discord #{discord_channel} for {session_id[:12]}")

    # Phone notification for AskUserQuestion (non-voice-chat, non-discord-hosted instances)
    if tool_name == "AskUserQuestion" and session_id and session_id not in _main().VOICE_CHAT_SESSIONS and not _ask_handled_by_discord:
        questions = tool_input.get("questions", [])
        if questions:
            q_text = questions[0].get("question", "")[:200]
            if q_text:
                asyncio.create_task(asyncio.to_thread(_main()._send_to_phone, "/notify", {
                    "vibe": 40,
                    "beep": 30,
                    "tts_text": f"Claude is asking: {q_text}",
                    "banner_text": q_text[:80],
                }))
                logger.info(f"PreToolUse: AskUserQuestion phone notify for {session_id[:12]}: {q_text[:60]}")

    # Only check Bash commands for blocking
    if tool_name != "Bash":
        return {"success": True, "action": "allowed"}

    command = tool_input.get("command", "")

    # Block 'make deploy' commands
    if "make deploy" in command or command.strip() == "make deploy":
        # Build alternative command suggestion
        deploy_args = []
        if "ENVIRONMENT=production" in command:
            deploy_args.append("production")
        if "--blocking" in command:
            deploy_args.append("--blocking")

        alt_command = "deploy"
        if deploy_args:
            alt_command += " " + " ".join(deploy_args)

        return {
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"'make deploy' is disabled. Use autonomous deployment instead:\n\n"
                f"  {alt_command}\n\n"
                f"This provides better error detection and log monitoring."
            )
        }

    return {"success": True, "action": "allowed"}


async def handle_notification(payload: dict) -> dict:
    """Handle Notification hook - play notification sound."""
    session_id = payload.get("session_id")

    # Get instance profile for sound selection
    sound_file = "chimes.wav"  # default

    if session_id:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT notification_sound FROM claude_instances WHERE id = ?",
                (session_id,)
            )
            row = await cursor.fetchone()
            if row and row["notification_sound"]:
                sound_file = row["notification_sound"]

    result = play_sound(sound_file)
    return {"success": True, "action": "sound_played", "sound": sound_file, "result": result}


# ============ Stop Hook Self-Evaluation (Compacted Retrigger) ============

_SELF_EVAL_PROMPT = (
    "You stopped. Read your session doc. "
    "If there's active work remaining or a session to maintain, "
    "run a recovery action (ScheduleWakeup, continue working, or escalate via Discord). "
    "If this was a clean exit or victory, do nothing — allow the stop."
)


async def handle_stop_validate(payload: dict) -> dict:
    """StopValidate: synchronous gate that can block a stop with a self-evaluation prompt.

    Replaces the old MiniMax retrigger dispatch + Golden Throne timer system.
    Golden Throne and sync instances get blocked once — the agent self-evaluates
    and decides whether to continue or allow the stop. Second stop passes through.
    """
    session_id = payload.get("session_id")
    if not session_id:
        return {}  # no decision — allow stop

    # ── Check if this is a second stop (self-eval already issued) ──
    now = time.time()
    if session_id in _self_eval_pending:
        issued_at = _self_eval_pending.pop(session_id)
        elapsed = now - issued_at
        logger.info(
            f"StopValidate: {session_id[:12]} self-eval complete "
            f"({elapsed:.1f}s) — allowing stop"
        )
        await log_event("stop_validate_pass", instance_id=session_id,
                        details={"reason": "self_eval_complete", "elapsed": elapsed})
        return {}  # no decision — allow stop

    # ── Expire stale entries ──
    stale = [sid for sid, ts in _self_eval_pending.items()
             if now - ts > SELF_EVAL_TTL_SECONDS]
    for sid in stale:
        del _self_eval_pending[sid]

    # ── Look up instance ──
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, instance_type, is_subagent, victory_at FROM claude_instances WHERE id = ?",
            (session_id,)
        )
        instance = await cursor.fetchone()

    if not instance:
        return {}  # unknown instance — allow stop

    instance = dict(instance)
    instance_type = instance.get("instance_type", "one_off")

    # ── Skip: subagents never get self-eval ──
    if instance.get("is_subagent"):
        return {}

    # ── Skip: victory already declared ──
    if instance.get("victory_at"):
        return {}

    # ── Skip: one-off instances don't need self-eval ──
    if instance_type == "one_off":
        return {}

    # ── ScheduleWakeup detection: don't block if the SDK is handling wakeup ──
    transcript_tail = payload.get("transcript_tail", "")
    if "ScheduleWakeup" in transcript_tail:
        logger.info(f"StopValidate: {session_id[:12]} has active ScheduleWakeup — allowing stop")
        await log_event("stop_validate_pass", instance_id=session_id,
                        details={"reason": "schedule_wakeup_active"})
        return {}

    # ── Block: golden_throne and sync instances get self-eval prompt ──
    if instance_type in ("golden_throne", "sync"):
        _self_eval_pending[session_id] = now
        logger.info(
            f"StopValidate: blocking {session_id[:12]} ({instance_type}) "
            f"with self-eval prompt"
        )
        await log_event("stop_validate_block", instance_id=session_id,
                        details={"instance_type": instance_type})
        return {
            "decision": "block",
            "reason": _SELF_EVAL_PROMPT,
        }

    return {}  # default: allow stop


# Hook dispatcher endpoint
@router.post("/api/hooks/{action_type}")
async def dispatch_hook(action_type: str, payload: dict, request: Request) -> dict:
    """
    Unified hook dispatcher for Claude Code hooks.

    Receives hook events from generic-hook.sh and routes to appropriate handler.
    Always returns a response - errors are logged but don't cause failures.
    """
    handlers = {
        "SessionStart": handle_session_start,
        "SessionEnd": handle_session_end,
        "UserPromptSubmit": handle_prompt_submit,
        "PostToolUse": handle_post_tool_use,
        "Stop": handle_stop,
        "StopValidate": handle_stop_validate,
        "PreToolUse": handle_pre_tool_use,
        "Notification": handle_notification,
    }

    handler = handlers.get(action_type)
    if not handler:
        logger.warning(f"Hook: Unknown action type: {action_type}")
        return {"success": False, "action": "unknown_hook_type", "type": action_type}

    # Inject HTTP client IP into payload for device detection fallback
    if request.client:
        payload["_client_ip"] = request.client.host

    try:
        result = await handler(payload)
        return result
    except Exception as e:
        logger.error(f"Hook handler error ({action_type}): {e}")
        await log_event("hook_error", details={"action_type": action_type, "error": str(e)})
        return {"success": False, "action": "handler_error", "error": str(e)}


