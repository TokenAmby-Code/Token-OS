"""
Claude Code Hook Handlers — extracted from main.py.

Owns:
- Hook lifecycle handlers (SessionStart, Stop, PromptSubmit, etc.)
- Hook dispatch endpoint (/api/hooks/{action_type})
- Discord output mirroring

Uses dependency injection from main.py for runtime-owned callbacks.
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
from fastapi import APIRouter, Request
from pydantic import BaseModel

import shared
from enforcement_service import close_distraction_windows
from instance_mutation import (
    _fetch_instance_row,
    sanctioned_delete_instance,
    sanctioned_insert_instance,
    sanctioned_update_instance,
)
from pane_surface import human_tab_name as _human_tab_name
from phone_service import _send_to_phone, check_instance_count_pavlok, send_pavlok_stimulus
from routes.tts import play_sound, queue_tts
from session_doc_helpers import (
    _update_doc_agents_list,
    read_frontmatter,
    resolve_session_doc_for_start,
    update_frontmatter,
)
from shared import (
    ASKQ_BUST_PROMPT,
    ASKQ_LADDER,
    DB_PATH,
    DESKTOP_STATE,
    DISCORD_DAEMON_URL,
    FALLBACK_VOICES,
    PROFILES,
    ULTIMATE_FALLBACK,
    VOICE_CHAT_SESSIONS,
    append_workflow_event,
    get_next_available_profile,
    is_subagent_pid,
    log_event,
    resolve_device_from_ip,
)
from timer import TimerEvent

logger = logging.getLogger("token_api")

router = APIRouter()

_QUESTION_LOG_TITLE = "AskUserQuestion Log"
_UNANSWERED_TITLE = "Unanswered Questions"
_ASKQ_PERSIST_LOCK = asyncio.Lock()
VALID_LAUNCH_INSTANCE_TYPES = {"golden_throne", "sync", "one_off"}


# ============ Injected Dependencies ============
# main.py owns these runtime services and injects them after import.

_scheduler: Any = None
_timer_engine: Any = None
_timer_log_shift: Callable[..., Any] | None = None
_run_stop_evaluators: Callable[..., Any] | None = None
_auto_name_instance: Callable[..., Any] | None = None
_work_action_callback: Callable[..., Any] | None = None
_schedule_golden_throne_callback: Callable[..., Any] | None = None
_golden_throne_activity_callback: Callable[..., Any] | None = None
# AUQ ladder escalation callbacks. Level 2 keeps the historical
# `_askq_touch2_callback` name for the cascade since main.py already wires it.
_askq_level1_callback: Callable[..., Any] | None = None
_askq_touch2_callback: Callable[..., Any] | None = None
_askq_level3_callback: Callable[..., Any] | None = None


def init_deps(
    *,
    scheduler=None,
    timer_engine=None,
    timer_log_shift=None,
    run_stop_evaluators=None,
    auto_name_instance=None,
    work_action_callback=None,
    schedule_golden_throne_callback=None,
    golden_throne_activity_callback=None,
    askq_level1_callback=None,
    askq_touch2_callback=None,
    askq_level3_callback=None,
):
    """Wire runtime-owned dependencies from main.py."""
    global _scheduler, _timer_engine, _timer_log_shift
    global _run_stop_evaluators, _auto_name_instance, _work_action_callback
    global _schedule_golden_throne_callback, _golden_throne_activity_callback
    global _askq_level1_callback, _askq_touch2_callback, _askq_level3_callback

    _scheduler = scheduler
    _timer_engine = timer_engine
    _timer_log_shift = timer_log_shift
    _run_stop_evaluators = run_stop_evaluators
    _auto_name_instance = auto_name_instance
    _work_action_callback = work_action_callback
    _schedule_golden_throne_callback = schedule_golden_throne_callback
    _golden_throne_activity_callback = golden_throne_activity_callback
    _askq_level1_callback = askq_level1_callback
    _askq_touch2_callback = askq_touch2_callback
    _askq_level3_callback = askq_level3_callback


def _require_dep(name: str, value):
    """Fail loudly if main.py forgot to wire a required runtime dependency."""
    if value is None:
        raise RuntimeError(f"routes.hooks dependency not initialized: {name}")
    return value


# ============ Hook Models ============


class HookResponse(BaseModel):
    """Standard response for hook handlers."""

    success: bool = True
    action: str
    details: dict | None = None


class PreToolUseResponse(BaseModel):
    """Response for PreToolUse hooks that can block operations."""

    permissionDecision: str | None = None  # "allow" or "deny"
    permissionDecisionReason: str | None = None


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


async def _run_subprocess_offloop(
    args: list[str] | tuple[str, ...],
    *,
    timeout: float | None = None,
    stdout=None,
    stderr=None,
) -> subprocess.CompletedProcess:
    """Run short hook utility subprocesses outside the asyncio event loop."""
    return await asyncio.to_thread(
        subprocess.run,
        list(args),
        stdout=stdout,
        stderr=stderr,
        timeout=timeout,
        check=False,
    )


async def _tmux_pane_exists(tmux_pane: str | None) -> bool:
    return await shared.tmux_pane_exists(tmux_pane)


async def _tmux_pane_label(tmux_pane: str | None) -> str | None:
    if not tmux_pane:
        return None
    try:
        proc = await _run_subprocess_offloop(
            ("tmux", "show-options", "-pv", "-t", tmux_pane, "@PANE_ID"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=2,
        )
        if proc.returncode == 0:
            label = proc.stdout.decode(errors="ignore").strip()
            return label or None
    except Exception as exc:
        logger.debug(f"Hook: pane label lookup failed for {tmux_pane}: {exc}")
    return None


async def _stop_if_dead_pane(db, session_id: str, existing: dict, actor: str) -> bool:
    tmux_pane = existing.get("tmux_pane")
    if not tmux_pane:
        return False
    if existing.get("status") == "stopped":
        return True
    if await _tmux_pane_exists(tmux_pane):
        return False
    await sanctioned_update_instance(
        db,
        instance_id=session_id,
        updates={
            "status": "stopped",
            "synced": 0,
            "stopped_at": datetime.now().isoformat(),
        },
        mutation_type="instance_stopped",
        write_source="hooks",
        actor=f"{actor}-dead-pane",
    )
    await db.commit()
    await log_event(
        "hook_ignored_dead_pane",
        instance_id=session_id,
        details={"actor": actor, "tmux_pane": tmux_pane},
    )
    if existing.get("instance_type") == "golden_throne" and _schedule_golden_throne_callback:
        try:
            await _schedule_golden_throne_callback(existing, reason=f"{actor}-dead-pane")
        except Exception as exc:
            logger.warning(
                f"Golden Throne: failed to schedule dead-pane follow-up for "
                f"{session_id[:12]}: {exc}"
            )
    return True


# Legion → Discord bot name mapping
_LEGION_BOT_MAP = {
    "custodes": "custodes",
    "mechanicus": "mechanicus",
    "astartes": "mechanicus",
    "inquisition": "inquisition",
}


def _normalize_text(value: Any) -> str | None:
    """Normalize launcher metadata so empty strings persist as NULL."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_or_none(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _run_git_value(working_dir: str | None, args: list[str], *, timeout: int = 2) -> str | None:
    if not working_dir or not Path(working_dir).is_dir():
        return None
    try:
        result = subprocess.run(
            ["git", "-C", working_dir, *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    text = result.stdout.strip()
    return text or None


def _git_changed_files(working_dir: str | None, *, limit: int = 12) -> list[str]:
    status = _run_git_value(working_dir, ["status", "--short"], timeout=2)
    if not status:
        return []
    files: list[str] = []
    for line in status.splitlines():
        text = line[3:].strip() if len(line) > 3 else line.strip()
        if " -> " in text:
            text = text.split(" -> ", 1)[1]
        if text:
            files.append(text)
        if len(files) >= limit:
            break
    return files


def _render_state_injection(kind: str, payload: dict) -> str:
    if kind == "child_stopped":
        child = payload.get("child_instance_id") or "unknown"
        doc = payload.get("child_session_doc_path") or payload.get("child_session_doc_id")
        reason = payload.get("exit_reason") or "unknown"
        files = payload.get("files_changed_summary") or []
        file_text = ", ".join(str(item) for item in files[:8]) if files else "none reported"
        lines = [
            "<system-reminder>",
            "A dispatched child instance stopped.",
            f"- child_instance_id: {child}",
            f"- exit_reason: {reason}",
            f"- session_doc: {doc or 'unknown'}",
            f"- files_changed_summary: {file_text}",
        ]
        if payload.get("last_commit"):
            lines.append(f"- last_commit: {payload['last_commit']}")
        if payload.get("exit_summary"):
            lines.append(f"- exit_summary: {payload['exit_summary']}")
        lines.append("</system-reminder>")
        return "\n".join(lines)
    return f"<system-reminder>\nState injection: {kind}\n{json.dumps(payload, sort_keys=True)}\n</system-reminder>"


async def _enqueue_state_injection(
    db,
    *,
    audience_instance_id: str,
    source_instance_id: str | None,
    kind: str,
    payload: dict,
) -> int:
    rendered_text = _render_state_injection(kind, payload)
    cursor = await db.execute(
        """INSERT INTO state_injections
           (audience_instance_id, source_instance_id, kind, payload_json, rendered_text)
           VALUES (?, ?, ?, ?, ?)""",
        (
            audience_instance_id,
            source_instance_id,
            kind,
            json.dumps(payload, sort_keys=True),
            rendered_text,
        ),
    )
    return int(cursor.lastrowid)


async def _consume_state_injections(db, audience_instance_id: str) -> list[dict]:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """SELECT id, source_instance_id, kind, payload_json, rendered_text, created_at
           FROM state_injections
           WHERE audience_instance_id = ? AND status = 'pending'
           ORDER BY created_at ASC, id ASC
           LIMIT 10""",
        (audience_instance_id,),
    )
    rows = await cursor.fetchall()
    if not rows:
        return []
    ids = [row["id"] for row in rows]
    placeholders = ",".join("?" for _ in ids)
    await db.execute(
        f"""UPDATE state_injections
            SET status = 'consumed', consumed_at = ?
            WHERE id IN ({placeholders})""",
        [datetime.now().isoformat(), *ids],
    )
    return [
        {
            "id": row["id"],
            "source_instance_id": row["source_instance_id"],
            "kind": row["kind"],
            "payload": _json_or_none(row["payload_json"]) or {},
            "rendered_text": row["rendered_text"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


async def _enqueue_child_stop_fanout(instance: dict, payload: dict) -> dict | None:
    parent_instance_id = _normalize_text(instance.get("parent_instance_id"))
    if not parent_instance_id:
        return None

    child_instance_id = instance["id"]
    child_session_doc_path = None
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        if instance.get("session_doc_id"):
            cursor = await db.execute(
                "SELECT file_path FROM session_documents WHERE id = ?",
                (instance["session_doc_id"],),
            )
            row = await cursor.fetchone()
            if row:
                child_session_doc_path = row["file_path"]

        exit_code = payload.get("exit_code")
        exit_reason = payload.get("exit_reason")
        if not exit_reason:
            exit_reason = "errored" if exit_code not in (None, 0, "0") else "normal"
        injection_payload = {
            "kind": "child_stopped",
            "child_instance_id": child_instance_id,
            "child_session_doc_id": instance.get("session_doc_id"),
            "child_session_doc_path": child_session_doc_path,
            "exit_reason": exit_reason,
            "last_commit": _run_git_value(
                instance.get("working_dir"), ["rev-parse", "--short", "HEAD"]
            ),
            "files_changed_summary": _git_changed_files(instance.get("working_dir")),
            "exit_summary": payload.get("exit_summary"),
        }
        injection_id = await _enqueue_state_injection(
            db,
            audience_instance_id=parent_instance_id,
            source_instance_id=child_instance_id,
            kind="child_stopped",
            payload=injection_payload,
        )
        await db.commit()

    await log_event(
        "state_injection_enqueued",
        instance_id=child_instance_id,
        details={
            "audience_instance_id": parent_instance_id,
            "kind": "child_stopped",
            "injection_id": injection_id,
            "payload": injection_payload,
        },
    )
    return {
        "injection_id": injection_id,
        "audience_instance_id": parent_instance_id,
        "payload": injection_payload,
    }


def _derive_continuity_binding_source(session_doc_policy: str | None) -> str | None:
    """Collapse session-doc policy variants into the higher-level ownership classes."""
    if not session_doc_policy:
        return None
    if session_doc_policy == "dispatch_explicit":
        return "dispatch"
    if session_doc_policy == "daily_note_custodes":
        return "daily_note"
    if session_doc_policy in {"manual_assigned", "manual_created"}:
        return "manual"
    return "auto_created"


def _derive_launch_workflow_state(
    *,
    dispatch_target: str | None,
    engine: str | None,
    launch_mode: str | None,
    working_dir: str | None,
    target_working_dir: str | None,
) -> str | None:
    """Return the coarse workflow state for a fresh launch registration."""
    if not dispatch_target:
        return None
    if launch_mode == "direct_target" or engine == "codex":
        return "worktree"
    if working_dir and target_working_dir and Path(working_dir) == Path(target_working_dir):
        return "worktree"
    return "dispatching"


async def _apply_instance_workflow_state(
    db,
    *,
    instance_id: str,
    session_doc_id: int | None,
    session_doc_policy: str | None,
    workflow_state: str | None,
    previous_session_doc_id: int | None = None,
    previous_workflow_state: str | None = None,
    event_owner: str = "hooks",
):
    """Persist coarse continuity/workflow fields and emit workflow events."""
    continuity_binding_source = _derive_continuity_binding_source(session_doc_policy)
    now = datetime.now().isoformat()
    workflow_events = []
    if session_doc_id:
        workflow_events.append(
            {
                "workflow_state": workflow_state,
                "event_type": "session_doc_bound",
                "event_owner": event_owner,
                "details": {
                    "session_doc_id": session_doc_id,
                    "session_doc_policy": session_doc_policy,
                    "continuity_binding_source": continuity_binding_source,
                },
            }
        )
    if previous_session_doc_id != session_doc_id:
        workflow_events.append(
            {
                "workflow_state": workflow_state,
                "event_type": "continuity_binding_changed",
                "event_owner": event_owner,
                "details": {
                    "old_session_doc_id": previous_session_doc_id,
                    "new_session_doc_id": session_doc_id,
                    "continuity_binding_source": continuity_binding_source,
                    "session_doc_policy": session_doc_policy,
                },
            }
        )
    if workflow_state and previous_workflow_state != workflow_state:
        workflow_events.append(
            {
                "workflow_state": workflow_state,
                "event_type": "workflow_state_changed",
                "event_owner": event_owner,
                "details": {
                    "old_workflow_state": previous_workflow_state,
                    "new_workflow_state": workflow_state,
                },
            }
        )

    updates = {
        "session_doc_id": session_doc_id,
        "session_doc_policy": session_doc_policy,
        "continuity_binding_source": continuity_binding_source,
        "workflow_state": workflow_state,
        "workflow_blocked_reason": None,
        "stop_allowed": 1,
        "next_required_action": None,
        "next_action_owner": None,
    }
    if workflow_state is not None:
        updates["workflow_updated_at"] = now

    await sanctioned_update_instance(
        db,
        instance_id=instance_id,
        updates=updates,
        mutation_type="continuity_binding_changed"
        if previous_session_doc_id != session_doc_id
        else "instance_updated",
        write_source="hooks",
        actor=event_owner,
        workflow_events=workflow_events,
    )


async def handle_wrapper_start(payload: dict) -> dict:
    """Handle wrapper-level launch telemetry without creating an instance row."""
    wrapper_launch_id = _normalize_text(
        payload.get("wrapper_launch_id")
        or payload.get("env", {}).get("TOKEN_API_WRAPPER_LAUNCH_ID", "")
    )
    details = {
        "wrapper_launch_id": wrapper_launch_id,
        "launcher": _normalize_text(
            payload.get("launcher") or payload.get("env", {}).get("TOKEN_API_LAUNCHER", "")
        ),
        "engine": _normalize_text(
            payload.get("engine") or payload.get("env", {}).get("TOKEN_API_ENGINE", "")
        ),
        "cwd": _normalize_text(payload.get("cwd")),
        "tmux_pane": _normalize_text(
            payload.get("tmux_pane") or payload.get("env", {}).get("TMUX_PANE", "")
        ),
        "pid": payload.get("pid"),
        "source": "wrapper",
    }
    await log_event("wrapper_start", details=details)
    return {
        "success": True,
        "action": "wrapper_start_logged",
        "wrapper_launch_id": wrapper_launch_id,
    }


async def handle_wrapper_end(payload: dict) -> dict:
    """Handle wrapper-level exit telemetry without stopping an instance row."""
    wrapper_launch_id = _normalize_text(
        payload.get("wrapper_launch_id")
        or payload.get("env", {}).get("TOKEN_API_WRAPPER_LAUNCH_ID", "")
    )
    details = {
        "wrapper_launch_id": wrapper_launch_id,
        "launcher": _normalize_text(
            payload.get("launcher") or payload.get("env", {}).get("TOKEN_API_LAUNCHER", "")
        ),
        "engine": _normalize_text(
            payload.get("engine") or payload.get("env", {}).get("TOKEN_API_ENGINE", "")
        ),
        "cwd": _normalize_text(payload.get("cwd")),
        "tmux_pane": _normalize_text(
            payload.get("tmux_pane") or payload.get("env", {}).get("TMUX_PANE", "")
        ),
        "pid": payload.get("pid"),
        "exit_code": payload.get("exit_code"),
        "source": "wrapper",
    }
    await log_event("wrapper_end", details=details)
    return {"success": True, "action": "wrapper_end_logged", "wrapper_launch_id": wrapper_launch_id}


# ============ Session Doc Pool Derivation ============


def _derive_pool(working_dir: str | None) -> str:
    """Derive pool from working directory: civic/pax dirs → pk, everything else → personal."""
    if not working_dir:
        return "personal"
    wd_lower = working_dir.lower()
    if any(tok in wd_lower for tok in ("pax-env", "askcivic", "/civic/", "/pax/")):
        return "pk"
    return "personal"


def _parse_launch_zealotry(value: Any) -> int | None:
    try:
        zealotry = int(value)
    except (TypeError, ValueError):
        return None
    if 1 <= zealotry <= 10:
        return zealotry
    return None


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

    # Capture tmux pane for Golden Throne transport and cross-machine dispatch
    # Claude Code strips $TMUX_PANE from hook env, so also check top-level payload
    # (hook resolves pane via PID walk and injects it directly)
    tmux_pane = (
        env.get("TMUX_PANE")
        or payload.get("tmux_pane")
        or env.get("TOKEN_API_DISPATCH_RESOLVED_PANE")
    )
    pane_label = payload.get("pane_label") or env.get("TOKEN_API_PANE_LABEL")
    if not pane_label:
        pane_label = await _tmux_pane_label(tmux_pane)

    # Auto-name subagents
    if is_subagent and not payload.get("env", {}).get("CLAUDE_TAB_NAME"):
        tab_name = f"sub: {subagent_env or 'agent'}"

    # Resolve device_id from HTTP client IP (where the instance actually runs)
    # SSH_CLIENT gives the SSH origin (Mac), not the instance's machine (WSL)
    client_ip = payload.get("_client_ip")
    if not source_ip:
        source_ip = client_ip
    device_id = resolve_device_from_ip(client_ip) if client_ip else "Mac-Mini"

    # Detect primarch (env var) and transplant-from (file-based handoff injected by hook)
    primarch_name = env.get("TOKEN_API_PRIMARCH", "")
    transplant_from = payload.get("transplant_from", "")
    launcher = _normalize_text(payload.get("launcher") or env.get("TOKEN_API_LAUNCHER", ""))
    engine = _normalize_text(payload.get("engine") or env.get("TOKEN_API_ENGINE", ""))
    dispatch_target = _normalize_text(
        payload.get("dispatch_target") or env.get("TOKEN_API_DISPATCH_TARGET", "")
    )
    dispatch_window = _normalize_text(
        payload.get("dispatch_window") or env.get("TOKEN_API_DISPATCH_WINDOW", "")
    )
    dispatch_mode = _normalize_text(
        payload.get("dispatch_mode") or env.get("TOKEN_API_DISPATCH_MODE", "")
    )
    dispatch_slot = _normalize_text(
        payload.get("dispatch_slot") or env.get("TOKEN_API_DISPATCH_SLOT", "")
    )
    dispatch_session_doc_path = _normalize_text(
        payload.get("dispatch_session_doc_path")
        or env.get("TOKEN_API_DISPATCH_SESSION_DOC_PATH", "")
    )
    target_working_dir = _normalize_text(
        payload.get("target_working_dir") or env.get("TOKEN_API_TARGET_WORKING_DIR", "")
    )
    launch_mode = _normalize_text(
        payload.get("launch_mode") or env.get("TOKEN_API_LAUNCH_MODE", "")
    )
    wrapper_launch_id = _normalize_text(
        payload.get("wrapper_launch_id") or env.get("TOKEN_API_WRAPPER_LAUNCH_ID", "")
    )
    parent_instance_id = _normalize_text(
        payload.get("parent_instance_id") or env.get("TOKEN_API_PARENT_INSTANCE_ID", "")
    )
    transplant_expected_raw = payload.get("transplant_expected")
    if transplant_expected_raw is None:
        transplant_expected_raw = env.get("TOKEN_API_TRANSPLANT_EXPECTED", "")
    transplant_expected = str(transplant_expected_raw).lower() in {"1", "true", "yes"}
    launch_instance_type = _normalize_text(
        payload.get("instance_type") or env.get("TOKEN_API_INSTANCE_TYPE", "")
    )
    if launch_instance_type not in VALID_LAUNCH_INSTANCE_TYPES:
        launch_instance_type = None
    launch_zealotry = _parse_launch_zealotry(
        payload.get("zealotry") or env.get("TOKEN_API_ZEALOTRY", "")
    )
    session_doc_policy = None
    dispatch_bound_doc = False

    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row

        # Check if already registered
        cursor = await db.execute("SELECT * FROM claude_instances WHERE id = ?", (session_id,))
        existing_row = await cursor.fetchone()

        # --- Supplant logic: reuse existing instance row instead of creating new ---
        # Priority: DB transplant marker > hook file handoff > primarch singleton
        supplant_id = None

        # 1. Check DB for pending transplant targeting this session (cross-device safe)
        cursor = await db.execute(
            "SELECT id FROM claude_instances WHERE transplant_target_session = ?", (session_id,)
        )
        db_transplant_row = await cursor.fetchone()
        if db_transplant_row:
            supplant_id = db_transplant_row["id"]
            # Clear the marker
            await sanctioned_update_instance(
                db,
                instance_id=supplant_id,
                updates={"transplant_target_session": None},
                mutation_type="instance_updated",
                write_source="hooks",
                actor="SessionStart",
            )

        # 2. File-based handoff (local transplant — injected by generic-hook.sh)
        if not supplant_id and transplant_from:
            supplant_id = transplant_from

        # 3. Primarch singleton (reuse most recent instance with same primarch)
        if not supplant_id and primarch_name:
            # Primarch singleton: find most recent instance with this primarch name
            cursor = await db.execute(
                "SELECT id FROM claude_instances WHERE primarch = ? ORDER BY registered_at DESC LIMIT 1",
                (primarch_name,),
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
                resolved_session_doc_id = None
                resolved_session_doc_policy = None
                if dispatch_session_doc_path or primarch_name or origin_type == "cron":
                    (
                        resolved_session_doc_id,
                        resolved_session_doc_policy,
                    ) = await resolve_session_doc_for_start(
                        db,
                        dispatch_session_doc_path=dispatch_session_doc_path,
                        primarch_name=primarch_name or None,
                        origin_type=origin_type,
                        cron_job_id=env.get("CRON_JOB_ID"),
                        cron_job_name=env.get("CRON_JOB_NAME", "cron"),
                        working_dir=working_dir,
                        is_subagent=bool(is_subagent),
                    )
                    session_doc_policy = resolved_session_doc_policy or session_doc_policy
                workflow_state = _derive_launch_workflow_state(
                    dispatch_target=dispatch_target,
                    engine=engine,
                    launch_mode=launch_mode,
                    working_dir=working_dir,
                    target_working_dir=target_working_dir,
                )

                old_tmux_pane = existing_row["tmux_pane"]

                # Same-ID transplant (--continue): update the existing row in-place
                now = datetime.now().isoformat()
                await sanctioned_update_instance(
                    db,
                    instance_id=session_id,
                    updates={
                        "working_dir": working_dir,
                        "pid": payload.get("pid"),
                        "device_id": device_id,
                        "status": "idle",
                        "tmux_pane": tmux_pane,
                        "pane_label": pane_label or existing_row["pane_label"],
                        "last_activity": now,
                        "stopped_at": None,
                        "victory_at": None,
                        "victory_reason": None,
                        "input_lock": None,
                        "transplant_target_session": None,
                        "primarch": primarch_name or existing_row["primarch"]
                        if hasattr(existing_row, "__getitem__")
                        else primarch_name,
                        "session_doc_id": resolved_session_doc_id or existing_row["session_doc_id"],
                        "wrapper_launch_id": wrapper_launch_id or existing_row["wrapper_launch_id"],
                        "launcher": launcher or existing_row["launcher"],
                        "engine": engine or existing_row["engine"],
                        "dispatch_target": dispatch_target or existing_row["dispatch_target"],
                        "dispatch_window": dispatch_window or existing_row["dispatch_window"],
                        "dispatch_mode": dispatch_mode or existing_row["dispatch_mode"],
                        "dispatch_slot": dispatch_slot or existing_row["dispatch_slot"],
                        "dispatch_session_doc_path": dispatch_session_doc_path
                        or existing_row["dispatch_session_doc_path"],
                        "target_working_dir": target_working_dir
                        or existing_row["target_working_dir"],
                        "launch_mode": launch_mode or existing_row["launch_mode"],
                        "parent_instance_id": parent_instance_id
                        or existing_row["parent_instance_id"],
                        "transplant_expected": 1 if transplant_expected else 0,
                        "instance_type": launch_instance_type or existing_row["instance_type"],
                        "zealotry": launch_zealotry
                        if launch_zealotry is not None
                        else existing_row["zealotry"],
                        "session_doc_policy": session_doc_policy
                        or existing_row["session_doc_policy"],
                    },
                    mutation_type="instance_updated",
                    write_source="hooks",
                    actor="SessionStart",
                    wrapper_launch_id=wrapper_launch_id or existing_row["wrapper_launch_id"],
                )
                await _apply_instance_workflow_state(
                    db,
                    instance_id=session_id,
                    session_doc_id=resolved_session_doc_id or existing_row["session_doc_id"],
                    session_doc_policy=session_doc_policy or existing_row["session_doc_policy"],
                    workflow_state=workflow_state,
                    previous_session_doc_id=existing_row["session_doc_id"],
                    previous_workflow_state=existing_row["workflow_state"],
                )
                await db.commit()

                # Queue legion pane recolor (tmux_pane changed, trigger won't fire since legion didn't)
                _transplant_legion = (
                    existing_row["legion"]
                    if hasattr(existing_row, "__getitem__") and existing_row["legion"]
                    else "astartes"
                )
                if _transplant_legion != "astartes" and tmux_pane:
                    await db.execute(
                        "INSERT INTO pane_recolor_queue (instance_id, legion, tmux_pane) VALUES (?, ?, ?)",
                        (session_id, _transplant_legion, tmux_pane),
                    )
                if (
                    old_tmux_pane
                    and old_tmux_pane != tmux_pane
                    and _transplant_legion != "astartes"
                ):
                    await db.execute(
                        "INSERT INTO pane_recolor_queue (instance_id, legion, tmux_pane) VALUES (?, 'astartes', ?)",
                        (session_id, old_tmux_pane),
                    )
                await db.commit()

                # Resolve preserved profile for color
                cursor = await db.execute(
                    "SELECT * FROM claude_instances WHERE id = ?", (session_id,)
                )
                updated_inst = await cursor.fetchone()
                cc_color = "default"
                hex_color = "#666666"
                if updated_inst and updated_inst["profile_name"]:
                    for p in PROFILES + FALLBACK_VOICES + [ULTIMATE_FALLBACK]:
                        if p["name"] == updated_inst["profile_name"]:
                            cc_color = p.get("cc_color", "default")
                            hex_color = p.get("color", "#666666")
                            break

                logger.info(
                    f"Hook: SessionStart transplant-refresh {session_id[:12]}... ({working_dir}) [device:{device_id}]"
                )
                return {
                    "success": True,
                    "action": "transplant_refreshed",
                    "instance_id": session_id,
                    "profile": updated_inst["profile_name"] if updated_inst else None,
                    "color": hex_color,
                    "cc_color": cc_color,
                    "session_doc_id": updated_inst["session_doc_id"] if updated_inst else None,
                }
            else:
                # Normal re-registration / Codex resume. Refresh transport fields so
                # a live pane cannot remain represented by a stale stopped row.
                now = datetime.now().isoformat()
                updates = {
                    "working_dir": working_dir,
                    "pid": payload.get("pid") or existing_row["pid"],
                    "device_id": device_id,
                    "status": "idle",
                    "tmux_pane": tmux_pane or existing_row["tmux_pane"],
                    "pane_label": pane_label or existing_row["pane_label"],
                    "last_activity": now,
                    "stopped_at": None,
                    "victory_at": None,
                    "victory_reason": None,
                    "input_lock": None,
                    "wrapper_launch_id": wrapper_launch_id or existing_row["wrapper_launch_id"],
                    "launcher": launcher or existing_row["launcher"],
                    "engine": engine or existing_row["engine"],
                    "dispatch_target": dispatch_target or existing_row["dispatch_target"],
                    "dispatch_window": dispatch_window or existing_row["dispatch_window"],
                    "dispatch_mode": dispatch_mode or existing_row["dispatch_mode"],
                    "dispatch_slot": dispatch_slot or existing_row["dispatch_slot"],
                    "dispatch_session_doc_path": dispatch_session_doc_path
                    or existing_row["dispatch_session_doc_path"],
                    "target_working_dir": target_working_dir or existing_row["target_working_dir"],
                    "launch_mode": launch_mode or existing_row["launch_mode"],
                    "parent_instance_id": parent_instance_id or existing_row["parent_instance_id"],
                    "transplant_expected": 1
                    if transplant_expected
                    else existing_row["transplant_expected"],
                    "instance_type": launch_instance_type or existing_row["instance_type"],
                    "zealotry": launch_zealotry
                    if launch_zealotry is not None
                    else existing_row["zealotry"],
                    "session_doc_policy": session_doc_policy or existing_row["session_doc_policy"],
                }
                await sanctioned_update_instance(
                    db,
                    instance_id=session_id,
                    updates=updates,
                    mutation_type="instance_updated",
                    write_source="hooks",
                    actor="SessionStart",
                    wrapper_launch_id=wrapper_launch_id or existing_row["wrapper_launch_id"],
                )
                await db.commit()
                await log_event(
                    "instance_reregistered",
                    instance_id=session_id,
                    device_id=device_id,
                    details={
                        "source": "hook",
                        "engine": engine or existing_row["engine"],
                        "tmux_pane": tmux_pane or existing_row["tmux_pane"],
                        "pane_label": pane_label or existing_row["pane_label"],
                        "was_status": existing_row["status"],
                    },
                )
                return {
                    "success": True,
                    "action": "reregistered",
                    "instance_id": session_id,
                    "pane_label": pane_label or existing_row["pane_label"],
                }

        if supplant_id:
            # Fetch the old instance to preserve its config
            cursor = await db.execute("SELECT * FROM claude_instances WHERE id = ?", (supplant_id,))
            old_inst = await cursor.fetchone()

            if old_inst:
                now = datetime.now().isoformat()
                internal_session_id = str(uuid.uuid4())
                resolved_session_doc_id = None
                resolved_session_doc_policy = None
                if dispatch_session_doc_path or primarch_name or origin_type == "cron":
                    (
                        resolved_session_doc_id,
                        resolved_session_doc_policy,
                    ) = await resolve_session_doc_for_start(
                        db,
                        dispatch_session_doc_path=dispatch_session_doc_path,
                        primarch_name=primarch_name or None,
                        origin_type=origin_type,
                        cron_job_id=env.get("CRON_JOB_ID"),
                        cron_job_name=env.get("CRON_JOB_NAME", "cron"),
                        working_dir=working_dir,
                        is_subagent=bool(is_subagent),
                    )
                    session_doc_policy = resolved_session_doc_policy or session_doc_policy
                workflow_state = _derive_launch_workflow_state(
                    dispatch_target=dispatch_target,
                    engine=engine,
                    launch_mode=launch_mode,
                    working_dir=working_dir,
                    target_working_dir=target_working_dir,
                )

                old_tmux_pane = old_inst["tmux_pane"]

                # Update the old row with new session identity, preserve config
                await sanctioned_update_instance(
                    db,
                    instance_id=supplant_id,
                    updates={
                        "id": session_id,
                        "session_id": internal_session_id,
                        "working_dir": working_dir,
                        "pid": payload.get("pid"),
                        "status": "idle",
                        "tmux_pane": tmux_pane,
                        "pane_label": pane_label or old_inst["pane_label"],
                        "device_id": device_id,
                        "registered_at": now,
                        "last_activity": now,
                        "stopped_at": None,
                        "victory_at": None,
                        "victory_reason": None,
                        "input_lock": None,
                        "primarch": primarch_name or old_inst["primarch"],
                        "session_doc_id": resolved_session_doc_id or old_inst["session_doc_id"],
                        "wrapper_launch_id": wrapper_launch_id or old_inst["wrapper_launch_id"],
                        "launcher": launcher or old_inst["launcher"],
                        "engine": engine or old_inst["engine"],
                        "dispatch_target": dispatch_target or old_inst["dispatch_target"],
                        "dispatch_window": dispatch_window or old_inst["dispatch_window"],
                        "dispatch_mode": dispatch_mode or old_inst["dispatch_mode"],
                        "dispatch_slot": dispatch_slot or old_inst["dispatch_slot"],
                        "dispatch_session_doc_path": dispatch_session_doc_path
                        or old_inst["dispatch_session_doc_path"],
                        "target_working_dir": target_working_dir or old_inst["target_working_dir"],
                        "launch_mode": launch_mode or old_inst["launch_mode"],
                        "parent_instance_id": parent_instance_id or old_inst["parent_instance_id"],
                        "transplant_expected": 1 if transplant_expected else 0,
                        "instance_type": launch_instance_type or old_inst["instance_type"],
                        "zealotry": launch_zealotry
                        if launch_zealotry is not None
                        else old_inst["zealotry"],
                        "session_doc_policy": session_doc_policy or old_inst["session_doc_policy"],
                    },
                    mutation_type="instance_updated",
                    write_source="hooks",
                    actor="SessionStart",
                    wrapper_launch_id=wrapper_launch_id or old_inst["wrapper_launch_id"],
                    where_clause="id = ?",
                    where_params=(supplant_id,),
                )

                # Auto-link primarch session doc if applicable
                session_doc_id = resolved_session_doc_id or old_inst["session_doc_id"]
                if primarch_name and not session_doc_id:
                    cursor = await db.execute(
                        "SELECT session_doc_id FROM primarch_session_docs WHERE primarch_name = ? AND unlinked_at IS NULL",
                        (primarch_name,),
                    )
                    link_row = await cursor.fetchone()
                    if link_row and link_row[0]:
                        session_doc_id = link_row[0]
                        await sanctioned_update_instance(
                            db,
                            instance_id=session_id,
                            updates={
                                "session_doc_id": session_doc_id,
                                "continuity_binding_source": "primarch",
                            },
                            mutation_type="continuity_binding_changed",
                            write_source="hooks",
                            actor="SessionStart",
                        )

                await _apply_instance_workflow_state(
                    db,
                    instance_id=session_id,
                    session_doc_id=session_doc_id,
                    session_doc_policy=session_doc_policy or old_inst["session_doc_policy"],
                    workflow_state=workflow_state,
                    previous_session_doc_id=old_inst["session_doc_id"],
                    previous_workflow_state=old_inst["workflow_state"],
                )
                dispatch_bound_doc = (
                    session_doc_policy or old_inst["session_doc_policy"]
                ) == "dispatch_explicit"

                await db.commit()

                # Queue legion pane recolor (tmux_pane changed via supplant, trigger won't fire)
                _supplant_legion = old_inst["legion"] if old_inst["legion"] else "astartes"
                if _supplant_legion != "astartes" and tmux_pane:
                    await db.execute(
                        "INSERT INTO pane_recolor_queue (instance_id, legion, tmux_pane) VALUES (?, ?, ?)",
                        (session_id, _supplant_legion, tmux_pane),
                    )
                if old_tmux_pane and old_tmux_pane != tmux_pane and _supplant_legion != "astartes":
                    await db.execute(
                        "INSERT INTO pane_recolor_queue (instance_id, legion, tmux_pane) VALUES (?, 'astartes', ?)",
                        (session_id, old_tmux_pane),
                    )
                await db.commit()

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

                supplant_source = (
                    f"transplant:{transplant_from}"
                    if transplant_from
                    else f"primarch:{primarch_name}"
                )
                logger.info(
                    f"Hook: SessionStart supplanted {supplant_id[:12]}... → {session_id[:12]}... ({working_dir}) [{supplant_source}]"
                )
                await log_event(
                    "instance_supplanted",
                    instance_id=session_id,
                    device_id=device_id,
                    details={
                        "old_id": supplant_id,
                        "tab_name": old_inst["tab_name"],
                        "source": supplant_source,
                        "primarch": primarch_name or None,
                    },
                )

                return {
                    "success": True,
                    "action": "supplanted",
                    "instance_id": session_id,
                    "supplanted_from": supplant_id,
                    "profile": preserved_profile,
                    "color": hex_color,
                    "cc_color": cc_color,
                    "session_doc_id": session_doc_id,
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
        _prior_wrapper_launch_id = None
        _prior_session_doc_policy = None
        _prior_session_doc_id = None
        _prior_workflow_state = None
        _prior_parent_instance_id = None
        _prior_dispatch = {}
        cursor = await db.execute(
            """SELECT discord_hosted, discord_channel, legion,
                      wrapper_launch_id,
                      launcher, engine, dispatch_target, dispatch_window,
                      dispatch_mode, dispatch_slot, dispatch_session_doc_path,
                      target_working_dir, launch_mode, transplant_expected,
                      session_doc_policy, session_doc_id, workflow_state,
                      parent_instance_id
               FROM claude_instances WHERE id = ?""",
            (session_id,),
        )
        _prior_row = await cursor.fetchone()
        if _prior_row:
            _prior_discord_hosted = _prior_row[0] or 0
            _prior_discord_channel = _prior_row[1]
            _prior_legion = _prior_row[2]
            _prior_wrapper_launch_id = _prior_row[3]
            _prior_dispatch = {
                "launcher": _prior_row[4],
                "engine": _prior_row[5],
                "dispatch_target": _prior_row[6],
                "dispatch_window": _prior_row[7],
                "dispatch_mode": _prior_row[8],
                "dispatch_slot": _prior_row[9],
                "dispatch_session_doc_path": _prior_row[10],
                "target_working_dir": _prior_row[11],
                "launch_mode": _prior_row[12],
                "transplant_expected": _prior_row[13] or 0,
            }
            _prior_session_doc_policy = _prior_row[14]
            _prior_session_doc_id = _prior_row[15]
            _prior_workflow_state = _prior_row[16]
            _prior_parent_instance_id = _prior_row[17]
            # Delete old row so INSERT succeeds (id is PRIMARY KEY)
            await sanctioned_delete_instance(
                db,
                instance_id=session_id,
                mutation_type="instance_replaced",
                write_source="hooks",
                actor="SessionStart",
                wrapper_launch_id=_prior_wrapper_launch_id,
            )

        wrapper_launch_id = wrapper_launch_id or _prior_wrapper_launch_id
        launcher = launcher or _prior_dispatch.get("launcher")
        engine = engine or _prior_dispatch.get("engine")
        dispatch_target = dispatch_target or _prior_dispatch.get("dispatch_target")
        dispatch_window = dispatch_window or _prior_dispatch.get("dispatch_window")
        dispatch_mode = dispatch_mode or _prior_dispatch.get("dispatch_mode")
        dispatch_slot = dispatch_slot or _prior_dispatch.get("dispatch_slot")
        dispatch_session_doc_path = dispatch_session_doc_path or _prior_dispatch.get(
            "dispatch_session_doc_path"
        )
        target_working_dir = target_working_dir or _prior_dispatch.get("target_working_dir")
        launch_mode = launch_mode or _prior_dispatch.get("launch_mode")
        parent_instance_id = parent_instance_id or _prior_parent_instance_id
        if not transplant_expected:
            transplant_expected = bool(_prior_dispatch.get("transplant_expected"))
        session_doc_policy = _prior_session_doc_policy

        # Insert instance
        now = datetime.now().isoformat()
        internal_session_id = str(uuid.uuid4())
        await sanctioned_insert_instance(
            db,
            values={
                "id": session_id,
                "session_id": internal_session_id,
                "tab_name": tab_name,
                "working_dir": working_dir,
                "origin_type": origin_type,
                "source_ip": source_ip,
                "device_id": device_id,
                "profile_name": profile["name"],
                "tts_voice": profile["wsl_voice"],
                "notification_sound": profile["notification_sound"],
                "pid": payload.get("pid"),
                "status": "idle",
                "legion": _prior_legion or "astartes",
                "synced": 0,
                "input_lock": None,
                "is_subagent": is_subagent,
                "tmux_pane": tmux_pane,
                "pane_label": pane_label,
                "primarch": primarch_name or None,
                "wrapper_launch_id": wrapper_launch_id,
                "launcher": launcher,
                "engine": engine,
                "dispatch_target": dispatch_target,
                "dispatch_window": dispatch_window,
                "dispatch_mode": dispatch_mode,
                "dispatch_slot": dispatch_slot,
                "dispatch_session_doc_path": dispatch_session_doc_path,
                "target_working_dir": target_working_dir,
                "launch_mode": launch_mode,
                "parent_instance_id": parent_instance_id,
                "transplant_expected": 1 if transplant_expected else 0,
                "instance_type": launch_instance_type or "one_off",
                "zealotry": launch_zealotry if launch_zealotry is not None else 4,
                "session_doc_policy": session_doc_policy,
                "discord_hosted": _prior_discord_hosted,
                "discord_channel": _prior_discord_channel,
                "registered_at": now,
                "last_activity": now,
            },
            mutation_type="instance_registered",
            write_source="hooks",
            actor="SessionStart",
            wrapper_launch_id=wrapper_launch_id,
        )
        # Auto-link primarch instance to its active session doc
        session_doc_id, resolved_session_doc_policy = await resolve_session_doc_for_start(
            db,
            dispatch_session_doc_path=dispatch_session_doc_path,
            primarch_name=primarch_name or None,
            origin_type=origin_type,
            cron_job_id=env.get("CRON_JOB_ID"),
            cron_job_name=env.get("CRON_JOB_NAME", "cron"),
            working_dir=working_dir,
            is_subagent=bool(is_subagent),
        )
        session_doc_policy = resolved_session_doc_policy or session_doc_policy
        workflow_state = _derive_launch_workflow_state(
            dispatch_target=dispatch_target,
            engine=engine,
            launch_mode=launch_mode,
            working_dir=working_dir,
            target_working_dir=target_working_dir,
        )
        dispatch_bound_doc = session_doc_policy == "dispatch_explicit"
        await _apply_instance_workflow_state(
            db,
            instance_id=session_id,
            session_doc_id=session_doc_id,
            session_doc_policy=session_doc_policy,
            workflow_state=workflow_state,
            previous_session_doc_id=_prior_session_doc_id,
            previous_workflow_state=_prior_workflow_state,
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
                auto_legion = cron_row[0] if cron_row and cron_row[0] else "mechanicus"
            else:
                auto_legion = "mechanicus"
        elif working_dir and ("pax-env" in working_dir.lower() or "/pax/" in working_dir.lower()):
            auto_legion = "civic"

        # Restore prior legion if no auto-detect, or apply auto-detect
        if auto_legion:
            await sanctioned_update_instance(
                db,
                instance_id=session_id,
                updates={"legion": auto_legion},
                mutation_type="instance_updated",
                write_source="hooks",
                actor="SessionStart",
                wrapper_launch_id=wrapper_launch_id,
            )
        elif _prior_legion and _prior_legion != "astartes":
            await sanctioned_update_instance(
                db,
                instance_id=session_id,
                updates={"legion": _prior_legion},
                mutation_type="instance_updated",
                write_source="hooks",
                actor="SessionStart",
                wrapper_launch_id=wrapper_launch_id,
            )

        await db.commit()

        # Update frontmatter if we linked a session doc
        if session_doc_id:
            await _update_doc_agents_list(db, session_doc_id)

            # Populate start_time and pool in session doc frontmatter
            cursor = await db.execute(
                "SELECT file_path FROM session_documents WHERE id = ?", (session_doc_id,)
            )
            doc_row = await cursor.fetchone()
            if doc_row and doc_row[0]:
                fp = Path(doc_row[0])
                if fp.exists():
                    start_time = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                    pool = _derive_pool(working_dir)
                    fm_updates = {"start_time": start_time, "pool": pool}
                    if auto_legion:
                        fm_updates["legion"] = auto_legion
                    if primarch_name:
                        fm_updates["primarch"] = primarch_name
                    await asyncio.to_thread(update_frontmatter, fp, fm_updates)

    logger.info(
        f"Hook: SessionStart registered {session_id[:12]}... ({working_dir})"
        f"{' [subagent]' if is_subagent else ''}"
        f"{f' [primarch:{primarch_name}]' if primarch_name else ''}"
        f"{f' [legion:{auto_legion}]' if auto_legion else ''}"
        f"{f' [launcher:{launcher}]' if launcher else ''}"
        f"{f' [dispatch:{dispatch_target}]' if dispatch_target else ''}"
    )
    await log_event(
        "instance_registered",
        instance_id=session_id,
        device_id=device_id,
        details={
            "tab_name": tab_name,
            "origin_type": origin_type,
            "source": "hook",
            "is_subagent": is_subagent,
            "subagent_env": subagent_env or None,
            "primarch": primarch_name or None,
            "launcher": launcher or None,
            "wrapper_launch_id": wrapper_launch_id or None,
            "engine": engine or None,
            "dispatch_target": dispatch_target or None,
            "dispatch_window": dispatch_window or None,
            "dispatch_mode": dispatch_mode or None,
            "dispatch_slot": dispatch_slot or None,
            "dispatch_session_doc_path": dispatch_session_doc_path or None,
            "target_working_dir": target_working_dir or None,
            "launch_mode": launch_mode or None,
            "parent_instance_id": parent_instance_id or None,
            "transplant_expected": transplant_expected,
            "instance_type": launch_instance_type or "one_off",
            "zealotry": launch_zealotry if launch_zealotry is not None else 4,
            "dispatch_bound_doc": dispatch_bound_doc,
            "session_doc_policy": session_doc_policy,
        },
    )

    return {
        "success": True,
        "action": "registered",
        "instance_id": session_id,
        "profile": profile["name"] if not is_subagent else None,
        "color": profile.get("color") if not is_subagent else None,
        "cc_color": profile.get("cc_color") if not is_subagent else None,
        "session_doc_id": session_doc_id,
    }


async def handle_session_end(payload: dict) -> dict:
    """Handle SessionEnd hook - deregister Claude instance."""
    session_id = payload.get("session_id") or payload.get("conversation_id")
    if not session_id:
        return {"success": False, "action": "no_session_id"}

    _pending_background_tasks.pop(session_id, None)

    now = datetime.now().isoformat()

    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        cursor = await db.execute(
            """SELECT id, device_id, COALESCE(is_subagent, 0), session_doc_id,
                      tmux_pane, legion, workflow_state
               FROM claude_instances WHERE id = ?""",
            (session_id,),
        )
        row = await cursor.fetchone()

        if not row:
            return {"success": False, "action": "not_found", "instance_id": session_id}

        is_subagent = row[2]
        session_doc_id = row[3]
        _stop_pane = row[4]
        _stop_legion = row[5] or "astartes"
        _prior_workflow_state = row[6]

        # Populate end_time and duration_minutes in session doc frontmatter
        if session_doc_id and not is_subagent:
            cursor = await db.execute(
                "SELECT file_path FROM session_documents WHERE id = ?", (session_doc_id,)
            )
            doc_row = await cursor.fetchone()
            if doc_row and doc_row[0]:
                fp = Path(doc_row[0])
                if fp.exists():
                    end_time = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                    fm_updates = {"end_time": end_time}
                    # Read start_time to compute duration
                    try:
                        fm, _ = read_frontmatter(fp)
                        start_time = fm.get("start_time")
                        if start_time and start_time != "null":
                            start_dt = datetime.fromisoformat(
                                str(start_time).replace("Z", "+00:00")
                            )
                            end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                            fm_updates["duration_minutes"] = round(
                                (end_dt - start_dt).total_seconds() / 60
                            )
                    except Exception:
                        pass
                    await asyncio.to_thread(update_frontmatter, fp, fm_updates)

        # Count non-subagent active instances BEFORE stopping
        cursor = await db.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle') AND COALESCE(is_subagent, 0) = 0"
        )
        count_row = await cursor.fetchone()
        was_active = count_row[0] if count_row else 0

        workflow_events = [
            {
                "workflow_state": "closed",
                "event_type": "workflow_closed",
                "event_owner": "hooks",
                "details": {"source": "session_end"},
            }
        ]
        if _prior_workflow_state != "closed":
            workflow_events.append(
                {
                    "workflow_state": "closed",
                    "event_type": "workflow_state_changed",
                    "event_owner": "hooks",
                    "details": {
                        "old_workflow_state": _prior_workflow_state,
                        "new_workflow_state": "closed",
                    },
                }
            )
        await sanctioned_update_instance(
            db,
            instance_id=session_id,
            updates={
                "status": "stopped",
                "synced": 0,
                "stopped_at": now,
                "workflow_state": "closed",
                "workflow_updated_at": now,
                "workflow_blocked_reason": None,
                "stop_allowed": 1,
                "next_required_action": None,
                "next_action_owner": None,
            },
            mutation_type="instance_stopped",
            write_source="hooks",
            actor="SessionEnd",
            wrapper_launch_id=payload.get("wrapper_launch_id"),
            workflow_events=workflow_events,
        )

        # Reset pane background on stop (clear legion tint so stale colors don't linger)
        if _stop_pane and _stop_legion != "astartes":
            await db.execute(
                "INSERT INTO pane_recolor_queue (instance_id, legion, tmux_pane) VALUES (?, 'astartes', ?)",
                (session_id, _stop_pane),
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
        shared.scheduler.remove_job(f"golden-throne-{session_id}")
        logger.info(f"Golden Throne: cancelled follow-up for {session_id[:12]} (session end)")
    except Exception:
        pass

    logger.info(f"Hook: SessionEnd stopped {session_id[:12]}...")
    await log_event(
        "instance_stopped", instance_id=session_id, device_id=row[1], details={"source": "hook"}
    )

    # Instance count Pavlok signals (skip subagents)
    if not is_subagent:
        await check_instance_count_pavlok(remaining_non_sub, was_active)

    # Spawn stop_hook.py to generate transcript + wikilink (session doc or daily note fallback)
    if not is_subagent:
        stop_hook_script = Path(__file__).parent / "stop_hook.py"
        if stop_hook_script.exists():
            try:
                with open("/tmp/stop_hook.log", "a") as log_handle:
                    await asyncio.to_thread(
                        subprocess.Popen,
                        ["python3", str(stop_hook_script), session_id],
                        stdout=subprocess.DEVNULL,
                        stderr=log_handle,
                        start_new_session=True,
                    )
                logger.info(
                    f"Hook: SessionEnd spawned stop_hook for {session_id[:12]}... (doc {session_doc_id or 'none, daily note fallback'})"
                )
            except Exception as e:
                logger.warning(f"Hook: SessionEnd failed to spawn stop_hook: {e}")

    # Handle productivity enforcement if needed
    result = {"success": True, "action": "stopped", "instance_id": session_id}
    if remaining_active == 0 and DESKTOP_STATE.get("current_mode") == "video":
        enforce_result = close_distraction_windows()
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
        logger.info(
            f"PromptSubmit: background task returned for {session_id[:12]} (pending: {_pending_background_tasks.get(session_id, 0)})"
        )

    now = datetime.now().isoformat()
    consumed_injections: list[dict] = []

    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM claude_instances WHERE id = ?", (session_id,))
        existing = await cursor.fetchone()
        if not existing:
            return {"success": False, "action": "not_found"}
        existing_dict = dict(existing)
        if await _stop_if_dead_pane(db, session_id, existing_dict, "PromptSubmit"):
            return {
                "success": True,
                "action": "ignored_dead_pane",
                "instance_id": session_id,
            }

        consumed_injections = await _consume_state_injections(db, session_id)

        # Also resurrect stopped instances - activity means they're active
        # Backfill PID if payload contains one and DB value is NULL
        await sanctioned_update_instance(
            db,
            instance_id=session_id,
            updates={
                "status": "processing",
                "last_activity": now,
                "stopped_at": None,
                "pid": existing_dict.get("pid") or payload.get("pid"),
            },
            mutation_type="status_changed",
            write_source="hooks",
            actor="PromptSubmit",
        )
        await db.commit()

    # Signal productivity — sets prod active, exits IDLE if needed
    now_ms = int(time.monotonic() * 1000)
    old_mode = shared.timer_engine.current_mode.value
    result = shared.timer_engine.set_productivity(True, now_ms)
    exited_idle = TimerEvent.MODE_CHANGED in result.events
    if exited_idle:
        new_mode = shared.timer_engine.current_mode.value
        await shared.timer_log_shift(old_mode, new_mode, trigger="prompt_submit", source="hook")
        logger.info(f"Hook: PromptSubmit exited {old_mode} → {new_mode}")
    if _work_action_callback:
        await _work_action_callback(source="prompt_submit", note=f"session_id={session_id}")

    # Golden Throne: cancel any pending follow-up (user is active)
    golden_throne_activity = None
    if existing_dict.get("instance_type") == "golden_throne" and _golden_throne_activity_callback:
        golden_throne_activity = await _golden_throne_activity_callback(
            session_id,
            source="prompt_submit",
        )
    try:
        shared.scheduler.remove_job(f"golden-throne-{session_id}")
        logger.info(f"Golden Throne: cancelled follow-up for {session_id[:12]} (user prompt)")
    except Exception:
        pass

    logger.info(f"Hook: PromptSubmit {session_id[:12]}... -> processing (resurrected if stopped)")
    response = {
        "success": True,
        "action": "processing",
        "instance_id": session_id,
        "exited_idle": exited_idle,
    }
    if golden_throne_activity:
        response["golden_throne"] = golden_throne_activity
    if consumed_injections:
        reminder_text = "\n\n".join(item["rendered_text"] for item in consumed_injections)
        response["state_injections"] = consumed_injections
        response["system_reminder"] = reminder_text
        response["additionalContext"] = reminder_text
        response["hookSpecificOutput"] = {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": reminder_text,
        }
        await log_event(
            "state_injection_consumed",
            instance_id=session_id,
            details={
                "count": len(consumed_injections),
                "injection_ids": [item["id"] for item in consumed_injections],
            },
        )
    return response


async def handle_post_tool_use(payload: dict) -> dict:
    """Handle PostToolUse hook - heartbeat with debouncing, ensures status='processing'."""
    session_id = payload.get("session_id")
    tool_name = payload.get("tool_name", "")
    if not session_id:
        return {"success": False, "action": "no_session_id"}

    # AskUserQuestion answered → cancel any active three-touch ladder.
    # Done before debounce so a quick-answered question always cancels.
    if tool_name == "AskUserQuestion" and session_id in ASKQ_LADDER:
        await _askq_ladder_cancel(
            session_id,
            reason="answered",
            answer=_askq_extract_answer(payload),
        )

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
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        existing = await _fetch_instance_row(db, session_id)
        if not existing:
            return {"success": False, "action": "not_found", "instance_id": session_id}
        if await _stop_if_dead_pane(db, session_id, existing, "PostToolUse"):
            return {
                "success": True,
                "action": "ignored_dead_pane",
                "instance_id": session_id,
            }
        await sanctioned_update_instance(
            db,
            instance_id=session_id,
            updates={
                "status": "processing",
                "last_activity": now,
                "stopped_at": None,
                "pid": existing.get("pid") or payload.get("pid"),
            },
            mutation_type="status_changed",
            write_source="hooks",
            actor="PostToolUse",
        )
        await db.commit()

    # Signal productivity — active tool use = real work
    now_ms = int(time.monotonic() * 1000)
    shared.timer_engine.set_productivity(True, now_ms)

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
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM claude_instances WHERE id = ?", (session_id,))
        instance = await cursor.fetchone()

    if not instance:
        return {"success": False, "action": "instance_not_found"}

    instance = dict(instance)
    device_id = instance.get("device_id", "Mac-Mini")
    tab_name = instance.get("tab_name", "Claude")
    notify_surface = _human_tab_name(tab_name) or session_id[:12]
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
    will_evaluate = (
        not is_subagent_instance_quick and not has_pending_background and not is_sync_instance
    )

    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        if will_evaluate or is_sync_instance:
            await sanctioned_update_instance(
                db,
                instance_id=session_id,
                updates={"last_activity": now},
                mutation_type="instance_updated",
                write_source="hooks",
                actor="Stop",
            )
        else:
            await sanctioned_update_instance(
                db,
                instance_id=session_id,
                updates={"status": "idle", "last_activity": now},
                mutation_type="status_changed",
                write_source="hooks",
                actor="Stop",
            )
        await db.commit()

    # Fire async stop evaluators (action_validator, plan_auditor, etc.)
    # Skips subagents, sync instances, and intermediate stops.
    if will_evaluate:
        session_doc_id = instance.get("session_doc_id")
        stop_context = (
            payload.get("transcript_tail", "")[:4000] if payload.get("transcript_tail") else ""
        )
        # Signal TUI that evaluators are running for this instance
        _tui_signal_dir = Path.home() / ".claude" / "tui-signals"
        _tui_signal_dir.mkdir(exist_ok=True)
        (_tui_signal_dir / f"evaluating-{session_id}").touch()
        asyncio.create_task(
            _require_dep("run_stop_evaluators", _run_stop_evaluators)(
                session_id, session_doc_id, stop_context, tab_name
            )
        )
        # Automatic rename is disabled. Instance names are DB-authoritative and
        # should change only through explicit rename actions; the future trigger
        # router can project those DB mutations back into tmux/Claude UI.

    result = {
        "success": True,
        "action": "stop_processed",
        "instance_id": session_id,
        "device_id": device_id,
    }
    child_fanout = await _enqueue_child_stop_fanout(instance, payload)
    if child_fanout:
        result["parent_fanout"] = child_fanout

    # ── Subagent detection: skip all notifications for subagents ──
    # DB flag covers subagent-CLI spawned instances; PID check covers Task tool subagents.
    pid = payload.get("pid")
    is_subagent_instance = bool(instance.get("is_subagent")) or bool(pid and is_subagent_pid(pid))
    if is_subagent_instance:
        result["action"] = "stop_processed_subagent"
        logger.info(
            f"Hook: Stop {session_id[:12]}... subagent — state updated, skipping notifications"
        )
        return result

    # Intermediate stop: background subagents still pending. Update state but skip notifications.
    if _pending_background_tasks.get(session_id, 0) > 0:
        result["action"] = "stop_processed_intermediate"
        logger.info(
            f"Hook: Stop {session_id[:12]}... intermediate ({_pending_background_tasks[session_id]} background tasks pending) — skipping notifications"
        )
        return result

    # Sync instances that passed through StopValidate don't need notifications
    # (the self-eval prompt already gave them a chance to continue).
    instance_type = instance.get("instance_type", "one_off")
    if instance_type == "sync" and not is_subagent_instance:
        result["action"] = "stop_processed_sync"
        await log_event("hook_stop", instance_id=session_id, details={"sync": True})
        return result

    # ── Golden Throne timer arm ──
    # StopValidate may block once for self-eval, but the async Stop hook owns
    # durable persistence after the model actually goes quiet.
    if instance_type == "golden_throne":
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            await sanctioned_update_instance(
                db,
                instance_id=session_id,
                updates={"status": "idle", "last_activity": now},
                mutation_type="status_changed",
                write_source="hooks",
                actor="Stop-golden-throne-idle",
            )
            await db.commit()
        schedule_result = await _require_dep(
            "schedule_golden_throne_callback", _schedule_golden_throne_callback
        )(dict(instance), reason="stop_hook")
        result["golden_throne"] = schedule_result

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
            with open(transcript_path) as f:
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
                        texts = [
                            c.get("text", "")
                            for c in content
                            if c.get("type") == "text" and c.get("text", "").strip()
                        ]
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
        asyncio.create_task(
            _post_discord_mirror(instance["discord_channel"], discord_bot, tts_text)
        )

    # Sanitize TTS text (remove markdown formatting and normalize whitespace)
    if tts_text:
        # Strip markdown headers (must be before newline conversion)
        tts_text = re.sub(r"^#{1,6}\s*", "", tts_text, flags=re.MULTILINE)
        # Strip markdown bold/italic
        tts_text = re.sub(r"\*\*([^*]+)\*\*", r"\1", tts_text)  # **bold**
        tts_text = re.sub(r"\*([^*]+)\*", r"\1", tts_text)  # *italic*
        tts_text = re.sub(r"__([^_]+)__", r"\1", tts_text)  # __bold__
        tts_text = re.sub(r"_([^_]+)_", r"\1", tts_text)  # _italic_
        # Strip inline code
        tts_text = re.sub(r"`([^`]+)`", r"\1", tts_text)
        # Strip code blocks
        tts_text = re.sub(r"```[\s\S]*?```", "", tts_text)
        # Strip bullet points and list markers
        tts_text = re.sub(r"^[\s]*[-*+]\s+", "", tts_text, flags=re.MULTILINE)
        tts_text = re.sub(r"^[\s]*\d+\.\s+", "", tts_text, flags=re.MULTILINE)
        # Convert newlines to spaces
        tts_text = tts_text.replace("\n", " ")
        # Normalize multiple spaces
        tts_text = re.sub(r" +", " ", tts_text)
        tts_text = tts_text.strip()

    # Mobile path: v3 /notify with TTS + banner + vibe
    if device_id == "Token-S24":
        notify_params = {
            "banner_text": f"[{notify_surface}] finished",
            "vibe": 30,
        }
        if tts_text:
            notify_params["tts_text"] = tts_text[:300]
        phone_result = await asyncio.to_thread(_send_to_phone, "/notify", notify_params)
        result["notification"] = phone_result
        logger.info(
            f"Hook: Stop {session_id[:12]}... -> mobile v3 notify ({len(tts_text or '')} chars)"
        )
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
        logger.info(
            f"Hook: Stop no TTS text (tts_enabled={tts_enabled}, has_text={bool(tts_text)})"
        )
        play_sound(notification_sound)
        result["sound"] = {"played": notification_sound}

    # Pavlok vibe notification (skip for subagents)
    if not instance.get("is_subagent"):
        vibe_result = send_pavlok_stimulus(
            stimulus_type="vibe",
            value=30,
            reason="claude_finished",
            respect_cooldown=False,
        )
        result["pavlok_vibe"] = vibe_result

    logger.info(f"Hook: Stop {session_id[:12]}... -> desktop notification")
    await log_event(
        "hook_stop",
        instance_id=session_id,
        details={"tts_enabled": tts_enabled, "tts_length": len(tts_text) if tts_text else 0},
    )

    return result


# ============ AskUserQuestion Persistence ============


def _imperium_env_root() -> Path:
    """Resolve the Obsidian vault root without relying on Token-OS cwd."""
    configured = os.environ.get("IMPERIUM_ENV")
    if configured:
        return Path(configured)
    imperium_root = os.environ.get("IMPERIUM", "/Volumes/Imperium")
    return Path(imperium_root) / "Imperium-ENV"


def _question_log_paths() -> tuple[Path, Path]:
    inbox = _imperium_env_root() / "Terra" / "Inbox"
    return inbox / "Questions.md", inbox / "Unanswered.md"


def _question_log_frontmatter(title: str) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    return (
        "---\n"
        f'title: "{title}"\n'
        "type: descriptive\n"
        f"created: {today}\n"
        "status: active\n"
        "tags: [terra/inbox, hooks/askuserquestion]\n"
        "---\n\n"
        f"# {title}\n\n"
    )


def _ensure_question_log(path: Path, title: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        path.write_text(_question_log_frontmatter(title), encoding="utf-8")


def _askq_instance_label(instance_id: str, instance_row: dict | None) -> str:
    row = instance_row or {}
    tab_name = row.get("tab_name") or row.get("name") or instance_id[:12]
    legion = row.get("legion")
    if legion:
        return f"{tab_name} / {legion}"
    return tab_name


def _askq_question_id(session_id: str) -> str:
    return f"{session_id}:{int(time.time() * 1000)}"


def _askq_question_lines(questions: list[dict] | None, fallback_text: str) -> list[str]:
    if not questions:
        return [fallback_text]
    lines: list[str] = []
    for question in questions:
        header = question.get("header")
        text = question.get("question") or question.get("text") or ""
        if header:
            lines.append(f"**{header}**")
        if text:
            lines.append(text)
    return lines or [fallback_text]


def _askq_option_lines(questions: list[dict] | None, options: list[str]) -> list[str]:
    if questions:
        lines: list[str] = []
        for question in questions:
            for option in question.get("options") or []:
                if isinstance(option, str):
                    lines.append(f"- {option}")
                elif isinstance(option, dict):
                    label = option.get("label") or option.get("value") or ""
                    description = option.get("description") or ""
                    if label and description:
                        lines.append(f"- **{label}** — {description}")
                    elif label:
                        lines.append(f"- {label}")
        if lines:
            return lines
    return [f"- {option}" for option in options] if options else ["- <none>"]


def _askq_format_section(state: dict, *, status: str, answer: str) -> str:
    started_at = state["started_at_wall"]
    label = state["instance_label"]
    instance_id = state["instance_id"]
    question_id = state["question_id"]
    tab_name = state.get("tab_name") or ""
    legion = state.get("legion") or ""
    header = f"## {started_at} — {label}"
    question_lines = "\n".join(_askq_question_lines(state.get("questions"), state["question_text"]))
    option_lines = "\n".join(_askq_option_lines(state.get("questions"), state.get("options") or []))

    return (
        f"{header}\n\n"
        f"- Question ID: `{question_id}`\n"
        f"- Instance ID: `{instance_id}`\n"
        f"- Tab: {tab_name or '<unknown>'}\n"
        f"- Legion: {legion or '<unknown>'}\n"
        f"- Status: {status}\n"
        f"- Answer: {answer}\n\n"
        "### Question\n"
        f"{question_lines}\n\n"
        "### Options\n"
        f"{option_lines}\n\n"
    )


def _askq_replace_section(content: str, question_id: str, replacement: str) -> str:
    marker = f"- Question ID: `{question_id}`"
    marker_index = content.find(marker)
    if marker_index == -1:
        return content.rstrip() + "\n\n" + replacement

    section_start = content.rfind("\n## ", 0, marker_index)
    if section_start == -1:
        section_start = content.find("## ")
    else:
        section_start += 1
    next_section = content.find("\n## ", marker_index)
    if next_section == -1:
        return content[:section_start] + replacement
    return content[:section_start] + replacement.rstrip() + "\n" + content[next_section:]


def _askq_append_unanswered(path: Path, state: dict, answer: str) -> None:
    _ensure_question_log(path, _UNANSWERED_TITLE)
    content = path.read_text(encoding="utf-8")
    question_id = state["question_id"]
    if f"- Question ID: `{question_id}`" in content:
        return
    started_at = state["started_at_wall"]
    label = state["instance_label"]
    question_lines = "\n".join(_askq_question_lines(state.get("questions"), state["question_text"]))
    entry = (
        f"## {started_at} — {label}\n\n"
        f"- [ ] Answer asynchronously\n"
        f"- Question ID: `{question_id}`\n"
        f"- Instance ID: `{state['instance_id']}`\n"
        f"- Status: {answer}\n\n"
        f"{question_lines}\n\n"
    )
    path.write_text(content.rstrip() + "\n\n" + entry, encoding="utf-8")


def _askq_persist_sync(state: dict, *, status: str, answer: str, unanswered: bool = False) -> None:
    questions_path, unanswered_path = _question_log_paths()
    _ensure_question_log(questions_path, _QUESTION_LOG_TITLE)
    content = questions_path.read_text(encoding="utf-8")
    section = _askq_format_section(state, status=status, answer=answer)
    questions_path.write_text(
        _askq_replace_section(content, state["question_id"], section),
        encoding="utf-8",
    )
    if unanswered:
        _askq_append_unanswered(unanswered_path, state, answer)


async def _askq_persist(state: dict, *, status: str, answer: str, unanswered: bool = False) -> None:
    try:
        async with _ASKQ_PERSIST_LOCK:
            await asyncio.to_thread(
                _askq_persist_sync, state, status=status, answer=answer, unanswered=unanswered
            )
    except Exception as e:
        logger.warning(f"AskQ persistence failed for {state.get('instance_id', '')[:12]}: {e}")


def _askq_extract_answer(payload: dict) -> str:
    """Best-effort extraction from Claude Code PostToolUse payload variants."""
    candidates = [
        payload.get("tool_response"),
        payload.get("tool_result"),
        payload.get("result"),
        payload.get("response"),
    ]

    def walk(value: Any) -> str | None:
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            for item in value:
                found = walk(item)
                if found:
                    return found
        if isinstance(value, dict):
            for key in ("answer", "answers", "value", "text", "content", "response"):
                if key in value:
                    found = walk(value[key])
                    if found:
                        return found
            for nested in value.values():
                found = walk(nested)
                if found:
                    return found
        return None

    for candidate in candidates:
        found = walk(candidate)
        if found:
            return found
    return "<answered>"


# ============ AskUserQuestion Three-Level Ladder ============
#
# Replaces the perma-block / silent auto-approve behavior with a graduated
# escalation ladder mirroring the expected_ack ladder. Active for voice-chat
# or golden_throne instances only (zealotry ≥ ASKQ_MIN_ZEALOTRY).
#
#   T1 elapses → Level 1 (TTS re-read + Discord nudge)
#   T2 elapses → Level 2 (enforcement cascade + persist Unanswered.md)
#   T3 elapses → Level 3 (pavlok shock + autonomous fallback prompt)
#
# Cancellation: PostToolUse(AskUserQuestion) means the question was answered.


async def _askq_ladder_run(instance_id: str, question_text: str) -> None:
    """Background coroutine that walks the three-level ladder for one question.

    Cancelled by PostToolUse(AskUserQuestion) when the user answers.
    """
    state = ASKQ_LADDER.get(instance_id)
    if not state:
        return

    try:
        # ── T1 elapses → Level 1 (TTS re-read + Discord nudge) ──
        await asyncio.sleep(shared.ASKQ_T1_SECONDS)
        state["current_touch"] = 1
        await log_event(
            "askq_level1_nudge",
            instance_id=instance_id,
            details={
                "question": question_text[:200],
                "elapsed_s": shared.ASKQ_T1_SECONDS,
                "question_id": state.get("question_id"),
            },
        )
        logger.info(f"AskQ ladder: Level 1 (TTS + Discord nudge) for {instance_id[:12]}")
        try:
            await queue_tts(instance_id, question_text, queue_target="hot")
        except Exception as e:
            logger.warning(f"AskQ ladder: Level 1 TTS failed: {e}")
        if _askq_level1_callback is not None:
            try:
                result = _askq_level1_callback(instance_id, question_text, state)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"AskQ ladder: Level 1 callback failed: {e}")

        # ── T2 elapses → Level 2 (enforcement cascade + persist Unanswered) ──
        await asyncio.sleep(shared.ASKQ_T2_SECONDS)
        state["current_touch"] = 2
        await log_event(
            "askq_level2_enforcement",
            instance_id=instance_id,
            details={
                "question": question_text[:200],
                "elapsed_s": shared.ASKQ_T1_SECONDS + shared.ASKQ_T2_SECONDS,
                "question_id": state.get("question_id"),
            },
        )
        logger.info(f"AskQ ladder: Level 2 (enforcement) for {instance_id[:12]}")
        await _askq_persist(state, status="unanswered", answer="<unanswered>", unanswered=True)
        if _askq_touch2_callback is not None:
            try:
                result = _askq_touch2_callback(instance_id, question_text)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"AskQ ladder: Level 2 callback failed: {e}")

        # ── T3 elapses → Level 3 (pavlok shock + autonomous fallback prompt) ──
        await asyncio.sleep(shared.ASKQ_T3_SECONDS)
        state["current_touch"] = 3
        await log_event(
            "askq_level3_pavlok",
            instance_id=instance_id,
            details={"question": question_text[:200], "question_id": state.get("question_id")},
        )
        await _askq_persist(state, status="bust", answer="<bust>", unanswered=True)
        if _askq_level3_callback is not None:
            try:
                result = _askq_level3_callback(instance_id, question_text, state)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.warning(f"AskQ ladder: Level 3 callback failed: {e}")
        logger.info(f"AskQ ladder: Level 3 BUST for {instance_id[:12]} — sending autonomous prompt")
        await _askq_send_bust_prompt(instance_id, state)

    except asyncio.CancelledError:
        logger.info(
            f"AskQ ladder: cancelled for {instance_id[:12]} (touch={state.get('current_touch')})"
        )
        raise
    finally:
        # Only clean up our own state — a newer ladder may have replaced us.
        if ASKQ_LADDER.get(instance_id) is state:
            ASKQ_LADDER.pop(instance_id, None)


async def _askq_send_bust_prompt(instance_id: str, state: dict) -> None:
    """Deliver the autonomous-fallback prompt to the asking instance via claude-cmd."""
    tmux_pane = state.get("tmux_pane")
    if not tmux_pane:
        # Re-fetch from DB in case state didn't capture it
        try:
            async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
                cursor = await db.execute(
                    "SELECT tmux_pane FROM claude_instances WHERE id = ?",
                    (instance_id,),
                )
                row = await cursor.fetchone()
                if row:
                    tmux_pane = row[0]
        except Exception:
            pass

    if not tmux_pane:
        logger.warning(f"AskQ ladder: bust prompt skipped for {instance_id[:12]} — no tmux_pane")
        return

    try:
        proc = await _run_subprocess_offloop(
            ("claude-cmd", "--pane", tmux_pane, ASKQ_BUST_PROMPT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=10,
        )
        if proc.returncode != 0:
            logger.warning(
                f"AskQ ladder: claude-cmd bust failed for {instance_id[:12]}: "
                f"{proc.stderr.decode()[:200]}"
            )
    except Exception as e:
        logger.warning(f"AskQ ladder: bust delivery failed for {instance_id[:12]}: {e}")


def _askq_should_engage_ladder(instance_row: dict | None, session_id: str) -> bool:
    """Ladder fires only for voice-chat or golden_throne instances. Plain CLI sessions
    keep the native dialog."""
    if session_id in VOICE_CHAT_SESSIONS:
        return True
    if not instance_row:
        return False
    return instance_row.get("instance_type") == "golden_throne"


async def _askq_ladder_start(
    session_id: str,
    question_text: str,
    options: list[str],
    instance_row: dict | None,
    questions: list[dict] | None = None,
) -> None:
    """Arm the three-touch ladder for an AskUserQuestion. Cancels any prior ladder
    for the same instance (newer question supersedes)."""
    prior = ASKQ_LADDER.pop(session_id, None)
    if prior and prior.get("task") and not prior["task"].done():
        prior["task"].cancel()

    state: dict[str, Any] = {
        "question_id": _askq_question_id(session_id),
        "instance_id": session_id,
        "question_text": question_text,
        "questions": questions or [],
        "options": options,
        "started_at_wall": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "started_at": time.monotonic(),
        "current_touch": 1,
        "task": None,
        "instance_label": _askq_instance_label(session_id, instance_row),
        "tab_name": (instance_row or {}).get("tab_name"),
        "legion": (instance_row or {}).get("legion"),
        "tmux_pane": (instance_row or {}).get("tmux_pane"),
        "device_id": (instance_row or {}).get("device_id"),
        "tts_voice": (instance_row or {}).get("tts_voice"),
    }
    ASKQ_LADDER[session_id] = state
    state["task"] = asyncio.create_task(_askq_ladder_run(session_id, question_text))

    await log_event(
        "askq_touch1_initial",
        instance_id=session_id,
        details={
            "question": question_text[:200],
            "options": options[:5],
            "t1_s": shared.ASKQ_T1_SECONDS,
            "t2_s": shared.ASKQ_T2_SECONDS,
            "t3_s": shared.ASKQ_T3_SECONDS,
            "question_id": state["question_id"],
        },
    )
    await _askq_persist(state, status="pending", answer="<pending>")
    logger.info(f"AskQ ladder: Touch 1 armed for {session_id[:12]} — T1={shared.ASKQ_T1_SECONDS}s")


async def _askq_ladder_cancel(
    session_id: str, reason: str = "answered", answer: str = "<answered>"
) -> None:
    """Cancel any active ladder for this instance (called on PostToolUse(AskUserQuestion))."""
    state = ASKQ_LADDER.pop(session_id, None)
    if not state:
        return
    task = state.get("task")
    if task and not task.done():
        task.cancel()
    elapsed = time.monotonic() - state.get("started_at", time.monotonic())
    await log_event(
        "askq_ladder_cancelled",
        instance_id=session_id,
        details={
            "reason": reason,
            "touch_at_cancel": state.get("current_touch"),
            "elapsed_s": round(elapsed, 1),
            "answer": answer,
            "question_id": state.get("question_id"),
        },
    )
    answer_value = answer if reason == "answered" else f"<{reason}>"
    await _askq_persist(state, status=reason, answer=answer_value)
    logger.info(
        f"AskQ ladder: cancelled for {session_id[:12]} "
        f"(touch={state.get('current_touch')}, elapsed={elapsed:.1f}s, reason={reason})"
    )


async def handle_pre_tool_use(payload: dict) -> dict:
    """Handle PreToolUse hook - marks processing, can block operations like 'make deploy'."""
    session_id = payload.get("session_id")
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})

    # Mark instance as processing (catches cases where prompt_submit was missed)
    # Also resurrect stopped instances - activity means they're active
    if session_id:
        now = datetime.now().isoformat()
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            await sanctioned_update_instance(
                db,
                instance_id=session_id,
                updates={"status": "processing", "last_activity": now, "stopped_at": None},
                mutation_type="status_changed",
                write_source="hooks",
                actor="PreToolUse",
            )
            await db.commit()

    # Track background Task subagents so Stop hooks can detect intermediate vs final stops.
    if tool_name == "Task" and tool_input.get("run_in_background"):
        _pending_background_tasks[session_id] = _pending_background_tasks.get(session_id, 0) + 1
        logger.info(
            f"PreToolUse: Task background launched for {session_id[:12]} (pending: {_pending_background_tasks[session_id]})"
        )
        return {"success": True, "action": "allowed"}

    # AskUserQuestion three-touch ladder + voice-chat AHK side effect.
    # Fetch instance row once for ladder eligibility (voice-chat OR golden_throne).
    askq_instance_row: dict | None = None
    if tool_name == "AskUserQuestion" and session_id:
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT instance_type, tab_name, legion, tmux_pane, device_id, tts_voice "
                "FROM claude_instances WHERE id = ?",
                (session_id,),
            )
            row = await cursor.fetchone()
            if row:
                askq_instance_row = dict(row)

        if _askq_should_engage_ladder(askq_instance_row, session_id):
            # Touch 1: TTS the question text + arm the ladder.
            questions = tool_input.get("questions", [])
            tts_parts = [q.get("question", "") for q in questions if q.get("question")]
            tts_message = " ".join(tts_parts).strip()
            options = []
            if questions:
                options = [o for o in (questions[0].get("options") or []) if isinstance(o, str)]
            if tts_message:
                try:
                    await queue_tts(session_id, tts_message, queue_target="hot")
                    logger.info(
                        f"PreToolUse: AskQ Touch 1 TTS queued (hot) for {session_id[:12]}: "
                        f"{tts_message[:80]}"
                    )
                except Exception as e:
                    logger.warning(
                        f"PreToolUse: AskQ Touch 1 TTS failed for {session_id[:12]}: {e}"
                    )
                await _askq_ladder_start(
                    session_id,
                    tts_message,
                    options,
                    askq_instance_row,
                    questions=questions,
                )

    # Voice chat: trigger AHK so dictation captures the answer (voice-chat only).
    if tool_name == "AskUserQuestion" and session_id and session_id in VOICE_CHAT_SESSIONS:
        vc_session = VOICE_CHAT_SESSIONS.get(session_id, {})
        tmux_pane = vc_session.get("tmux_pane", "")
        pane_arg = f' "{tmux_pane}"' if tmux_pane else ""
        logger.info(
            f"PreToolUse: Voice chat local_exec for {session_id[:12]} (pane: {tmux_pane or 'default'})"
        )
        return {
            "success": True,
            "action": "allowed",
            "local_exec": f'"/mnt/c/Program Files/AutoHotkey/v2/AutoHotkey.exe" "//Token-NAS/Imperium/Token-OS/ahk/voice-send-keys.ahk"{pane_arg} --navigate',
        }

    # Discord-hosted: post AskUserQuestion to Discord channel and notify phone
    _ask_handled_by_discord = False
    if tool_name == "AskUserQuestion" and session_id:
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            cursor = await db.execute(
                "SELECT discord_hosted, discord_channel, legion FROM claude_instances WHERE id = ?",
                (session_id,),
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
                    asyncio.create_task(
                        _post_discord_mirror(
                            discord_channel, discord_bot, f"**Question:** {q_text}"
                        )
                    )
                    # Also phone notify so Emperor knows to check Discord
                    asyncio.create_task(
                        asyncio.to_thread(
                            _send_to_phone,
                            "/notify",
                            {
                                "vibe": 40,
                                "tts_text": "Claude is asking a question in Discord.",
                                "banner_text": q_parts[0][:80],
                            },
                        )
                    )
                    logger.info(
                        f"PreToolUse: AskUserQuestion posted to Discord #{discord_channel} for {session_id[:12]}"
                    )

    # Phone notification for AskUserQuestion (non-voice-chat, non-discord-hosted instances)
    if (
        tool_name == "AskUserQuestion"
        and session_id
        and session_id not in VOICE_CHAT_SESSIONS
        and not _ask_handled_by_discord
    ):
        questions = tool_input.get("questions", [])
        if questions:
            q_text = questions[0].get("question", "")[:200]
            if q_text:
                asyncio.create_task(
                    asyncio.to_thread(
                        _send_to_phone,
                        "/notify",
                        {
                            "vibe": 40,
                            "beep": 30,
                            "tts_text": f"Claude is asking: {q_text}",
                            "banner_text": q_text[:80],
                        },
                    )
                )
                logger.info(
                    f"PreToolUse: AskUserQuestion phone notify for {session_id[:12]}: {q_text[:60]}"
                )

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
            ),
        }

    return {"success": True, "action": "allowed"}


async def handle_notification(payload: dict) -> dict:
    """Handle Notification hook - play notification sound."""
    session_id = payload.get("session_id")

    # Get instance profile for sound selection
    sound_file = "chimes.wav"  # default

    if session_id:
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT notification_sound FROM claude_instances WHERE id = ?", (session_id,)
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
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            await sanctioned_update_instance(
                db,
                instance_id=session_id,
                updates={
                    "stop_allowed": 1,
                    "workflow_blocked_reason": None,
                    "next_required_action": None,
                    "next_action_owner": None,
                },
                mutation_type="instance_updated",
                write_source="hooks",
                actor="StopValidate",
            )
            await db.commit()
        logger.info(
            f"StopValidate: {session_id[:12]} self-eval complete ({elapsed:.1f}s) — allowing stop"
        )
        await log_event(
            "stop_validate_pass",
            instance_id=session_id,
            details={"reason": "self_eval_complete", "elapsed": elapsed},
        )
        return {}  # no decision — allow stop

    # ── Expire stale entries ──
    stale = [sid for sid, ts in _self_eval_pending.items() if now - ts > SELF_EVAL_TTL_SECONDS]
    for sid in stale:
        del _self_eval_pending[sid]

    # ── Look up instance ──
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, instance_type, is_subagent, victory_at, workflow_state FROM claude_instances WHERE id = ?",
            (session_id,),
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
        await log_event(
            "stop_validate_pass",
            instance_id=session_id,
            details={"reason": "schedule_wakeup_active"},
        )
        return {}

    # ── Block: golden_throne and sync instances get self-eval prompt ──
    if instance_type in ("golden_throne", "sync"):
        _self_eval_pending[session_id] = now
        blocked_at = datetime.now().isoformat()
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            await sanctioned_update_instance(
                db,
                instance_id=session_id,
                updates={
                    "workflow_state": "blocked",
                    "workflow_updated_at": blocked_at,
                    "workflow_blocked_reason": "self_eval_required",
                    "stop_allowed": 0,
                    "next_required_action": "self_eval",
                    "next_action_owner": "agent",
                },
                mutation_type="status_changed",
                write_source="hooks",
                actor="StopValidate",
            )
            await append_workflow_event(
                db,
                instance_id=session_id,
                workflow_state="blocked",
                event_type="stop_blocked",
                event_owner="hooks",
                details={"instance_type": instance_type, "reason": "self_eval_required"},
            )
            if instance.get("workflow_state") != "blocked":
                await append_workflow_event(
                    db,
                    instance_id=session_id,
                    workflow_state="blocked",
                    event_type="workflow_state_changed",
                    event_owner="hooks",
                    details={
                        "old_workflow_state": instance.get("workflow_state"),
                        "new_workflow_state": "blocked",
                    },
                )
            await db.commit()
        logger.info(
            f"StopValidate: blocking {session_id[:12]} ({instance_type}) with self-eval prompt"
        )
        await log_event(
            "stop_validate_block", instance_id=session_id, details={"instance_type": instance_type}
        )
        return {
            "decision": "block",
            "reason": _SELF_EVAL_PROMPT,
        }

    return {}  # default: allow stop


# Hook dispatcher endpoint
@router.post("/api/hooks/{action_type}")
async def dispatch_hook(action_type: str, payload: dict, request: Request) -> dict:
    """
    Unified hook dispatcher for Claude Code and Codex hooks.

    Receives hook events from shell bridges and routes to appropriate handler.
    Always returns a response - errors are logged but don't cause failures.
    """
    action_aliases = {
        "PromptSubmit": "UserPromptSubmit",
        "InferenceStop": "Stop",
        "InferenceStopValidate": "StopValidate",
    }

    normalized_action_type = action_aliases.get(action_type, action_type)

    handlers = {
        "WrapperStart": handle_wrapper_start,
        "WrapperEnd": handle_wrapper_end,
        "SessionStart": handle_session_start,
        "SessionEnd": handle_session_end,
        "UserPromptSubmit": handle_prompt_submit,
        "PostToolUse": handle_post_tool_use,
        "Stop": handle_stop,
        "StopValidate": handle_stop_validate,
        "PreToolUse": handle_pre_tool_use,
        "Notification": handle_notification,
    }

    handler = handlers.get(normalized_action_type)
    if not handler:
        logger.warning(f"Hook: Unknown action type: {action_type}")
        return {"success": False, "action": "unknown_hook_type", "type": action_type}

    # Inject HTTP client IP into payload for device detection fallback
    if request.client:
        payload["_client_ip"] = request.client.host
    payload["_hook_action_type"] = normalized_action_type
    payload["_hook_action_type_raw"] = action_type

    try:
        result = await handler(payload)
        return result
    except Exception as e:
        logger.error(f"Hook handler error ({normalized_action_type}): {e}")
        await log_event(
            "hook_error",
            details={
                "action_type": normalized_action_type,
                "raw_action_type": action_type,
                "error": str(e),
            },
        )
        return {"success": False, "action": "handler_error", "error": str(e)}
