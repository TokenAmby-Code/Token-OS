"""
Claude Code Hook Handlers — extracted from main.py.

Owns:
- Hook lifecycle handlers (SessionStart, Stop, PromptSubmit, etc.)
- Hook dispatch endpoint (/api/hooks/{action_type})
- Discord output mirroring

Uses dependency injection from main.py for runtime-owned callbacks.
"""

import asyncio
import hashlib
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
from zoneinfo import ZoneInfo

import aiosqlite
from fastapi import APIRouter, Request
from pydantic import BaseModel

import shared
import talk as talk_service
from enforcement_service import close_distraction_windows
from instance_mutation import (
    _fetch_instance_row,
    create_golden_throne_binding,
    sanctioned_delete_instance,
    sanctioned_insert_instance,
    sanctioned_update_instance,
    sanctioned_update_instance_record,
    sanctioned_update_runtime_fields,
)
from instance_registry import LEGACY_PERSONA_ALIASES
from pane_surface import PLACEHOLDER_TAB_NAME_RX, human_pane_surface
from personas import assign_astartes_persona, persona_to_profile
from phone_service import _send_to_phone
from questions_gate import trials_clear
from routes.tts import dispatch_notify, play_sound, queue_tts
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
    MORNING_EXPIRY_NOTICE,
    MORNING_KEEPALIVE_PROMPT,
    VOICE_CHAT_SESSIONS,
    append_workflow_event,
    is_subagent_pid,
    log_event,
    profile_by_name,
    resolve_device_from_ip,
    resolve_persona_profile,
)

logger = logging.getLogger("token_api")

router = APIRouter()

_QUESTION_LOG_TITLE = "AskUserQuestion Log"
_UNANSWERED_TITLE = "Unanswered Questions"
_ASKQ_PERSIST_LOCK = asyncio.Lock()
VALID_LAUNCH_INSTANCE_TYPES = {"golden_throne", "sync", "one_off", "hook_driven"}

# SessionEnd `reason` values that are NON-terminal: the wrapper is still alive
# and a paired SessionStart re-fire follows in the SAME wrapper (plan-accept /
# `/clear` / compaction). Tearing the row down on these (status=stopped +
# _spawn_session_end_assertion, which unsets @INSTANCE_ID) destroys the
# continuity stamp the re-fire needs to re-key — forcing a mint + orphan doc.
# Everything else (logout, prompt_input_exit, bypass_permissions_disabled,
# other, unknown/missing) keeps the full terminal teardown. Gate STRICTLY: an
# unknown reason must fail closed to teardown, never silently preserve a row.
NON_TERMINAL_SESSION_END_REASONS = {"clear", "compact"}


async def _launch_golden_throne_marker(
    db,
    launch_instance_type: str | None,
    *,
    zealotry: int | None = None,
    existing_marker: str | None = None,
) -> str | None:
    """Map the legacy launch instance_type vocabulary onto the instances.golden_throne
    marker (its durable home): 'sync' → 'sync'; 'golden_throne' → a real golden_throne.id
    (reusing an existing GT binding when present); 'one_off'/'hook_driven' → NULL.
    """
    if launch_instance_type == "sync":
        return "sync"
    if launch_instance_type == "golden_throne":
        if existing_marker and existing_marker != "sync":
            return existing_marker
        return await create_golden_throne_binding(db, zealotry=zealotry)
    return None


def _tmuxctl_bin() -> Path:
    return Path(__file__).resolve().parents[2] / "cli-tools" / "bin" / "tmuxctl"


def _spawn_session_end_assertion(tmux_pane: str, session_id: str) -> None:
    """Assert/prune a just-closed pane without blocking hook completion."""
    if not tmux_pane:
        return
    tmuxctl = _tmuxctl_bin()
    if not tmuxctl.exists():
        logger.warning("Hook: SessionEnd assert skipped for %s — tmuxctl not found", tmux_pane)
        return
    code = r"""
import os
import subprocess
import sys
import time

tmuxctl, pane, session_id = sys.argv[1:4]
env = os.environ.copy()
env.setdefault("IMPERIUM_TMUX_AUTOMATION", "1")
try:
    # Let the DB-triggered pane_state_queue publish its stopped state first;
    # assert-instance then owns the final close-down cleanup and clears stale
    # stopped/idle header chrome rather than racing the queue worker.
    time.sleep(2)
    proc = subprocess.run(
        [tmuxctl, "assert-instance", "--pane", pane],
        text=True,
        capture_output=True,
        timeout=75,
        check=False,
        env=env,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    raise SystemExit(proc.returncode)
except subprocess.TimeoutExpired as exc:
    sys.stderr.write(f"SessionEnd assert-instance timeout pane={pane} session={session_id}: {exc}\n")
    raise SystemExit(124)
"""
    log_path = Path("/tmp/session-end-assert-instance.log")
    log_handle = None
    try:
        log_handle = log_path.open("a")
        subprocess.Popen(
            ["python3", "-c", code, str(tmuxctl), tmux_pane, session_id],
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
            close_fds=True,
        )
        logger.info(
            "Hook: SessionEnd spawned assert-instance for %s (%s)",
            tmux_pane,
            session_id[:12],
        )
    except Exception as exc:
        logger.warning(
            "Hook: SessionEnd failed to spawn assert-instance for %s: %s",
            tmux_pane,
            exc,
        )
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except Exception:
                pass


# ============ Injected Dependencies ============
# main.py owns these runtime services and injects them after import.

_scheduler: Any = None
_timer_engine: Any = None
_timer_log_shift: Callable[..., Any] | None = None
_run_stop_evaluators: Callable[..., Any] | None = None
_auto_name_instance: Callable[..., Any] | None = None
# Server-side naming interview (main._maybe_naming_nudge). Fired on Stop so
# Codex panes — which have no naming-nudge.sh Stop shim — get interviewed too.
_maybe_naming_nudge: Callable[..., Any] | None = None
_work_action_callback: Callable[..., Any] | None = None
_schedule_golden_throne_callback: Callable[..., Any] | None = None
_golden_throne_activity_callback: Callable[..., Any] | None = None
# AskUserQuestion ladder callbacks. Optional injection points (default None);
# the ladder skips any stage whose callback is unset. Tests wire fakes directly.
_askq_level1_callback: Callable[..., Any] | None = None
_askq_touch2_callback: Callable[..., Any] | None = None
_askq_level3_callback: Callable[..., Any] | None = None
# main.py's gate-aware, verification-tracked pane-write primitive. Hooks route
# live prompt delivery through this instead of a bespoke send so the universal
# send gate is honored and delivery truth (gated/unverified/submitted) is not
# faked. Tests wire a fake directly onto this module global.
_tmux_send_payload_then_submit: Callable[..., Any] | None = None


def init_deps(
    *,
    scheduler: Any | None = None,
    timer_engine: Any | None = None,
    timer_log_shift: Callable[..., Any] | None = None,
    run_stop_evaluators: Callable[..., Any] | None = None,
    auto_name_instance: Callable[..., Any] | None = None,
    maybe_naming_nudge: Callable[..., Any] | None = None,
    work_action_callback: Callable[..., Any] | None = None,
    schedule_golden_throne_callback: Callable[..., Any] | None = None,
    golden_throne_activity_callback: Callable[..., Any] | None = None,
    askq_level1_callback: Callable[..., Any] | None = None,
    tmux_send_payload_then_submit: Callable[..., Any] | None = None,
) -> None:
    """Wire runtime-owned dependencies from main.py."""
    global _scheduler, _timer_engine, _timer_log_shift
    global _run_stop_evaluators, _auto_name_instance, _maybe_naming_nudge
    global _work_action_callback
    global _schedule_golden_throne_callback, _golden_throne_activity_callback
    global _askq_level1_callback, _tmux_send_payload_then_submit

    _scheduler = scheduler
    _timer_engine = timer_engine
    _timer_log_shift = timer_log_shift
    _run_stop_evaluators = run_stop_evaluators
    _auto_name_instance = auto_name_instance
    _maybe_naming_nudge = maybe_naming_nudge
    _work_action_callback = work_action_callback
    _schedule_golden_throne_callback = schedule_golden_throne_callback
    _golden_throne_activity_callback = golden_throne_activity_callback
    _askq_level1_callback = askq_level1_callback
    _tmux_send_payload_then_submit = tmux_send_payload_then_submit


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


class HookSubscribeRequest(BaseModel):
    target_instance_id: str | None = None
    target_pane: str | None = None
    subscriber_instance_id: str | None = None
    subscriber_pane: str | None = None
    event: str = "stop"
    delivery: str = "prompt"
    purpose: str = "generic"
    payload: str | None = None
    oneshot: bool = False


class HookUnsubscribeRequest(BaseModel):
    target_instance_id: str | None = None
    target_pane: str | None = None
    subscriber_instance_id: str | None = None
    subscriber_pane: str | None = None
    event: str = "stop"
    purpose: str | None = None


class PlanningStateRequest(BaseModel):
    instance_id: str | None = None
    tmux_pane: str | None = None
    state: str | None = None
    cycle: bool = False
    source: str = "api"


class HookSubscriptionsQuery(BaseModel):
    target_instance_id: str | None = None
    target_pane: str | None = None
    subscriber_instance_id: str | None = None
    subscriber_pane: str | None = None
    event: str = "stop"
    status: str = "active"


class HookReconcileRequest(BaseModel):
    page: str = "mechanicus"


class HookPruneRequest(BaseModel):
    confirm: bool = False
    event: str = "stop"


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

MECHANICUS_FG_LABEL = "mechanicus:fabricator-general"
MECHANICUS_ADMIN_LABEL = "mechanicus:admin"
CUSTODES_PANE_LABEL = "legion:custodes"
LEGION_MALCADOR_LABEL = "legion:malcador"
KORONUS_PAX_LABEL = "koronus:pax"
KORONUS_ORCHESTRATOR_LABEL = "koronus:orchestrator"

# Persona/orchestrator singleton panes → canonical DB identity. tmuxctl stamps a
# stable @PANE_ID on each of these panes; a fresh SessionStart inside one IS that
# persona, so we derive its row identity (legion + primarch + instance_type) from
# the pane. This makes "SessionStart from
# a persona pane registers correctly" an infrastructure invariant — no persona
# ever has to self-PATCH legion/type/synced. The fields chosen match each
# persona's own resolution key: custodes resolves on the legion:custodes pane marker,
# FG on its pane label, Administratum on primarch='administratum'.
#
# Worker panes (mechanicus:N, mechanicus:worker-N) are intentionally absent — they
# are not personas and resolve their legion from dispatch env / working-dir
# auto-detect, not from a fixed identity.
PERSONA_PANE_IDENTITY: dict[str, dict] = {
    CUSTODES_PANE_LABEL: {
        "legion": "custodes",
        "primarch": "custodes",
        # Custodes identity is persona + rank (resolve_live_persona_instance), not
        # sync. The resting registration default matches FG/Admin (hook_driven);
        # the morning session sets sync MODE (instance_type='sync', synced=1) only
        # while a session is live, and clears it on /api/morning/end. Do NOT touch
        # the legion:custodes pane marker — that is the pane source of truth.
        "instance_type": "hook_driven",
        "synced": False,
    },
    MECHANICUS_FG_LABEL: {
        # FG owns a dedicated singleton legion ("fabricator", see ALLOWED_LEGIONS /
        # SINGLETON_LEGIONS and assertions._row_matches_persona). The "mechanicus:"
        # prefix is the tmux page/region, NOT the legion.
        "legion": "fabricator",
        "primarch": "fabricator-general",
        "instance_type": "hook_driven",
        "synced": False,
    },
    MECHANICUS_ADMIN_LABEL: {
        # Administratum has no dedicated legion; it registers under the shared
        # mechanicus legion. Its load-bearing resolution key is primarch (token-api
        # _resolve_administratum_instance keys on primarch='administratum').
        "legion": "mechanicus",
        "primarch": "administratum",
        "instance_type": "hook_driven",
        "synced": False,
    },
    LEGION_MALCADOR_LABEL: {
        # Malcador (advisor seat) shares the astartes legion with regiment workers,
        # so legion cannot identify it — its load-bearing key is primarch='malcador'
        # (personas seed default_rank='primarch'; tmuxctl
        # assertions._row_matches_persona resolves on the same column), mirroring
        # Administratum. Outside enforcement and state-hook routing: never sync.
        "legion": "astartes",
        "primarch": "malcador",
        "instance_type": "hook_driven",
        "synced": False,
    },
    KORONUS_PAX_LABEL: {
        # Pax (civic overseer seat on the koronus page — the combined
        # Custodes+Administratum interaction/record-keeper seat) registers under
        # the shared `civic` legion (an ALLOWED_LEGION), so legion cannot identify
        # it — its load-bearing key is primarch='pax'. That resolves to the `pax`
        # personas row (default_rank='overseer'), and the rank-stamp trigger
        # promotes the freshly inserted row off the 'astartes' column default. A
        # fresh SessionStart in this pane IS Pax: Emperor-commanded, never a
        # chapter child. The civic identity is keyed strictly on this koronus pane
        # label, so a pax pane promoted to palace/somnium gets no entry here and
        # falls through to the normal astartes registration. Never sync.
        "legion": "civic",
        "primarch": "pax",
        "instance_type": "hook_driven",
        "synced": False,
    },
    KORONUS_ORCHESTRATOR_LABEL: {
        # Orchestrator (civic dispatch seat on the koronus page — the role the
        # Fabricator-General plays for mechanicus). Shares the `civic` legion with
        # pax, so legion cannot identify it — its load-bearing key is
        # primarch='orchestrator', which resolves to the `orchestrator` personas
        # row (default_rank='overseer'). Like pax, the civic identity applies only
        # while ON the koronus page (resolved from this pane label); started
        # elsewhere it falls through to the astartes default. Never sync.
        "legion": "civic",
        "primarch": "orchestrator",
        "instance_type": "hook_driven",
        "synced": False,
    },
}


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


async def _stamp_instance_id(
    tmux_pane: str | None,
    session_id: str | None,
    display_name: str | None = None,
) -> None:
    """Stamp ``@INSTANCE_ID=<session_id>`` (and optionally ``@PANE_LABEL``) on the pane.

    tmux becomes the source of truth for ``instance_id -> pane`` resolution; the
    stamp lives and dies with the pane. Done in the same critical section as the
    row upsert so no reader ever sees a registered row whose pane is unstamped.
    Best-effort: a failed stamp is logged, not raised — the row write must not be
    blocked by tmux being unavailable (e.g. remote/satellite-hosted panes whose
    ``%N`` is not addressable from this host).

    ``display_name`` is the instance's ``tab_name`` (NOT the persona ``pane_label``
    column). When provided, it hydrates ``@PANE_LABEL`` so the border shows the name
    *before* the first rename — a fresh register INSERTs, so ``trg_tab_name_pane_state``
    (AFTER UPDATE) never fires for it. Renames thereafter flow through the trigger.
    """
    if not tmux_pane or not session_id:
        return
    try:
        proc = await _run_subprocess_offloop(
            ("tmux", "set-option", "-p", "-t", tmux_pane, "@INSTANCE_ID", session_id),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=2,
        )
        if proc.returncode != 0:
            logger.debug(
                f"Hook: @INSTANCE_ID stamp failed for {tmux_pane} "
                f"({session_id}): {proc.stderr.decode(errors='ignore').strip()}"
            )
    except Exception as exc:
        logger.debug(f"Hook: @INSTANCE_ID stamp errored for {tmux_pane}: {exc}")

    label = (display_name or "").strip()
    if not label:
        return
    try:
        await _run_subprocess_offloop(
            ("tmux", "set-option", "-p", "-t", tmux_pane, "@PANE_LABEL", label),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            timeout=2,
        )
    except Exception as exc:
        logger.debug(f"Hook: @PANE_LABEL stamp errored for {tmux_pane}: {exc}")


async def _persona_id_by_slug(db, slug: str) -> str | None:
    cursor = await db.execute("SELECT id FROM personas WHERE slug = ?", (slug,))
    row = await cursor.fetchone()
    return str(row[0]) if row else None


async def _apply_commander_binding(
    db,
    *,
    instance_id: str,
    dispatch_target: str | None,
    parent_instance_id: str | None,
    dispatch_mode: str | None = None,
) -> None:
    """Set durable commander semantics from SessionStart context.

    Runtime dispatch target/window/slot are not stored in ``instances``; this is
    the one-time translation from launch context into durable commander routing.
    """
    parent_instance_id = _normalize_text(parent_instance_id)
    if parent_instance_id:
        cursor = await db.execute(
            """SELECT id, persona_id FROM instances
               WHERE id = ? AND status != 'archived' AND rank != 'retired'""",
            (parent_instance_id,),
        )
        parent = await cursor.fetchone()
        if parent:
            await sanctioned_update_instance_record(
                db,
                instance_id=instance_id,
                updates={
                    "persona_id": parent[1],
                    "commander_type": "chapter",
                    "commander_id": parent[0],
                },
                mutation_type="commander_binding_changed",
                write_source="hooks",
                actor="SessionStart",
            )
        return
    mode = (_normalize_text(dispatch_mode) or "").lower()
    if mode in {"silent", "breakoff", "break-off", "break_off"}:
        await sanctioned_update_instance_record(
            db,
            instance_id=instance_id,
            updates={"commander_type": "emperor", "commander_id": None},
            mutation_type="commander_binding_changed",
            write_source="hooks",
            actor="SessionStart",
        )
        return
    target = (_normalize_text(dispatch_target) or "").lower()
    commander_slug = None
    if target == "legion:new":
        commander_slug = "custodes"
    elif target == "mechanicus:new":
        commander_slug = "fabricator-general"
    if not commander_slug:
        return
    commander_persona_id = await _persona_id_by_slug(db, commander_slug)
    if commander_persona_id is None:
        return
    await sanctioned_update_instance_record(
        db,
        instance_id=instance_id,
        updates={
            "commander_type": "persona",
            "commander_id": str(commander_persona_id),
        },
        mutation_type="commander_binding_changed",
        write_source="hooks",
        actor="SessionStart",
    )


async def _persist_runtime_fields(
    db,
    *,
    instance_id: str,
    tmux_pane: str | None = None,
    pane_label: str | None = None,
    dispatch_target: str | None = None,
    dispatch_window: str | None = None,
    dispatch_slot: str | None = None,
) -> None:
    """Persist tmux runtime geometry onto the instances runtime annex.

    Post-exterminatus these land on `instances` (annex columns) through the
    dedicated runtime gate, so every pane rebind keeps provenance. The annex
    columns die when @INSTANCE_ID-stamp resolution fully replaces stored panes.
    """
    updates = {}
    if tmux_pane is not None:
        updates["tmux_pane"] = tmux_pane
    if pane_label is not None:
        updates["pane_label"] = pane_label
    if dispatch_target is not None:
        updates["dispatch_target"] = dispatch_target
    if dispatch_window is not None:
        updates["dispatch_window"] = dispatch_window
    if dispatch_slot is not None:
        updates["dispatch_slot"] = dispatch_slot
    if not updates:
        return
    await sanctioned_update_runtime_fields(
        db,
        instance_id=instance_id,
        updates=updates,
        mutation_type="runtime_binding_changed",
        write_source="hooks",
        actor="SessionStart-runtime-binding",
    )


async def _unstamp_instance_id(tmux_pane: str | None, session_id: str | None) -> None:
    """Clear ``@INSTANCE_ID`` on a pane an instance is moving *off* of.

    When an instance moves panes (transplant / re-register onto a new ``%N``) the
    new pane is stamped, but the old pane would otherwise keep this instance's
    stamp — leaving two live panes resolving to the same UUID until teardown. We
    clear it here so ``resolve-instance`` never sees a duplicate.

    Guarded: only unset when the pane *still* carries this instance's id, so a
    pane already reused by a different agent (which re-stamped with its own id) is
    never clobbered. Best-effort; never raises.
    """
    if not tmux_pane or not session_id:
        return
    try:
        current = await _run_subprocess_offloop(
            ("tmux", "show-options", "-pqv", "-t", tmux_pane, "@INSTANCE_ID"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=2,
        )
        if current.returncode != 0:
            return
        if current.stdout.decode(errors="ignore").strip() != session_id:
            return  # pane gone or already owned by a different instance
        await _run_subprocess_offloop(
            ("tmux", "set-option", "-p", "-u", "-t", tmux_pane, "@INSTANCE_ID"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            timeout=2,
        )
    except Exception as exc:
        logger.debug(f"Hook: @INSTANCE_ID unstamp errored for {tmux_pane}: {exc}")


async def _stop_if_dead_pane(db, session_id: str, existing: dict, actor: str) -> bool:
    tmux_pane = existing.get("tmux_pane")
    if not tmux_pane:
        return False
    if existing.get("status") == "stopped":
        return True
    if await _tmux_pane_exists(tmux_pane):
        return False
    dead_pane_updates = {
        "status": "stopped",
        "input_lock": None,
        "stopped_at": datetime.now().isoformat(),
    }
    # Legacy `synced=0` cleared the morning-session sync flag. Its durable home is the
    # golden_throne marker: clear it ONLY when it is the 'sync' sentinel — a real
    # golden_throne.id binding must survive (the dead-pane GT follow-up below
    # depends on it).
    if existing.get("golden_throne") == "sync":
        dead_pane_updates["golden_throne"] = None
    await sanctioned_update_instance(
        db,
        instance_id=session_id,
        updates=dead_pane_updates,
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
    _dead_marker = existing.get("golden_throne")
    if _dead_marker and _dead_marker != "sync" and _schedule_golden_throne_callback:
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


def _instance_name_base_from_session_doc(title: str | None, file_path: str | None) -> str:
    """Return the shared instance-name prefix for a session doc.

    Instances are named after the document they are attached to, with a
    monotonic per-document suffix: `<session-doc-name>-1`, `...-2`, etc.
    Dates remain metadata, so strip legacy date prefixes/suffixes if they are
    present on old docs.
    """
    raw = (title or "").strip()
    if not raw and file_path:
        raw = Path(file_path).stem
    raw = re.sub(r"^\d{4}-\d{2}-\d{2}[- ]+", "", raw)
    raw = re.sub(r"[- ]+\d{4}-\d{2}-\d{2}(?:-\d+)?$", "", raw)
    raw = re.sub(r"[^\w\s-]", " ", raw)
    raw = re.sub(r"[_\s-]+", "-", raw.lower()).strip("-")
    return raw[:80].strip("-") or "session-doc"


def _is_unnamed_session_doc_base(base: str | None) -> bool:
    # Centralized in pane_surface so all four placeholder detectors agree and
    # numbered variants (needs-session-name-345) cannot leak into instance names.
    return bool(PLACEHOLDER_TAB_NAME_RX.match(base or ""))


async def _next_session_doc_instance_name(db: aiosqlite.Connection, doc_id: int) -> str:
    cursor = await db.execute(
        "SELECT title, file_path FROM session_documents WHERE id = ?", (doc_id,)
    )
    row = await cursor.fetchone()
    title = row[0] if row else None
    file_path = row[1] if row else None
    base = _instance_name_base_from_session_doc(title, file_path)
    if _is_unnamed_session_doc_base(base):
        return "needs-name"

    # Monotonic by existing suffix, not row count: stopped/historical rows
    # remain in the DB and prior instance renames may leave gaps.
    cursor = await db.execute("SELECT name FROM instances WHERE session_doc_id = ?", (doc_id,))
    rows = await cursor.fetchall()
    suffix_rx = re.compile(rf"^{re.escape(base)}-(\d+)$")
    max_suffix = 0
    for row in rows:
        match = suffix_rx.match(str(row[0] or ""))
        if match:
            max_suffix = max(max_suffix, int(match.group(1)))
    return f"{base}-{max_suffix + 1}"


async def _apply_session_doc_instance_name(
    db: aiosqlite.Connection,
    *,
    instance_id: str,
    session_doc_id: int | None,
    wrapper_launch_id: str | None = None,
) -> str | None:
    """Name instance from its session doc: `<doc-slug>-<monotonic ordinal>`."""
    if not session_doc_id:
        return None
    cursor = await db.execute(
        """
        SELECT ci.name, sd.title, sd.file_path
        FROM instances ci
        LEFT JOIN session_documents sd ON sd.id = ci.session_doc_id
        WHERE ci.id = ?
        """,
        (instance_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    base = _instance_name_base_from_session_doc(row[1], row[2])
    if _is_unnamed_session_doc_base(base):
        return None
    if re.match(rf"^{re.escape(base)}-\d+$", str(row[0] or "")):
        return str(row[0])
    new_name = await _next_session_doc_instance_name(db, session_doc_id)
    await sanctioned_update_instance(
        db,
        instance_id=instance_id,
        updates={"name": new_name},
        mutation_type="instance_updated",
        write_source="hooks",
        actor="SessionStart:session-doc-instance-name",
        wrapper_launch_id=wrapper_launch_id,
    )
    return new_name


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


def _row_parent_instance_id(row: dict) -> str | None:
    """Legacy `parent_instance_id` derived from the canonical commander edge: only a
    `commander_type='chapter'` edge carries a parent instance id (the column died
    with legacy instance table). Works for raw `SELECT *` rows that lack the alias."""
    if row.get("parent_instance_id") is not None:
        return row.get("parent_instance_id")
    if row.get("commander_type") == "chapter":
        return row.get("commander_id")
    return None


async def _enqueue_child_stop_fanout(instance: dict, payload: dict) -> dict | None:
    parent_instance_id = _normalize_text(_row_parent_instance_id(instance))
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


async def _resolve_instance_for_pane(db, pane: str | None) -> dict | None:
    raw = _normalize_text(pane)
    if not raw:
        return None
    resolved = await talk_service.resolve_pane(raw) or raw
    instance_id = await shared.instance_id_for_pane(resolved)
    db.row_factory = aiosqlite.Row
    row = None
    if instance_id:
        cursor = await db.execute(
            """SELECT id, name AS tab_name, engine, status, last_activity
               FROM instances
               WHERE id = ?
               ORDER BY CASE WHEN status = 'stopped' THEN 1 ELSE 0 END,
                        last_activity DESC
               LIMIT 1""",
            (instance_id,),
        )
        row = await cursor.fetchone()
    allow_stamped_pane_fallback = (
        os.environ.get("TOKEN_API_TEST_ALLOW_STAMPED_PANE_FALLBACK") == "1"
    )
    if not row and (not instance_id or allow_stamped_pane_fallback):
        # Compatibility fallback for legacy rows/tests that predate @INSTANCE_ID
        # stamps. Tests may opt into stamped fallback when the live tmux server
        # carries an @INSTANCE_ID from a different temporary Token-API database.
        cursor = await db.execute(
            """SELECT id, name AS tab_name, engine, status, last_activity
               FROM instances
               WHERE tmux_pane = ?
               ORDER BY CASE WHEN status = 'stopped' THEN 1 ELSE 0 END,
                        last_activity DESC
               LIMIT 1""",
            (resolved,),
        )
        row = await cursor.fetchone()
    if not row:
        return {"id": instance_id, "tmux_pane": resolved}
    result = dict(row)
    result["tmux_pane"] = resolved
    return result


async def _resolve_instance_by_id(db, instance_id: str | None) -> dict | None:
    raw = _normalize_text(instance_id)
    if not raw:
        return None
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """SELECT id, tmux_pane, name AS tab_name, engine, status, last_activity
           FROM instances
           WHERE id = ?
           ORDER BY last_activity DESC
           LIMIT 1""",
        (raw,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else {"id": raw}


async def _resolve_live_instance(db, instance_id: str | None) -> dict | None:
    """Return the instance row for ``instance_id`` ONLY if it is a live row.

    Live = present in instances with an active runtime status and a
    bound pane. Unlike ``_resolve_instance_by_id`` this never fabricates a
    ``{"id": raw}`` placeholder — a missing/dead/phantom id returns None, which
    is exactly what reconcile and prune need to verify true parentage and to
    detect dangling references.
    """
    raw = _normalize_text(instance_id)
    if not raw:
        return None
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """SELECT id, tmux_pane, pane_label, status
           FROM instances
           WHERE id = ?
             AND status NOT IN ('stopped', 'archived')
             AND tmux_pane IS NOT NULL
           LIMIT 1""",
        (raw,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def _upsert_stop_subscription(
    db,
    *,
    target_instance_id: str,
    target_pane: str | None,
    subscriber_instance_id: str | None,
    subscriber_pane: str,
    event: str = "stop",
    delivery: str = "prompt",
    purpose: str = "generic",
    payload: str | None = None,
    oneshot: bool = False,
) -> int:
    now = datetime.now().isoformat()
    event = event or "stop"
    delivery = delivery or "prompt"
    purpose = purpose or "generic"
    cursor = await db.execute(
        """INSERT INTO stop_hook_subscriptions
           (target_instance_id, target_pane, subscriber_instance_id, subscriber_pane,
            event, delivery, status, created_at, updated_at, purpose, payload, oneshot)
           VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
           ON CONFLICT(target_instance_id, subscriber_instance_id, subscriber_pane, event)
           DO UPDATE SET
             target_pane = excluded.target_pane,
             delivery = excluded.delivery,
             purpose = excluded.purpose,
             payload = excluded.payload,
             oneshot = excluded.oneshot,
             status = 'active',
             updated_at = excluded.updated_at,
             unsubscribed_at = NULL""",
        (
            target_instance_id,
            target_pane,
            subscriber_instance_id,
            subscriber_pane,
            event,
            delivery,
            now,
            now,
            purpose,
            payload,
            1 if oneshot else 0,
        ),
    )
    if cursor.lastrowid:
        return int(cursor.lastrowid)
    lookup = await db.execute(
        """SELECT id FROM stop_hook_subscriptions
           WHERE target_instance_id = ?
             AND COALESCE(subscriber_instance_id, '') = COALESCE(?, '')
             AND subscriber_pane = ?
             AND event = ?""",
        (target_instance_id, subscriber_instance_id, subscriber_pane, event),
    )
    row = await lookup.fetchone()
    return int(row[0]) if row else 0


async def _auto_subscribe_parent_on_start(
    db,
    *,
    child_instance_id: str,
    child_pane: str | None,
    parent_instance_id: str | None,
) -> dict | None:
    parent_instance_id = _normalize_text(parent_instance_id)
    if not parent_instance_id:
        return None
    parent = await _resolve_instance_by_id(db, parent_instance_id)
    parent_pane = _normalize_text((parent or {}).get("tmux_pane"))
    if not parent_pane:
        return None
    sub_id = await _upsert_stop_subscription(
        db,
        target_instance_id=child_instance_id,
        target_pane=child_pane,
        subscriber_instance_id=(parent or {}).get("id") or parent_instance_id,
        subscriber_pane=parent_pane,
        event="stop",
        delivery="prompt",
    )
    return {
        "subscription_id": sub_id,
        "target_instance_id": child_instance_id,
        "subscriber_instance_id": (parent or {}).get("id") or parent_instance_id,
        "subscriber_pane": parent_pane,
    }


def _is_mechanicus_worker_label(label: str | None) -> bool:
    label = _normalize_text(label)
    if not label:
        return False
    if label in {MECHANICUS_FG_LABEL, MECHANICUS_ADMIN_LABEL}:
        return False
    prefix, _, suffix = label.partition(":")
    if prefix != "mechanicus":
        return False
    if suffix.isdigit():
        return int(suffix) > 0
    return suffix == "worker" or suffix.startswith("worker-")


def _is_mechanicus_stack_window(value: str | None) -> bool:
    text = _normalize_text(value)
    return bool(text and re.match(r"^mechanicus(?:-\d+)?(?:\W.*)?$", text))


def _is_mechanicus_worker_row(row: dict) -> bool:
    label = row.get("effective_pane_label") or row.get("pane_label")
    if label in {MECHANICUS_FG_LABEL, MECHANICUS_ADMIN_LABEL}:
        return False
    if _is_mechanicus_worker_label(label):
        return True
    return _normalize_text(
        row.get("dispatch_target")
    ) == "mechanicus:new" or _is_mechanicus_stack_window(row.get("dispatch_window"))


async def _active_stop_subscription_id(
    db,
    *,
    target_instance_id: str,
    subscriber_instance_id: str | None,
    subscriber_pane: str,
    event: str = "stop",
) -> int | None:
    cursor = await db.execute(
        """SELECT id FROM stop_hook_subscriptions
           WHERE target_instance_id = ?
             AND COALESCE(subscriber_instance_id, '') = COALESCE(?, '')
             AND subscriber_pane = ?
             AND event = ?
             AND status = 'active'
           LIMIT 1""",
        (target_instance_id, subscriber_instance_id, subscriber_pane, event),
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else None


async def _active_hook_instances(db) -> list[dict]:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        """SELECT id, tmux_pane, pane_label, name AS tab_name, status, last_activity,
                  dispatch_target, dispatch_window,
                  CASE WHEN commander_type = 'chapter' THEN commander_id END AS parent_instance_id
           FROM instances
           WHERE status NOT IN ('stopped', 'archived')
             AND tmux_pane IS NOT NULL
           ORDER BY last_activity DESC, created_at DESC"""
    )
    return [dict(row) for row in await cursor.fetchall()]


async def _with_effective_pane_labels(rows: list[dict]) -> list[dict]:
    resolved: list[dict] = []
    for row in rows:
        item = dict(row)
        label = _normalize_text(item.get("pane_label"))
        if not label:
            label = await _tmux_pane_label(item.get("tmux_pane"))
        item["effective_pane_label"] = label
        resolved.append(item)
    return resolved


async def _find_live_fabricator_general(db) -> dict | None:
    rows = await _with_effective_pane_labels(await _active_hook_instances(db))
    for row in rows:
        if row.get("effective_pane_label") == MECHANICUS_FG_LABEL:
            return row
    return None


async def _reconcile_mechanicus_stop_subscriptions(
    db,
    *,
    source_instance_id: str | None = None,
) -> dict:
    """Ensure active Mechanicus stack workers deliver Stop notices to FG."""
    fg = await _find_live_fabricator_general(db)
    counts = {"created": 0, "existing": 0, "skipped": 0}
    skipped: list[dict] = []
    subscriptions: list[dict] = []
    if not fg or not fg.get("id") or not fg.get("tmux_pane"):
        return {
            "success": True,
            "action": "no_live_fabricator_general",
            "page": "mechanicus",
            **counts,
            "subscriber": None,
            "subscriptions": [],
        }

    rows = await _with_effective_pane_labels(await _active_hook_instances(db))
    if source_instance_id:
        rows = [row for row in rows if row.get("id") == source_instance_id]

    for row in rows:
        target_id = _normalize_text(row.get("id"))
        target_pane = _normalize_text(row.get("tmux_pane"))
        label = row.get("effective_pane_label")
        if not target_id or not target_pane:
            counts["skipped"] += 1
            skipped.append({"instance_id": target_id, "reason": "missing_target"})
            continue
        if target_id == fg.get("id") or label in {MECHANICUS_FG_LABEL, MECHANICUS_ADMIN_LABEL}:
            counts["skipped"] += 1
            skipped.append({"instance_id": target_id, "pane_label": label, "reason": "persona"})
            continue
        if not _is_mechanicus_worker_row(row):
            counts["skipped"] += 1
            skipped.append(
                {"instance_id": target_id, "pane_label": label, "reason": "not_mechanicus_worker"}
            )
            continue

        # Only subscribe VERIFIED true children of FG. A mechanicus-labelled pane
        # is NOT proof of parentage: the additive reconcile used to wire every
        # worker to FG, mis-attributing orphans (parent FK = dead/phantom uuid)
        # and workers owned by a different live commander. Require the worker's
        # parent FK to resolve to a LIVE row that IS this FG before subscribing.
        parent_id = _normalize_text(row.get("parent_instance_id"))
        parent = await _resolve_live_instance(db, parent_id)
        if not parent:
            counts["skipped"] += 1
            skipped.append(
                {
                    "instance_id": target_id,
                    "pane_label": label,
                    "reason": "parent_not_live",
                    "parent_instance_id": parent_id,
                }
            )
            continue
        if parent.get("id") != fg.get("id"):
            counts["skipped"] += 1
            skipped.append(
                {
                    "instance_id": target_id,
                    "pane_label": label,
                    "reason": "parent_not_fg",
                    "parent_instance_id": parent_id,
                }
            )
            continue

        existing_id = await _active_stop_subscription_id(
            db,
            target_instance_id=target_id,
            subscriber_instance_id=fg.get("id"),
            subscriber_pane=fg["tmux_pane"],
        )
        sub_id = await _upsert_stop_subscription(
            db,
            target_instance_id=target_id,
            target_pane=target_pane,
            subscriber_instance_id=fg.get("id"),
            subscriber_pane=fg["tmux_pane"],
            event="stop",
            delivery="prompt",
        )
        if existing_id:
            counts["existing"] += 1
        else:
            counts["created"] += 1
        subscriptions.append(
            {
                "subscription_id": sub_id,
                "target_instance_id": target_id,
                "target_pane": target_pane,
                "target_pane_label": label,
                "subscriber_instance_id": fg.get("id"),
                "subscriber_pane": fg["tmux_pane"],
            }
        )

    return {
        "success": True,
        "action": "reconciled",
        "page": "mechanicus",
        **counts,
        "subscriber": {
            "instance_id": fg.get("id"),
            "pane": fg.get("tmux_pane"),
            "pane_label": fg.get("effective_pane_label"),
        },
        "subscriptions": subscriptions,
        "skipped_targets": skipped,
    }


async def _prune_dangling_stop_subscriptions(
    db,
    *,
    confirm: bool,
    event: str = "stop",
    extra_live_ids: set[str] | None = None,
) -> dict:
    """Remove active subscriptions that reference a non-live instance.

    A subscription is dangling when its WATCHED (target) or NOTIFY (subscriber)
    instance_id has no live instance row — the watched pane was stopped, or the
    notify target is a phantom UUID with no instance row. `hook reconcile` is
    additive and never removes these, so they accumulate forever, inflate
    `hook list`, and resolve to nothing (false "passive/dead" reads). Dry-run by
    default: nothing is removed unless ``confirm`` is set.

    ``extra_live_ids`` augments the DB-derived live set with instance ids known to
    be live by some other oracle — specifically the sweep's tmux ``@INSTANCE_ID``
    stamps. A swept-but-live instance (the "live panes, dead rows" state) has a
    ``stopped`` row yet a running pane; without this union its still-valid hooks
    would be GC'd before the reconciler reactivates the row.
    """
    db.row_factory = aiosqlite.Row
    live_cursor = await db.execute(
        """SELECT id FROM instances
           WHERE status NOT IN ('stopped', 'archived') AND tmux_pane IS NOT NULL"""
    )
    live_ids = {row["id"] for row in await live_cursor.fetchall()}
    if extra_live_ids:
        live_ids |= extra_live_ids

    cursor = await db.execute(
        """SELECT id, target_instance_id, target_pane, subscriber_instance_id,
                  subscriber_pane, purpose
           FROM stop_hook_subscriptions
           WHERE status = 'active' AND event = ?
           ORDER BY id""",
        (event,),
    )
    rows = [dict(row) for row in await cursor.fetchall()]

    removable: list[dict] = []
    for row in rows:
        target_id = _normalize_text(row.get("target_instance_id"))
        sub_id = _normalize_text(row.get("subscriber_instance_id"))
        reasons: list[str] = []
        if not target_id or target_id not in live_ids:
            reasons.append("watched_not_live")
        # subscriber_instance_id may legitimately be NULL (pane-only notify);
        # only a NON-null id that resolves to no live row is a dangling ref.
        if sub_id and sub_id not in live_ids:
            reasons.append("notify_not_live")
        if reasons:
            removable.append({**row, "reasons": reasons})

    if confirm and removable:
        now = datetime.now().isoformat()
        await db.executemany(
            """UPDATE stop_hook_subscriptions
               SET status = 'unsubscribed', unsubscribed_at = ?, updated_at = ?
               WHERE id = ?""",
            [(now, now, row["id"]) for row in removable],
        )
        await db.commit()

    return {
        "success": True,
        "action": "pruned" if confirm else "prune_preview",
        "confirmed": confirm,
        "event": event,
        "count": len(removable),
        "active_remaining": len(rows) - (len(removable) if confirm else 0),
        "removed": removable,
    }


async def _reconcile_mechanicus_on_session_start(db, instance_id: str) -> dict | None:
    rows = await _with_effective_pane_labels(await _active_hook_instances(db))
    current = next((row for row in rows if row.get("id") == instance_id), None)
    if not current:
        return None
    label = current.get("effective_pane_label")
    if label == MECHANICUS_FG_LABEL:
        return await _reconcile_mechanicus_stop_subscriptions(db)
    if _is_mechanicus_worker_row(current):
        return await _reconcile_mechanicus_stop_subscriptions(db, source_instance_id=instance_id)
    return None


def _stop_event_key(session_id: str, payload: dict) -> str:
    path = payload.get("transcript_path")
    if path:
        try:
            p = Path(path)
            if p.exists():
                st = p.stat()
                return f"transcript:{p}:{st.st_mtime_ns}:{st.st_size}"
        except OSError:
            pass
    stable = {
        k: v
        for k, v in payload.items()
        if not str(k).startswith("_") and k not in {"stop_hook_active"}
    }
    raw = json.dumps(stable, sort_keys=True, default=str)
    return f"payload:{session_id}:{hashlib.sha256(raw.encode()).hexdigest()}"


async def _direct_pane_write(tmux_pane: str, payload: str) -> dict:
    """Live prompt delivery through the verified pane-write primitive.

    Routes through main.py's gate-aware ``_tmux_send_payload_then_submit``
    (injected via init_deps) instead of the old bespoke send that discarded the
    adapter's result and hardcoded ``{"status": "sent"}`` — and then fell back to
    a raw ``tmux send-keys`` that reported ``sent`` on rc==0 alone, with no
    delivery proof and no respect for the universal send gate. Both were lies:
    "fired but nothing arrived" while the system believed it delivered.

    Reports the truth instead:

      * ``sent``       — submission verified (the composer cleared after submit)
      * ``unverified`` — bytes were issued but delivery is not yet proven; the
                          proof belt (UserPromptSubmit correlation) confirms it
                          asynchronously
      * ``gated``      — the universal send gate suppressed the write (NO bytes
                          reached the pane); never reported as delivered
      * ``failed``     — the send errored

    The full primitive result is returned under ``send`` so callers can persist
    the verification record.
    """
    send = _tmux_send_payload_then_submit
    if send is None:
        return {"status": "failed", "error": "pane-write primitive not initialized"}
    result = await send(tmux_pane, payload)
    if result.get("gated"):
        # Gate suppressed the send — no bytes issued. NOT a delivery.
        return {
            "status": "gated",
            "verification_status": result.get("verification_status", "gated"),
            "gate_reason": result.get("gate_reason"),
            "send": result,
        }
    if result.get("returncode") == 0:
        # Bytes issued. Only a confirmed submission (composer cleared) is
        # "sent"; otherwise it is honestly "unverified" until the proof belt
        # correlates a UserPromptSubmit. Never default bytes-issued to "sent".
        verified = result.get("verification_status") == "submitted"
        return {
            "status": "sent" if verified else "unverified",
            "verification_status": result.get("verification_status"),
            "operation": result.get("operation"),
            "send": result,
        }
    return {
        "status": "failed",
        "error": result.get("stderr") or result.get("error"),
        "send": result,
    }


async def _flag_subscriber_hook_driven(db, subscription: dict) -> None:
    """Flag a stop-subscription subscriber hook_driven=1 before delivery — the
    subscriber (e.g. FG watching its stack workers) is being woken autonomously by
    the watched instance's Stop, NOT by the Emperor. Resolve by the LIVE occupant of
    subscriber_pane first, falling back to subscriber_instance_id only when the pane
    yields no live row. A subscriber's instance id rotates on resume while it keeps
    its pane (the live `1402f092`→`f55ac307`-at-`%96` FG split); trusting the recorded
    id first would flag a now-dead row (a silent no-op) and the autonomous-wakeup
    marker would never reach the live subscriber. Committed by the caller before the
    byte lands (see the commit preceding _direct_pane_write). Best-effort."""
    declared_id = _normalize_text(subscription.get("subscriber_instance_id"))
    pane = _normalize_text(subscription.get("subscriber_pane"))
    target_id = None
    try:
        if pane:
            target_id = await shared.instance_id_for_pane(pane)
        # Fall back to the recorded subscriber id only when the pane has no live row.
        if not target_id:
            target_id = declared_id
        if not target_id:
            return
        await sanctioned_update_instance(
            db,
            instance_id=target_id,
            updates={"hook_driven": 1},
            mutation_type="status_changed",
            write_source="hooks",
            actor="stop-subscription-delivery",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("hook_driven flag (stop-subscription) failed: %s", exc)


async def _enqueue_and_send_stop_delivery(
    db,
    *,
    subscription: dict,
    stop_event_key: str,
    payload: str,
) -> dict:
    # Subscriber is woken autonomously by this Stop — flag it before the send. The
    # flag is committed together with the delivery rows below (db.commit precedes
    # _direct_pane_write), so the subscriber's PromptSubmit can't observe a stale 0.
    await _flag_subscriber_hook_driven(db, subscription)
    delivery_id: int | None = None
    queue_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    try:
        cursor = await db.execute(
            """INSERT INTO stop_hook_deliveries
               (subscription_id, target_instance_id, subscriber_instance_id,
                subscriber_pane, event, stop_event_key, delivery, status,
                payload_json, pane_write_queue_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
            (
                subscription["id"],
                subscription["target_instance_id"],
                subscription["subscriber_instance_id"],
                subscription["subscriber_pane"],
                subscription["event"],
                stop_event_key,
                subscription["delivery"],
                json.dumps({"prompt": payload}, sort_keys=True),
                queue_id,
                now,
            ),
        )
        delivery_id = int(cursor.lastrowid)
    except aiosqlite.IntegrityError:
        return {"status": "duplicate", "subscription_id": subscription["id"]}

    await db.execute(
        """INSERT INTO pane_write_queue
           (id, instance_id, tmux_pane, source, purpose, payload, status, created_at, updated_at)
           VALUES (?, ?, ?, 'hook', 'stop_subscription', ?, 'pending', ?, ?)""",
        (
            queue_id,
            subscription["subscriber_instance_id"] or subscription["subscriber_pane"],
            subscription["subscriber_pane"],
            payload,
            now,
            now,
        ),
    )
    await db.commit()

    send_result = await _direct_pane_write(subscription["subscriber_pane"], payload)
    delivery_status = send_result.get("status")
    # Map delivery truth onto the durable queue row + the delivery record:
    #   sent/unverified -> bytes were issued, so the queue row is terminal
    #       'sent' (re-queuing would double the send); the proof belt upgrades
    #       an 'unverified' delivery asynchronously.
    #   gated           -> the universal gate suppressed the write (NO bytes).
    #       Keep the pane_write_queue row 'pending' so the periodic worker
    #       re-drains it when the gate clears — never reported as delivered.
    #   failed          -> the send errored.
    bytes_issued = delivery_status in ("sent", "unverified")
    if delivery_status == "gated":
        queue_status = "pending"
    elif bytes_issued:
        queue_status = "sent"
    else:
        queue_status = "failed"
    # The delivery record carries the precise truth, including 'unverified'.
    delivery_record_status = (
        delivery_status
        if delivery_status in ("sent", "unverified", "gated", "failed")
        else "failed"
    )
    delivered_at = datetime.now().isoformat() if bytes_issued else None
    error = send_result.get("error") or send_result.get("gate_reason")
    await db.execute(
        """UPDATE pane_write_queue
           SET status = ?, attempted_at = ?, sent_at = ?, updated_at = ?,
               last_error = ?, last_result_json = ?
           WHERE id = ?""",
        (
            queue_status,
            now,
            now if bytes_issued else None,
            datetime.now().isoformat(),
            error,
            json.dumps(send_result, sort_keys=True),
            queue_id,
        ),
    )
    await db.execute(
        """UPDATE stop_hook_deliveries
           SET status = ?, delivered_at = ?, error = ?
           WHERE id = ?""",
        (
            delivery_record_status,
            delivered_at,
            error,
            delivery_id,
        ),
    )
    await db.commit()
    return {
        "status": delivery_status,
        "delivery_id": delivery_id,
        "queue_id": queue_id,
        "subscriber_pane": subscription["subscriber_pane"],
        "send": send_result,
    }


async def _fanout_stop_subscriptions(
    instance: dict, payload: dict, final_response: str | None
) -> list[dict]:
    session_id = instance["id"]
    stop_event_key = _stop_event_key(session_id, payload)
    surface = human_pane_surface(
        instance.get("name") or instance.get("tab_name"),
        instance.get("tmux_pane"),
        instance.get("pane_label"),
    )
    name = surface if surface != "session" else session_id[:12]
    response = (final_response or "").strip()
    if len(response) > 4000:
        response = response[:4000] + "\n… [truncated]"
    default_notice = (
        "<system-reminder>\n"
        f"Stop-hook subscription: {name} ({session_id[:12]}) stopped.\n\n"
        "Final response:\n"
        f"{response or '[no final assistant text captured]'}\n"
        "</system-reminder>"
    )
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT * FROM stop_hook_subscriptions
               WHERE target_instance_id = ?
                 AND event = 'stop'
                 AND status = 'active'
               ORDER BY created_at ASC, id ASC""",
            (session_id,),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        results = []
        for row in rows:
            delivery_payload = row.get("payload") or default_notice
            result = await _enqueue_and_send_stop_delivery(
                db,
                subscription=row,
                stop_event_key=stop_event_key,
                payload=delivery_payload,
            )
            results.append(result)
            if row.get("oneshot") and result.get("status") != "duplicate":
                now = datetime.now().isoformat()
                await db.execute(
                    """UPDATE stop_hook_subscriptions
                       SET status = 'delivered', unsubscribed_at = ?, updated_at = ?
                       WHERE id = ? AND status = 'active'""",
                    (now, now, row["id"]),
                )
                await db.commit()
        return results


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
    """Handle terminal wrapper exit and stop the correlated live instance row.

    Claude normally emits SessionEnd before WrapperEnd; Codex and crashy wrappers
    may not. WrapperEnd is terminal, so use wrapper_launch_id as the durable
    correlation key and mark any still-live row stopped. This is best-effort and
    idempotent: already stopped/archived/retired rows are left alone.
    """
    wrapper_launch_id = _normalize_text(
        payload.get("wrapper_launch_id")
        or payload.get("env", {}).get("TOKEN_API_WRAPPER_LAUNCH_ID", "")
    )
    tmux_pane = _normalize_text(
        payload.get("tmux_pane") or payload.get("env", {}).get("TMUX_PANE", "")
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
        "tmux_pane": tmux_pane,
        "pid": payload.get("pid"),
        "exit_code": payload.get("exit_code"),
        "source": "wrapper",
    }
    stopped_instance_id = None
    if wrapper_launch_id:
        now = datetime.now().isoformat()
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT id, status, rank, tmux_pane
                   FROM instances
                   WHERE wrapper_launch_id = ?
                   ORDER BY last_activity DESC, created_at DESC
                   LIMIT 1""",
                (wrapper_launch_id,),
            )
            row = await cursor.fetchone()
            if row and row["status"] not in {"stopped", "archived"} and row["rank"] != "retired":
                stopped_instance_id = row["id"]
                await sanctioned_update_instance(
                    db,
                    instance_id=stopped_instance_id,
                    updates={
                        "status": "stopped",
                        "input_lock": None,
                        "stopped_at": now,
                        "hook_driven": 0,
                        "golden_throne": None,
                    },
                    mutation_type="instance_stopped",
                    write_source="hooks",
                    actor="WrapperEnd",
                    wrapper_launch_id=wrapper_launch_id,
                )
                await db.commit()
                pane_to_clear = row["tmux_pane"] or tmux_pane
                if pane_to_clear:
                    try:
                        await asyncio.to_thread(
                            shared.clear_pane_tint, pane_to_clear, source="WrapperEnd"
                        )
                    except Exception:
                        pass
    details["stopped_instance_id"] = stopped_instance_id
    await log_event("wrapper_end", instance_id=stopped_instance_id or None, details=details)
    return {
        "success": True,
        "action": "wrapper_end_logged"
        if not stopped_instance_id
        else "wrapper_end_stopped_instance",
        "wrapper_launch_id": wrapper_launch_id,
        "instance_id": stopped_instance_id,
    }


# ============ Launch Zealotry Parsing ============


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
    tab_name = tab_name or "needs-name"

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

    # The pane's @INSTANCE_ID stamp as read by the hook itself, atomically with
    # the event. The live tmuxctl lookup stays preferred (fresher), but when it
    # misses (latency, SMB stall, racing the stamp) this is the only surviving
    # occupancy signal — without it a plan-approval context-clear INSERTs a
    # duplicate row and strands the prior row's persona/rank identity.
    pane_stamp_instance_id = _normalize_text(payload.get("pane_instance_id") or "") or None
    if pane_stamp_instance_id == session_id:
        # Own stamp: same instance re-registering, not a supplant source.
        pane_stamp_instance_id = None

    # Auto-name subagents
    if is_subagent and not payload.get("env", {}).get("CLAUDE_TAB_NAME"):
        tab_name = f"sub: {subagent_env or 'agent'}"

    # Resolve device_id from HTTP client IP (where the instance actually runs)
    # SSH_CLIENT gives the SSH origin (Mac), not the instance's machine (WSL)
    client_ip = payload.get("_client_ip")
    if not source_ip:
        source_ip = client_ip
    device_id = resolve_device_from_ip(client_ip) if client_ip else "Mac-Mini"

    # Detect persona (env var) and transplant-from (file-based handoff injected by hook).
    primarch_name = _normalize_text(env.get("TOKEN_API_PERSONA", "")) or ""
    dispatch_legion = _normalize_text(
        payload.get("dispatch_legion") or env.get("TOKEN_API_LEGION", "")
    )
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
    # Persona/orchestrator pane (tmuxctl stamps a stable @PANE_ID like
    # "legion:custodes" / "mechanicus:fabricator-general" / "mechanicus:admin").
    # A fresh spawn in one of these panes IS that persona — derive its row identity
    # from the pane so the agent never self-PATCHes legion/primarch/type/synced.
    # The pane is authoritative for the persona's legion (written below in the
    # auto_legion block regardless of env); here we only fill the blanks the launch
    # left in the env-derived fields (dispatch_legion for doc resolution, primarch,
    # instance_type) so an explicit dispatch can still tune those.
    persona_identity = PERSONA_PANE_IDENTITY.get(pane_label or "")
    if persona_identity:
        if not dispatch_legion:
            dispatch_legion = persona_identity["legion"]
        if not primarch_name:
            primarch_name = persona_identity.get("primarch") or ""
        if launch_instance_type is None:
            launch_instance_type = persona_identity.get("instance_type")
        # A persona singleton is Emperor-commanded, never a chapter child. A
        # relaunch chain (old persona session dispatching/resuming its successor)
        # leaks the predecessor into TOKEN_API_PARENT_INSTANCE_ID; honoring it
        # registers the new row with commander_type='chapter', which exempts it
        # from the singleton guard, the default-rank stamp triggers, and
        # resolve_live_persona_instance — leaving the dead predecessor as the
        # resolvable singleton (live custodes 6a8773e9 commanded by its own
        # zombie d865db2e).
        parent_instance_id = ""

    def _effective_parent(prior_parent: str | None) -> str | None:
        # Same invariant for every restore path (supplant, --continue, prior
        # dispatch env): a persona singleton never inherits a parent — not from
        # the launch env (cleared above) and not from a poisoned prior row.
        if persona_identity:
            return ""
        return parent_instance_id or prior_parent

    persona_synced = bool(persona_identity and persona_identity.get("synced"))
    launch_zealotry = _parse_launch_zealotry(
        payload.get("zealotry") or env.get("TOKEN_API_ZEALOTRY", "")
    )
    session_doc_policy = None
    dispatch_bound_doc = False

    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row

        # Cron-launched custodes carries its legion on the cron_jobs row, not in
        # the dispatch env. Resolve it up front so resolve_session_doc_for_start
        # can take the daily-note branch for cron jobs too.
        if not dispatch_legion and origin_type == "cron":
            cron_job_id = env.get("CRON_JOB_ID")
            if cron_job_id:
                cursor = await db.execute(
                    "SELECT legion FROM cron_jobs WHERE id = ?", (cron_job_id,)
                )
                cron_legion_row = await cursor.fetchone()
                if cron_legion_row and cron_legion_row[0]:
                    dispatch_legion = _normalize_text(cron_legion_row[0])

        # Check if already registered
        cursor = await db.execute("SELECT * FROM instances WHERE id = ?", (session_id,))
        existing_row = await cursor.fetchone()

        # Legacy-shaped derivations off the instance row (these columns died with
        # legacy instance table): parent_instance_id lives in commander_id when the
        # commander edge is a chapter; primarch identity lives in persona_id.
        existing_parent_id = (
            existing_row["commander_id"]
            if existing_row is not None and existing_row["commander_type"] == "chapter"
            else None
        )
        launch_persona_id = None
        if primarch_name:
            persona_slug = LEGACY_PERSONA_ALIASES.get(
                primarch_name.strip().lower(), primarch_name.strip().lower()
            )
            cursor = await db.execute("SELECT id FROM personas WHERE slug = ?", (persona_slug,))
            persona_row = await cursor.fetchone()
            launch_persona_id = persona_row["id"] if persona_row else None
        # Persona panes assert their identity rank explicitly in every in-place
        # update. Retiring a poisoned row's old commander (singleton guard)
        # cascades trg_instances_retire_children back onto the row mid-statement
        # — it is still that commander's chapter child at statement start — and
        # the cascade's rank='retired' survives any column the statement does not
        # assign. Explicit assignment makes the outcome deterministic; it is a
        # no-op against the stamp trigger when the rank already matches.
        persona_default_rank = None
        if persona_identity and launch_persona_id is not None:
            cursor = await db.execute(
                "SELECT default_rank FROM personas WHERE id = ?", (launch_persona_id,)
            )
            rank_row = await cursor.fetchone()
            persona_default_rank = rank_row["default_rank"] if rank_row else None

        # --- Supplant logic: reuse existing instance row instead of creating new ---
        # Priority: DB transplant marker > hook file handoff > primarch singleton
        supplant_id = None

        # 1. Check DB for pending transplant targeting this session (cross-device safe)
        cursor = await db.execute(
            "SELECT id FROM instances WHERE transplant_target_session = ?", (session_id,)
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

        # 3. Persona singleton (reuse most recent instance bound to the same
        # persona — the legacy `primarch` column died into persona_id).
        if not supplant_id and primarch_name:
            persona_slug = LEGACY_PERSONA_ALIASES.get(
                primarch_name.strip().lower(), primarch_name.strip().lower()
            )
            cursor = await db.execute(
                """SELECT i.id FROM instances i
                   JOIN personas p ON p.id = i.persona_id
                   WHERE p.slug = ?
                   ORDER BY i.created_at DESC LIMIT 1""",
                (persona_slug,),
            )
            row = await cursor.fetchone()
            if row:
                supplant_id = row["id"]

        # 3b. Persona pane-label singleton (FG / Administratum / Custodes). A persona
        # pane is bound by its tmux pane label, and the `primarch` derived above comes
        # from that label via PERSONA_PANE_IDENTITY — but ONLY when the label resolves
        # at SessionStart. On a fresh persona resume the @PANE_ID may not be stamped
        # yet, so `_tmux_pane_label` returns nothing, no primarch is derived, and the
        # primarch-singleton case (3) cannot fire. The result is a *duplicate* persona
        # row while the prior row lingers un-demoted (its stop subscriptions orphaned —
        # the live `1402f092`/`f55ac307`-at-`%96` split). Supplant the persona row
        # already occupying this pane, keyed off ITS persisted primarch rather than the
        # (possibly-unresolved) new registration's label.
        if not supplant_id and tmux_pane:
            # Gate strictly to the KNOWN persona primarchs (from PERSONA_PANE_IDENTITY),
            # resolving pane occupancy from tmuxctl's live @INSTANCE_ID stamp instead
            # of the deprecated stored tmux_pane column.
            persona_primarchs = sorted(
                {v["primarch"] for v in PERSONA_PANE_IDENTITY.values() if v.get("primarch")}
            )
            live_pane_instance_id = (
                await shared.instance_id_for_pane(tmux_pane) or pane_stamp_instance_id
            )
            if live_pane_instance_id and live_pane_instance_id != session_id:
                placeholders = ",".join("?" for _ in persona_primarchs)
                cursor = await db.execute(
                    f"""SELECT i.id FROM instances i
                        JOIN personas p ON p.id = i.persona_id
                        WHERE i.id = ?
                          AND p.slug IN ({placeholders})
                        ORDER BY i.created_at DESC LIMIT 1""",
                    (live_pane_instance_id, *persona_primarchs),
                )
                row = await cursor.fetchone()
                if row:
                    supplant_id = row["id"]

        # 4. Pane-occupant match (covers plan-mode context-clear: Claude Code emits
        # a fresh session_id but the underlying process keeps the same tmux pane).
        # Without this, a custodes plan-mode exit spawns a duplicate row and the prior
        # row's persona_id/rank identity is stranded — breaking the state-hook
        # dispatcher's persona+rank resolution (resolve_live_persona_instance).
        # Prefer tmuxctl's live @INSTANCE_ID pane stamp; fall back to stored tmux_pane
        # only for rows created before stamps existed.
        if not supplant_id:
            payload_pid = payload.get("pid")
            if payload_pid and tmux_pane:
                live_pane_instance_id = (
                    await shared.instance_id_for_pane(tmux_pane) or pane_stamp_instance_id
                )
                if live_pane_instance_id:
                    cursor = await db.execute(
                        """SELECT id FROM instances
                           WHERE id = ?
                             AND status NOT IN ('stopped', 'archived')
                           ORDER BY created_at DESC LIMIT 1""",
                        (live_pane_instance_id,),
                    )
                else:
                    # Legacy fallback for rows created before @INSTANCE_ID stamps.
                    cursor = await db.execute(
                        """SELECT id FROM instances
                           WHERE tmux_pane = ?
                             AND status NOT IN ('stopped', 'archived')
                           ORDER BY created_at DESC LIMIT 1""",
                        (tmux_pane,),
                    )
                row = await cursor.fetchone()
                if row:
                    supplant_id = row["id"]

        # 5. Wrapper-launch adoption (stamp-independent in-wrapper backstop).
        # An in-wrapper re-fire (plan-accept / `/clear` / compaction) emits a
        # fresh session_id but keeps the SAME wrapper_launch_id — present in the
        # SessionStart payload, unique per wrapper launch (so it strings together
        # re-fires but correctly does NOT span a full close→reboot, which mints a
        # fresh independent instance). When the @INSTANCE_ID stamp is lost before
        # this re-fire can read it (the race that defeated the case-4 / payload
        # stamp paths — Layer 1 removes the trigger, this guarantees the outcome),
        # wrapper_launch_id is the durable continuity key that survives. Adopt the
        # most-recent non-archived row carrying it so the supplant path re-keys
        # that row (one row, same session_doc_id) instead of minting a duplicate +
        # orphan doc. Scoped to a fresh registration (no existing_row) so a
        # --continue re-register stays on its own id.
        if not supplant_id and not existing_row and wrapper_launch_id:
            cursor = await db.execute(
                """SELECT id FROM instances
                   WHERE wrapper_launch_id = ?
                     AND status != 'archived'
                     AND COALESCE(is_subagent, 0) = 0
                   ORDER BY created_at DESC LIMIT 1""",
                (wrapper_launch_id,),
            )
            row = await cursor.fetchone()
            if row and row["id"] != session_id:
                supplant_id = row["id"]

        # --- Handle --continue (same session ID) with transplant ---
        # With --continue, the session ID doesn't change. If the row already exists
        # and there's a transplant signal, update the row in-place (new device, dir, pid).
        # If no transplant signal, it's a normal re-registration (no-op).
        if existing_row:
            if supplant_id and supplant_id == session_id:
                resolved_session_doc_id = None
                resolved_session_doc_policy = None
                if (
                    dispatch_session_doc_path
                    or primarch_name
                    or dispatch_legion
                    or origin_type == "cron"
                ):
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
                        legion=dispatch_legion or None,
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

                # Same-ID transplant (--continue): update the existing row in-place.
                # pid died with legacy instance table; the commander edge (legacy
                # parent_instance_id) is applied by _apply_commander_binding
                # below; primarch/instance_type land on persona_id/golden_throne.
                now = datetime.now().isoformat()
                transplant_updates = {
                    "working_dir": working_dir,
                    "device_id": device_id,
                    "status": "idle",
                    "last_activity": now,
                    "stopped_at": None,
                    "victory_at": None,
                    "victory_reason": None,
                    "input_lock": None,
                    # A resumed session is never mid-modal — reconcile any
                    # stuck planning_state (the transplant case is the classic
                    # offender). No-op for rows already at `none` (the trigger's
                    # WHEN guard suppresses noise).
                    "planning_state": "none",
                    "planning_updated_at": now,
                    "planning_source": "auto-clear:session-start",
                    "transplant_target_session": None,
                    "session_doc_id": resolved_session_doc_id or existing_row["session_doc_id"],
                    "wrapper_launch_id": wrapper_launch_id or existing_row["wrapper_launch_id"],
                    "launcher": launcher or existing_row["launcher"],
                    "engine": engine or existing_row["engine"],
                    "dispatch_mode": dispatch_mode or existing_row["dispatch_mode"],
                    "dispatch_session_doc_path": dispatch_session_doc_path
                    or existing_row["dispatch_session_doc_path"],
                    "target_working_dir": target_working_dir or existing_row["target_working_dir"],
                    "launch_mode": launch_mode or existing_row["launch_mode"],
                    "transplant_expected": 1 if transplant_expected else 0,
                    "zealotry": launch_zealotry
                    if launch_zealotry is not None
                    else existing_row["zealotry"],
                    "session_doc_policy": session_doc_policy or existing_row["session_doc_policy"],
                }
                if persona_identity:
                    # _effective_parent stops a persona row from re-inheriting a
                    # poisoned parent, but an in-place refresh keeps whatever
                    # chapter edge the row already carries — clear it explicitly
                    # (the commander binding below no-ops on an empty parent).
                    transplant_updates["commander_type"] = "emperor"
                    transplant_updates["commander_id"] = None
                    if persona_default_rank:
                        transplant_updates["rank"] = persona_default_rank
                if launch_persona_id is not None:
                    transplant_updates["persona_id"] = launch_persona_id
                if launch_instance_type:
                    transplant_updates["golden_throne"] = await _launch_golden_throne_marker(
                        db,
                        launch_instance_type,
                        zealotry=launch_zealotry,
                        existing_marker=existing_row["golden_throne"],
                    )
                await sanctioned_update_instance(
                    db,
                    instance_id=session_id,
                    updates=transplant_updates,
                    mutation_type="instance_updated",
                    write_source="hooks",
                    actor="SessionStart",
                    wrapper_launch_id=wrapper_launch_id or existing_row["wrapper_launch_id"],
                )
                await _persist_runtime_fields(
                    db,
                    instance_id=session_id,
                    tmux_pane=tmux_pane,
                    pane_label=pane_label or existing_row["pane_label"],
                    dispatch_target=dispatch_target,
                    dispatch_window=dispatch_window,
                    dispatch_slot=dispatch_slot,
                )
                await _apply_commander_binding(
                    db,
                    instance_id=session_id,
                    dispatch_target=dispatch_target,
                    parent_instance_id=_effective_parent(existing_parent_id),
                    dispatch_mode=dispatch_mode,
                )
                await _stamp_instance_id(tmux_pane, session_id, display_name=existing_row["name"])
                if old_tmux_pane and old_tmux_pane != tmux_pane:
                    await _unstamp_instance_id(old_tmux_pane, session_id)
                await _apply_instance_workflow_state(
                    db,
                    instance_id=session_id,
                    session_doc_id=resolved_session_doc_id or existing_row["session_doc_id"],
                    session_doc_policy=session_doc_policy or existing_row["session_doc_policy"],
                    workflow_state=workflow_state,
                    previous_session_doc_id=existing_row["session_doc_id"],
                    previous_workflow_state=existing_row["workflow_state"],
                )
                await _apply_session_doc_instance_name(
                    db,
                    instance_id=session_id,
                    session_doc_id=resolved_session_doc_id or existing_row["session_doc_id"],
                    wrapper_launch_id=wrapper_launch_id or existing_row["wrapper_launch_id"],
                )
                await db.commit()
                auto_subscription = await _auto_subscribe_parent_on_start(
                    db,
                    child_instance_id=session_id,
                    child_pane=tmux_pane,
                    parent_instance_id=_effective_parent(existing_parent_id),
                )
                if auto_subscription:
                    await db.commit()
                mechanicus_subscription = await _reconcile_mechanicus_on_session_start(
                    db, session_id
                )
                if mechanicus_subscription and (
                    mechanicus_subscription.get("created")
                    or mechanicus_subscription.get("existing")
                ):
                    await db.commit()

                # Event-driven tint: the persona moved panes. Clear the vacated
                # pane and paint the new one from canonical instances.persona_id
                # → personas.pane_tint (no recolor queue).
                # Tint is cosmetic — best-effort, never fail registration on it.
                try:
                    if old_tmux_pane and old_tmux_pane != tmux_pane:
                        await asyncio.to_thread(
                            shared.clear_pane_tint, old_tmux_pane, source="transplant-vacate"
                        )
                    if tmux_pane:
                        await shared.apply_instance_pane_tint(
                            db, session_id, tmux_pane, source="transplant"
                        )
                except Exception as exc:
                    logger.warning(
                        "Hook: SessionStart transplant tint repaint failed for %s: %s",
                        session_id[:12],
                        exc,
                    )
                await _apply_commander_binding(
                    db,
                    instance_id=session_id,
                    dispatch_target=dispatch_target,
                    parent_instance_id=_effective_parent(existing_parent_id),
                    dispatch_mode=dispatch_mode,
                )
                await db.commit()

                cursor = await db.execute(
                    """SELECT i.*, (SELECT slug FROM personas WHERE id = i.persona_id)
                              AS persona_slug
                       FROM instances i WHERE i.id = ?""",
                    (session_id,),
                )
                updated_inst = await cursor.fetchone()
                # profiles are persona-keyed; legacy profile_name died into persona_id
                prof = profile_by_name(updated_inst["persona_slug"] if updated_inst else None)
                hex_color = (prof.get("chip_color") or prof.get("color")) if prof else "#666666"
                pane_tint = prof.get("pane_tint") if prof else None

                logger.info(
                    f"Hook: SessionStart transplant-refresh {session_id[:12]}... ({working_dir}) [device:{device_id}]"
                )
                return {
                    "success": True,
                    "action": "transplant_refreshed",
                    "instance_id": session_id,
                    "profile": updated_inst["persona_slug"] if updated_inst else None,
                    "color": hex_color,
                    "chip_color": hex_color,
                    "pane_tint": pane_tint,
                    "session_doc_id": updated_inst["session_doc_id"] if updated_inst else None,
                    "stop_subscription": auto_subscription,
                    "mechanicus_stop_subscription": mechanicus_subscription,
                }
            else:
                # Normal re-registration / Codex resume. Refresh transport fields so
                # a live pane cannot remain represented by a stale stopped row.
                now = datetime.now().isoformat()
                # pid died with legacy instance table; the commander edge (legacy
                # parent_instance_id) is applied by _apply_commander_binding
                # below; instance_type lands on the golden_throne marker.
                updates = {
                    "working_dir": working_dir,
                    "device_id": device_id,
                    "status": "idle",
                    "last_activity": now,
                    "stopped_at": None,
                    "victory_at": None,
                    "victory_reason": None,
                    "input_lock": None,
                    # A resumed session is never mid-modal — reconcile any stuck
                    # planning_state. No-op for rows already at `none`.
                    "planning_state": "none",
                    "planning_updated_at": now,
                    "planning_source": "auto-clear:session-start",
                    "wrapper_launch_id": wrapper_launch_id or existing_row["wrapper_launch_id"],
                    "launcher": launcher or existing_row["launcher"],
                    "engine": engine or existing_row["engine"],
                    "dispatch_mode": dispatch_mode or existing_row["dispatch_mode"],
                    "dispatch_session_doc_path": dispatch_session_doc_path
                    or existing_row["dispatch_session_doc_path"],
                    "target_working_dir": target_working_dir or existing_row["target_working_dir"],
                    "launch_mode": launch_mode or existing_row["launch_mode"],
                    "transplant_expected": 1
                    if transplant_expected
                    else existing_row["transplant_expected"],
                    "zealotry": launch_zealotry
                    if launch_zealotry is not None
                    else existing_row["zealotry"],
                    "session_doc_policy": session_doc_policy or existing_row["session_doc_policy"],
                }
                if persona_identity:
                    # Persona singletons stay Emperor-commanded: clear any chapter
                    # edge the refreshed row already carries (see transplant path).
                    updates["commander_type"] = "emperor"
                    updates["commander_id"] = None
                    if persona_default_rank:
                        updates["rank"] = persona_default_rank
                if launch_instance_type:
                    updates["golden_throne"] = await _launch_golden_throne_marker(
                        db,
                        launch_instance_type,
                        zealotry=launch_zealotry,
                        existing_marker=existing_row["golden_throne"],
                    )
                await sanctioned_update_instance(
                    db,
                    instance_id=session_id,
                    updates=updates,
                    mutation_type="instance_updated",
                    write_source="hooks",
                    actor="SessionStart",
                    wrapper_launch_id=wrapper_launch_id or existing_row["wrapper_launch_id"],
                )
                await _persist_runtime_fields(
                    db,
                    instance_id=session_id,
                    tmux_pane=tmux_pane or existing_row["tmux_pane"],
                    pane_label=pane_label or existing_row["pane_label"],
                    dispatch_target=dispatch_target,
                    dispatch_window=dispatch_window,
                    dispatch_slot=dispatch_slot,
                )
                await _apply_commander_binding(
                    db,
                    instance_id=session_id,
                    dispatch_target=dispatch_target,
                    parent_instance_id=_effective_parent(existing_parent_id),
                    dispatch_mode=dispatch_mode,
                )
                await _stamp_instance_id(
                    tmux_pane or existing_row["tmux_pane"],
                    session_id,
                    display_name=existing_row["name"],
                )
                if (
                    tmux_pane
                    and existing_row["tmux_pane"]
                    and tmux_pane != existing_row["tmux_pane"]
                ):
                    await _unstamp_instance_id(existing_row["tmux_pane"], session_id)
                await _apply_session_doc_instance_name(
                    db,
                    instance_id=session_id,
                    session_doc_id=existing_row["session_doc_id"],
                    wrapper_launch_id=wrapper_launch_id or existing_row["wrapper_launch_id"],
                )
                await db.commit()
                auto_subscription = await _auto_subscribe_parent_on_start(
                    db,
                    child_instance_id=session_id,
                    child_pane=tmux_pane or existing_row["tmux_pane"],
                    parent_instance_id=_effective_parent(existing_parent_id),
                )
                if auto_subscription:
                    await db.commit()
                mechanicus_subscription = await _reconcile_mechanicus_on_session_start(
                    db, session_id
                )
                if mechanicus_subscription and (
                    mechanicus_subscription.get("created")
                    or mechanicus_subscription.get("existing")
                ):
                    await db.commit()
                await _apply_commander_binding(
                    db,
                    instance_id=session_id,
                    dispatch_target=dispatch_target,
                    parent_instance_id=_effective_parent(existing_parent_id),
                    dispatch_mode=dispatch_mode,
                )
                await db.commit()
                try:
                    target_pane = tmux_pane or existing_row["tmux_pane"]
                    if (
                        tmux_pane
                        and existing_row["tmux_pane"]
                        and tmux_pane != existing_row["tmux_pane"]
                    ):
                        await asyncio.to_thread(
                            shared.clear_pane_tint,
                            existing_row["tmux_pane"],
                            source="reregister-vacate",
                        )
                    if target_pane:
                        await shared.apply_instance_pane_tint(
                            db, session_id, target_pane, source="reregister"
                        )
                except Exception as exc:
                    logger.warning(
                        "Hook: SessionStart reregister tint repaint failed for %s: %s",
                        session_id[:12],
                        exc,
                    )
                await log_event(
                    "instance_reregistered",
                    instance_id=session_id,
                    device_id=device_id,
                    details={
                        "source": "hook",
                        "engine": engine or existing_row["engine"],
                        "was_status": existing_row["status"],
                    },
                )
                return {
                    "success": True,
                    "action": "reregistered",
                    "instance_id": session_id,
                    "stop_subscription": auto_subscription,
                    "mechanicus_stop_subscription": mechanicus_subscription,
                }

        if supplant_id:
            # Fetch the old instance to preserve its config
            cursor = await db.execute("SELECT * FROM instances WHERE id = ?", (supplant_id,))
            old_inst = await cursor.fetchone()
            old_parent_id = (
                old_inst["commander_id"]
                if old_inst is not None and old_inst["commander_type"] == "chapter"
                else None
            )

            if old_inst:
                now = datetime.now().isoformat()
                resolved_session_doc_id = None
                resolved_session_doc_policy = None
                if (
                    dispatch_session_doc_path
                    or primarch_name
                    or dispatch_legion
                    or origin_type == "cron"
                ):
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
                        legion=dispatch_legion or None,
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

                # Update the old row with new session identity, preserve config.
                # pid/session_id died with legacy instance table; the commander edge
                # (legacy parent_instance_id) is applied by
                # _apply_commander_binding below.
                supplant_updates = {
                    "id": session_id,
                    "working_dir": working_dir,
                    "status": "idle",
                    "device_id": device_id,
                    "created_at": now,
                    "last_activity": now,
                    "stopped_at": None,
                    "victory_at": None,
                    "victory_reason": None,
                    "input_lock": None,
                    # A supplanted session is never mid-modal — reconcile any
                    # stuck planning_state. No-op for rows already at `none`.
                    "planning_state": "none",
                    "planning_updated_at": now,
                    "planning_source": "auto-clear:instance-supplanted",
                    "session_doc_id": resolved_session_doc_id or old_inst["session_doc_id"],
                    "wrapper_launch_id": wrapper_launch_id or old_inst["wrapper_launch_id"],
                    "launcher": launcher or old_inst["launcher"],
                    "engine": engine or old_inst["engine"],
                    "dispatch_mode": dispatch_mode or old_inst["dispatch_mode"],
                    "dispatch_session_doc_path": dispatch_session_doc_path
                    or old_inst["dispatch_session_doc_path"],
                    "target_working_dir": target_working_dir or old_inst["target_working_dir"],
                    "launch_mode": launch_mode or old_inst["launch_mode"],
                    "transplant_expected": 1 if transplant_expected else 0,
                    "zealotry": launch_zealotry
                    if launch_zealotry is not None
                    else old_inst["zealotry"],
                    "session_doc_policy": session_doc_policy or old_inst["session_doc_policy"],
                }
                if persona_identity:
                    # Persona singletons stay Emperor-commanded: a supplanted prior
                    # row may carry a poisoned chapter edge — clear it here, the
                    # commander binding below no-ops on an empty parent.
                    supplant_updates["commander_type"] = "emperor"
                    supplant_updates["commander_id"] = None
                    if persona_default_rank:
                        supplant_updates["rank"] = persona_default_rank
                if launch_persona_id is not None:
                    supplant_updates["persona_id"] = launch_persona_id
                if launch_instance_type:
                    supplant_updates["golden_throne"] = await _launch_golden_throne_marker(
                        db,
                        launch_instance_type,
                        zealotry=launch_zealotry,
                        existing_marker=old_inst["golden_throne"],
                    )
                await sanctioned_update_instance(
                    db,
                    instance_id=supplant_id,
                    updates=supplant_updates,
                    mutation_type="instance_updated",
                    write_source="hooks",
                    actor="SessionStart",
                    wrapper_launch_id=wrapper_launch_id or old_inst["wrapper_launch_id"],
                    where_clause="id = ?",
                    where_params=(supplant_id,),
                )
                await _persist_runtime_fields(
                    db,
                    instance_id=session_id,
                    tmux_pane=tmux_pane,
                    pane_label=pane_label or old_inst["pane_label"],
                    dispatch_target=dispatch_target,
                    dispatch_window=dispatch_window,
                    dispatch_slot=dispatch_slot,
                )
                await _apply_commander_binding(
                    db,
                    instance_id=session_id,
                    dispatch_target=dispatch_target,
                    parent_instance_id=_effective_parent(old_parent_id),
                    dispatch_mode=dispatch_mode,
                )
                await _stamp_instance_id(tmux_pane, session_id, display_name=old_inst["name"])

                await _apply_commander_binding(
                    db,
                    instance_id=session_id,
                    dispatch_target=dispatch_target,
                    parent_instance_id=_effective_parent(old_parent_id),
                    dispatch_mode=dispatch_mode,
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
                await _apply_session_doc_instance_name(
                    db,
                    instance_id=session_id,
                    session_doc_id=session_doc_id,
                    wrapper_launch_id=wrapper_launch_id or old_inst["wrapper_launch_id"],
                )
                dispatch_bound_doc = (
                    session_doc_policy or old_inst["session_doc_policy"]
                ) == "dispatch_explicit"

                await db.commit()
                auto_subscription = await _auto_subscribe_parent_on_start(
                    db,
                    child_instance_id=session_id,
                    child_pane=tmux_pane,
                    parent_instance_id=_effective_parent(old_parent_id),
                )
                if auto_subscription:
                    await db.commit()
                mechanicus_subscription = await _reconcile_mechanicus_on_session_start(
                    db, session_id
                )
                if mechanicus_subscription and (
                    mechanicus_subscription.get("created")
                    or mechanicus_subscription.get("existing")
                ):
                    await db.commit()

                # Event-driven tint: the persona moved panes. Clear the vacated
                # pane and paint the new one from canonical instances.persona_id
                # → personas.pane_tint (no recolor queue).
                # Tint is cosmetic — best-effort, never fail registration on it.
                try:
                    if old_tmux_pane and old_tmux_pane != tmux_pane:
                        await asyncio.to_thread(
                            shared.clear_pane_tint, old_tmux_pane, source="supplant-vacate"
                        )
                    if tmux_pane:
                        await shared.apply_instance_pane_tint(
                            db, session_id, tmux_pane, source="supplant"
                        )
                except Exception as exc:
                    logger.warning(
                        "Hook: SessionStart supplant tint repaint failed for %s: %s",
                        session_id[:12],
                        exc,
                    )
                await db.commit()

                # profiles are persona-keyed; legacy profile_name died into persona_id
                cursor = await db.execute(
                    "SELECT slug FROM personas WHERE id = ?", (old_inst["persona_id"],)
                )
                slug_row = await cursor.fetchone()
                preserved_profile = slug_row["slug"] if slug_row else None
                prof = profile_by_name(preserved_profile)
                hex_color = (prof.get("chip_color") or prof.get("color")) if prof else "#666666"
                pane_tint = prof.get("pane_tint") if prof else None

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
                        "tab_name": old_inst["name"],
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
                    "chip_color": hex_color,
                    "pane_tint": pane_tint,
                    "session_doc_id": session_doc_id,
                    "stop_subscription": auto_subscription,
                    "mechanicus_stop_subscription": mechanicus_subscription,
                }

        # --- Normal registration (no supplant) ---

        # Skip TTS profile assignment for subagents (headless, no voice needed)
        if is_subagent:
            profile = {"name": None, "wsl_voice": None, "notification_sound": None}
            pool_exhausted = False
        else:
            persona, pool_exhausted = await assign_astartes_persona(db)
            profile = persona_to_profile(persona)

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
            """SELECT discord_hosted, discord_channel,
                      (SELECT slug FROM personas WHERE id = instances.persona_id) AS legion,
                      wrapper_launch_id,
                      launcher, engine, dispatch_target, dispatch_window,
                      dispatch_mode, dispatch_slot, dispatch_session_doc_path,
                      target_working_dir, launch_mode, transplant_expected,
                      session_doc_policy, session_doc_id, workflow_state,
                      CASE WHEN commander_type = 'chapter' THEN commander_id END
                          AS parent_instance_id
               FROM instances WHERE id = ?""",
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
        parent_instance_id = _effective_parent(_prior_parent_instance_id)
        if not transplant_expected:
            transplant_expected = bool(_prior_dispatch.get("transplant_expected"))
        session_doc_policy = _prior_session_doc_policy

        # Dispatch → worker classification (hook_driven column, distinct from the
        # legacy instance_type='hook_driven' enum). A worker dispatched by a
        # non-Custodes agent (e.g. Fabricator General) is driven autonomously →
        # hook_driven=1; a Custodes-dispatched worker is Emperor-proxied → 0; a
        # direct-Emperor launch has no agent parent → 0. parent_instance_id is
        # the resolved dispatcher. Cleared on the worker's first Stop.
        launch_hook_driven = 0
        if parent_instance_id and not is_subagent:
            cursor = await db.execute(
                """SELECT (SELECT slug FROM personas WHERE id = i.persona_id)
                   FROM instances i WHERE i.id = ? LIMIT 1""",
                (parent_instance_id,),
            )
            _parent_row = await cursor.fetchone()
            if _parent_row and (_parent_row[0] or "").lower() != "custodes":
                launch_hook_driven = 1

        # Insert instance. session_id/source_ip/pid died with legacy instance table
        # (the instance id IS the session uuid); legacy-shaped keys below
        # (tab_name/legion/primarch/parent_instance_id/registered_at) are
        # normalized by sanctioned_insert_instance.
        now = datetime.now().isoformat()
        launch_marker = await _launch_golden_throne_marker(
            db, launch_instance_type, zealotry=launch_zealotry
        )
        if persona_synced:
            launch_marker = launch_marker or "sync"
        await sanctioned_insert_instance(
            db,
            values={
                "id": session_id,
                "tab_name": tab_name,
                "working_dir": working_dir,
                "origin_type": origin_type,
                "device_id": device_id,
                "profile_name": profile["name"],
                "tts_voice": profile["wsl_voice"],
                "notification_sound": profile["notification_sound"],
                "status": "idle",
                "legion": _prior_legion or "astartes",
                "golden_throne": launch_marker,
                "input_lock": None,
                "is_subagent": is_subagent,
                "primarch": primarch_name or None,
                "wrapper_launch_id": wrapper_launch_id,
                "launcher": launcher,
                "engine": engine,
                "dispatch_mode": dispatch_mode,
                "dispatch_session_doc_path": dispatch_session_doc_path,
                "target_working_dir": target_working_dir,
                "launch_mode": launch_mode,
                "parent_instance_id": parent_instance_id,
                "transplant_expected": 1 if transplant_expected else 0,
                "hook_driven": launch_hook_driven,
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
        await _persist_runtime_fields(
            db,
            instance_id=session_id,
            tmux_pane=tmux_pane,
            pane_label=pane_label,
            dispatch_target=dispatch_target,
            dispatch_window=dispatch_window,
            dispatch_slot=dispatch_slot,
        )
        await _apply_commander_binding(
            db,
            instance_id=session_id,
            dispatch_target=dispatch_target,
            parent_instance_id=parent_instance_id,
            dispatch_mode=dispatch_mode,
        )
        await _stamp_instance_id(tmux_pane, session_id, display_name=tab_name)
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
            legion=dispatch_legion or None,
        )
        session_doc_policy = resolved_session_doc_policy or session_doc_policy
        # Automated launch that couldn't resolve a doc: leave session_doc_id NULL
        # (no placeholder minted) and surface the miss for the orchestrator.
        if session_doc_id is None and resolved_session_doc_policy == "unresolved_dispatch":
            await log_event(
                "session_doc_unresolved",
                instance_id=session_id,
                device_id=device_id,
                details={
                    "launcher": launcher,
                    "legion": dispatch_legion,
                    "primarch": primarch_name,
                    "origin_type": origin_type,
                    "dispatch_session_doc_path": dispatch_session_doc_path,
                },
            )
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
        tab_name = (
            await _apply_session_doc_instance_name(
                db,
                instance_id=session_id,
                session_doc_id=session_doc_id,
                wrapper_launch_id=wrapper_launch_id,
            )
            or tab_name
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
        elif persona_identity:
            # Fresh persona singleton: the INSERT wrote _prior_legion or "astartes",
            # never dispatch_legion — so write the pane's canonical legion here.
            auto_legion = persona_identity["legion"]

        # Restore prior legion if no auto-detect, or apply auto-detect. The legacy
        # legion column died into persona_id: resolve the legion name to a persona
        # slug (LEGACY_PERSONA_ALIASES) and bind/clear persona_id accordingly.
        if auto_legion:
            legion_updates = {}
            if auto_legion == "civic":
                # Civic/Pax launches have no persona tint authority: clear the
                # persona assignment so tint resolution falls through to tmux
                # default instead of an old civic-green or arbitrary chapter colour.
                legion_updates["persona_id"] = None
            else:
                # Persona panes bind persona_id by primarch, not legion: a seat in
                # a shared legion (Malcador in astartes, Administratum in
                # mechanicus) is invisible to a legion-keyed lookup, leaving the
                # random chapter drawn at INSERT as the bound persona.
                persona_source = auto_legion
                if persona_identity and persona_identity.get("primarch"):
                    persona_source = persona_identity["primarch"]
                auto_slug = LEGACY_PERSONA_ALIASES.get(
                    persona_source.strip().lower(), persona_source.strip().lower()
                )
                cursor = await db.execute("SELECT id FROM personas WHERE slug = ?", (auto_slug,))
                auto_persona_row = await cursor.fetchone()
                if auto_persona_row:
                    legion_updates["persona_id"] = auto_persona_row["id"]
            # Persona panes (Custodes, FG, Administratum, …) are recognised by their
            # tmux label. The moment one is, fold in its persona profile, overriding
            # whatever random chapter it drew at registration: Custodes gets the
            # reserved George voice (the only path to it — George lives outside the
            # rotation pools); every other persona is voiceless (tts_voice=None) so
            # it never TTSes and frees the chapter voice it briefly held. All persona
            # voiceless personas keep tts_voice=NULL (silent). Pane colour is applied
            # only through tmux select-pane -P bg=<personas.pane_tint>.
            if persona_identity:
                # Persona panes are identified by their label's primarch, not their
                # legion. Malcador shares the astartes legion with regiment workers,
                # so the legion→persona_id bind above cannot resolve it (and at
                # insert time profile_name still carries the random chapter the row
                # drew, which slug_from_legacy prefers over primarch). Bind the
                # canonical persona_id straight from the label's primarch so the
                # default-rank stamp trigger and singleton guard recognise the row.
                if launch_persona_id is not None:
                    legion_updates["persona_id"] = launch_persona_id
                persona_profile = resolve_persona_profile(
                    persona_identity.get("primarch"), auto_legion
                )
                legion_updates.update(
                    {
                        "tts_voice": persona_profile["wsl_voice"],
                        "notification_sound": persona_profile["notification_sound"],
                    }
                )
                # Rebind the local profile so the SessionStart response carries
                # persona display/chip/tint data. No Claude slash-color command is emitted.
                profile = persona_profile
            if legion_updates:
                await sanctioned_update_instance(
                    db,
                    instance_id=session_id,
                    updates=legion_updates,
                    mutation_type="instance_updated",
                    write_source="hooks",
                    actor="SessionStart",
                    wrapper_launch_id=wrapper_launch_id,
                )
        elif _prior_legion and _prior_legion != "astartes":
            prior_slug = LEGACY_PERSONA_ALIASES.get(
                _prior_legion.strip().lower(), _prior_legion.strip().lower()
            )
            cursor = await db.execute("SELECT id FROM personas WHERE slug = ?", (prior_slug,))
            prior_persona_row = await cursor.fetchone()
            if prior_persona_row:
                await sanctioned_update_instance(
                    db,
                    instance_id=session_id,
                    updates={"persona_id": prior_persona_row["id"]},
                    mutation_type="instance_updated",
                    write_source="hooks",
                    actor="SessionStart",
                    wrapper_launch_id=wrapper_launch_id,
                )

        await _apply_commander_binding(
            db,
            instance_id=session_id,
            dispatch_target=dispatch_target,
            parent_instance_id=parent_instance_id,
            dispatch_mode=dispatch_mode,
        )
        await db.commit()

        # Update frontmatter if we linked a session doc
        if session_doc_id:
            await _update_doc_agents_list(db, session_doc_id)

            # Populate start_time in session doc frontmatter
            cursor = await db.execute(
                "SELECT file_path FROM session_documents WHERE id = ?", (session_doc_id,)
            )
            doc_row = await cursor.fetchone()
            if doc_row and doc_row[0]:
                fp = Path(doc_row[0])
                if fp.exists():
                    start_time = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
                    fm_updates = {"start_time": start_time}
                    if auto_legion:
                        fm_updates["legion"] = auto_legion
                    if primarch_name:
                        fm_updates["primarch"] = primarch_name
                    await asyncio.to_thread(update_frontmatter, fp, fm_updates)

        auto_subscription = await _auto_subscribe_parent_on_start(
            db,
            child_instance_id=session_id,
            child_pane=tmux_pane,
            parent_instance_id=parent_instance_id,
        )
        if auto_subscription:
            await db.commit()
        mechanicus_subscription = await _reconcile_mechanicus_on_session_start(db, session_id)
        if mechanicus_subscription and (
            mechanicus_subscription.get("created") or mechanicus_subscription.get("existing")
        ):
            await db.commit()

        try:
            if tmux_pane:
                await shared.apply_instance_pane_tint(
                    db, session_id, tmux_pane, source="SessionStart"
                )
        except Exception as exc:
            logger.warning(
                "Hook: SessionStart tint repaint failed for %s: %s",
                session_id[:12],
                exc,
            )

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
        "chip_color": profile.get("chip_color") if not is_subagent else None,
        "pane_tint": profile.get("pane_tint") if not is_subagent else None,
        "session_doc_id": session_doc_id,
        "stop_subscription": auto_subscription,
        "mechanicus_stop_subscription": mechanicus_subscription,
    }


async def handle_session_end(payload: dict) -> dict:
    """Handle SessionEnd hook - deregister Claude instance."""
    session_id = payload.get("session_id") or payload.get("conversation_id")
    if not session_id:
        return {"success": False, "action": "no_session_id"}

    # Claude Code forwards the SessionEnd `reason` (clear / compact / logout /
    # prompt_input_exit / ...) via generic-hook.sh. A non-terminal boundary is
    # the wrapper still alive about to re-fire SessionStart — never tear the row
    # or stamp down for those (see Layer 1 short-circuit below).
    end_reason = _normalize_text(payload.get("reason") or "") or None

    _pending_background_tasks.pop(session_id, None)

    now = datetime.now().isoformat()

    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        cursor = await db.execute(
            """SELECT id, device_id, COALESCE(is_subagent, 0), session_doc_id,
                      tmux_pane,
                      (SELECT slug FROM personas WHERE id = instances.persona_id) AS legion,
                      workflow_state, pane_label, golden_throne, status, rank
               FROM instances WHERE id = ?""",
            (session_id,),
        )
        row = await cursor.fetchone()

        if not row:
            fallback_pane = _normalize_text(
                payload.get("pane_label")
                or payload.get("tmux_pane")
                or payload.get("env", {}).get("TMUX_PANE", "")
            )
            _spawn_session_end_assertion(fallback_pane, session_id)
            return {"success": False, "action": "not_found", "instance_id": session_id}

        is_subagent = row[2]
        session_doc_id = row[3]
        _stop_pane = row[4]
        _prior_workflow_state = row[6]
        _stop_pane_label = row[7]
        _gt_marker = row[8]
        _existing_status = row[9]
        _existing_rank = row[10]

        # Layer 1 — non-terminal SessionEnd short-circuit (in-wrapper re-fire).
        # plan-accept / `/clear` / compaction fire SessionEnd→SessionStart inside
        # the SAME wrapper. The default teardown below marks the row `stopped` and
        # spawns _spawn_session_end_assertion, which (registry row now stopped →
        # assert ok=False) unsets @INSTANCE_ID — destroying the continuity stamp
        # the paired SessionStart needs to re-key, so it mints a new id + orphan
        # doc. The wrapper is still alive: leave the row + stamp intact and let the
        # re-fire adopt it (clean stamp path; Layer 2 is the backstop).
        if end_reason in NON_TERMINAL_SESSION_END_REASONS:
            await log_event(
                "instance_session_end_skipped",
                instance_id=session_id,
                device_id=row[1],
                details={
                    "reason": end_reason,
                    "non_terminal": True,
                    "source": "hook",
                },
            )
            logger.info(
                "Hook: SessionEnd non-terminal (%s) for %s — preserving row + stamp",
                end_reason,
                session_id[:12],
            )
            return {
                "success": True,
                "action": "non_terminal_end",
                "instance_id": session_id,
                "reason": end_reason,
            }

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
        session_end_updates = {
            "input_lock": None,
            "stopped_at": now,
            "hook_driven": 0,
            "workflow_state": "closed",
            "workflow_updated_at": now,
            "workflow_blocked_reason": None,
            "stop_allowed": 1,
            "next_required_action": None,
            "next_action_owner": None,
        }
        if _existing_status != "archived":
            session_end_updates["status"] = "stopped"
        # Legacy `synced=0` cleared the morning-session sync flag. Its durable home is the
        # golden_throne marker: clear it ONLY when it is the 'sync' sentinel — a real
        # golden_throne.id binding survives the session ending.
        if _gt_marker == "sync":
            session_end_updates["golden_throne"] = None
        await sanctioned_update_instance(
            db,
            instance_id=session_id,
            updates=session_end_updates,
            mutation_type="instance_stopped",
            write_source="hooks",
            actor="SessionEnd",
            wrapper_launch_id=payload.get("wrapper_launch_id"),
            workflow_events=workflow_events,
        )

        await db.commit()

        try:
            if _stop_pane:
                await asyncio.to_thread(shared.clear_pane_tint, _stop_pane, source="SessionEnd")
        except Exception as exc:
            logger.warning(
                "Hook: SessionEnd tint clear failed for %s: %s",
                session_id[:12],
                exc,
            )

        # Close-time pane cleanup is centralized through tmuxctl assertion:
        # persona panes self-heal/recolor, dead stack workers prune, and failed
        # assertions clear stale overlays. Spawn bounded work out-of-band so the
        # hook response is not held hostage by a relaunch.
        _spawn_session_end_assertion(_stop_pane_label or _stop_pane, session_id)

        # Check remaining active instances
        cursor = await db.execute(
            "SELECT COUNT(*) FROM instances WHERE status NOT IN ('stopped', 'archived')"
        )
        count_row = await cursor.fetchone()
        remaining_active = count_row[0] if count_row else 0

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


def _payload_starts_slash_plan(payload: dict) -> bool:
    """Return true when a prompt-submit payload contains a direct /plan command."""
    prompt_keys = (
        "prompt",
        "user_prompt",
        "userPrompt",
        "message",
        "text",
        "input",
        "command",
    )
    for key in prompt_keys:
        value = payload.get(key)
        if isinstance(value, str) and value.lstrip().startswith("/plan"):
            return True

    for parent_key in ("payload", "event", "data", "turn", "turn_context", "context"):
        parent = payload.get(parent_key)
        if isinstance(parent, dict) and _payload_starts_slash_plan(parent):
            return True
    return False


def _payload_indicates_plan_mode(payload: dict) -> bool:
    """Detect native Codex plan-mode prompt context without screen scraping."""

    def walk(obj: Any) -> bool:
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_norm = str(key).lower().replace("-", "_")
                if key_norm in {"plan_mode", "planning_mode", "is_plan_mode"}:
                    if value is True:
                        return True
                    if isinstance(value, str) and value.lower() in {"1", "true", "yes", "plan"}:
                        return True
                if key_norm in {"mode", "phase", "reasoning_mode", "turn_mode"}:
                    if isinstance(value, str) and value.lower().replace("-", "_") in {
                        "plan",
                        "planning",
                        "plan_mode",
                    }:
                        return True
                if key_norm in {"item", "message", "event"} and isinstance(value, dict):
                    item_type = str(value.get("type") or "").lower()
                    if item_type == "plan":
                        return True
                if walk(value):
                    return True
        elif isinstance(obj, list):
            return any(walk(item) for item in obj)
        return False

    return walk(payload)


def _transcript_indicates_plan_mode_sync(payload: dict) -> bool:
    """Detect Codex native Plan Mode from the JSONL transcript turn context.

    Codex's UserPromptSubmit hook payload may contain only the rendered prompt
    text.  When the user entered native Plan Mode, the `/plan` prefix is
    consumed by the TUI before the hook sees it, but the transcript records the
    current turn as plan-mode before the hook is forwarded:

      * event_msg.payload.type == "task_started" with
        collaboration_mode_kind == "plan"
      * turn_context.payload.collaboration_mode.mode == "plan"

    Key on the hook's turn_id so an older plan-mode turn in a resumed transcript
    cannot arm planning for an ordinary later prompt.
    """
    turn_id = _normalize_text(payload.get("turn_id"))
    transcript_path = _normalize_text(payload.get("transcript_path"))
    if not turn_id or not transcript_path:
        return False

    try:
        path = Path(transcript_path).expanduser()
        if not path.exists() or not path.is_file():
            return False
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False

    seen_matching_turn = False
    for line in reversed(lines):
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        record_type = record.get("type")
        record_payload = record.get("payload")
        if not isinstance(record_payload, dict):
            continue

        payload_turn_id = _normalize_text(record_payload.get("turn_id"))
        if payload_turn_id:
            if payload_turn_id != turn_id:
                # Ignore newer turns until the target is found.  Once we have
                # seen the target turn, a different turn_id means we crossed
                # into an older turn and no turn-less records beyond this point
                # can belong to the target turn.
                if seen_matching_turn:
                    break
                continue
            seen_matching_turn = True

        if record_type == "event_msg" and record_payload.get("type") == "task_started":
            if _normalize_text(record_payload.get("collaboration_mode_kind")) == "plan":
                return True
            if payload_turn_id == turn_id:
                break
            continue

        if record_type == "turn_context" and payload_turn_id == turn_id:
            if _payload_indicates_plan_mode(record_payload):
                return True

        if record_type == "event_msg" and record_payload.get("type") == "item_completed":
            # Late but useful for re-fired PromptSubmit hooks: a completed Plan
            # item for this turn proves the pane is still in native Plan Mode.
            item = record_payload.get("item")
            item_type = _normalize_text(item.get("type")) if isinstance(item, dict) else None
            if (
                item_type
                and item_type.lower() == "plan"
                and (payload_turn_id == turn_id or (not payload_turn_id and seen_matching_turn))
            ):
                return True
    return False


async def _transcript_indicates_plan_mode(payload: dict) -> bool:
    """Async wrapper so transcript disk I/O never blocks the hook event loop."""
    return await asyncio.to_thread(_transcript_indicates_plan_mode_sync, payload)


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
    await log_event(
        "hook_user_prompt_submit",
        instance_id=session_id,
        details={
            "hook_event_name": "UserPromptSubmit",
            "payload_keys": sorted(payload.keys()),
            "prompt_hash": payload.get("prompt_hash") or payload.get("payload_hash"),
        },
    )
    consumed_injections: list[dict] = []

    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM instances WHERE id = ?", (session_id,))
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

        planning_event = None
        if (
            _payload_starts_slash_plan(payload)
            or _payload_indicates_plan_mode(payload)
            or await _transcript_indicates_plan_mode(payload)
        ):
            planning_event = await _set_planning_state(
                db,
                session_id,
                "planning",
                source="auto-clear:prompt-submit",
                only_if_in=("none", "preplanning", "planning"),
                actor="PromptSubmit",
            )

        # Also resurrect stopped instances - activity means they're active.
        # (pid died with legacy instance table; nothing to backfill.)
        await sanctioned_update_instance(
            db,
            instance_id=session_id,
            updates={
                "status": "working",
                "last_activity": now,
                "stopped_at": None,
            },
            mutation_type="status_changed",
            write_source="hooks",
            actor="PromptSubmit",
        )
        await db.commit()

    if planning_event:
        await log_event("planning_state_changed", instance_id=session_id, details=planning_event)

    # NOTE: this hook no longer flips global productivity. Productivity is now a
    # read-time calculus in compute_work_state (the 10s poll), which discounts
    # hook_driven / automated-marker panes. An automated wake that triggers a fresh
    # PromptSubmit therefore can no longer reset the idle clock. The work_action
    # callback still fires (resolves expected-acks, busts quiet-state) but, for this
    # non-explicit source, no longer sets global productivity (see _work_action).
    exited_idle = False
    if _work_action_callback:
        await _work_action_callback(source="prompt_submit", note=f"session_id={session_id}")

    # Golden Throne: cancel any pending follow-up (user is active). A real GT
    # binding is a golden_throne.id marker — i.e. non-null and not the 'sync'
    # sentinel (legacy instance_type='golden_throne').
    golden_throne_activity = None
    _gt_marker = existing_dict.get("golden_throne")
    if _gt_marker and _gt_marker != "sync" and _golden_throne_activity_callback:
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

    # Plan approved → clear planning_state. Mutating tools are blocked in Claude
    # plan mode, so the first Write/Edit after approval is a poll-free, race-proof
    # "planning ended" signal — it owns the planning→none transition, replacing the
    # screen-scrape watcher's 10s-timeout race. Done before the debounce (like the
    # AskUserQuestion cancel above) because the debounce early-returns before
    # instance resolution, so a prior tool's debounce window would otherwise
    # swallow the approval edit. The only_if_in gate excludes `preplanning` (a
    # /preplan session-doc edit must not false-clear) and `none` (ordinary edits
    # no-op); `tool_name in MUTATING_TOOLS` short-circuits first so the common
    # non-mutating path adds zero cost.
    if tool_name in MUTATING_TOOLS:
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            db.row_factory = aiosqlite.Row
            ev = await _set_planning_state(
                db,
                session_id,
                "none",
                source="auto-clear:tool-exec",
                only_if_in=("planning", "approving"),
            )
            await db.commit()
        if ev:
            await log_event("planning_state_changed", instance_id=session_id, details=ev)

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
                "status": "working",
                "last_activity": now,
                "stopped_at": None,
            },
            mutation_type="status_changed",
            write_source="hooks",
            actor="PostToolUse",
        )
        await db.commit()

    # NOTE: no longer flips global productivity (read-time calculus owns it now —
    # see handle_prompt_submit / compute_work_state). The status/last_activity
    # heartbeat above is unchanged so stopped instances still resurrect and a
    # missed PromptSubmit is still caught. Liveness is preserved; only the
    # productivity flip is removed.
    if tool_name == "AskUserQuestion" and _work_action_callback:
        await _work_action_callback(
            source="ask_user_question_answered", note=f"session_id={session_id}"
        )

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
        cursor = await db.execute("SELECT * FROM instances WHERE id = ?", (session_id,))
        instance = await cursor.fetchone()

        # Is this session the live Custodes orchestrator by persona identity (NOT
        # sync mode)? The morning keepalive is gated on Custodes identity + an active
        # morning session, so resolve identity from the canonical instances/personas
        # join rather than instance_type. A superseded (retired) custodes is excluded
        # (it should stop cleanly, not keepalive), and so are custodes chapter
        # children (subagents share the persona_id but are not the orchestrator).
        cursor = await db.execute(
            """SELECT 1 FROM instances i JOIN personas p ON p.id = i.persona_id
               WHERE i.id = ? AND p.slug = 'custodes'
                 AND i.rank != 'retired' AND i.commander_type != 'chapter'""",
            (session_id,),
        )
        is_custodes_persona = await cursor.fetchone() is not None

    if not instance:
        return {"success": False, "action": "instance_not_found"}

    instance = dict(instance)
    device_id = instance.get("device_id", "Mac-Mini")
    tab_name = instance.get("name", "Claude")
    _resolved_surface = human_pane_surface(
        tab_name, instance.get("tmux_pane"), instance.get("pane_label")
    )
    notify_surface = _resolved_surface if _resolved_surface != "session" else session_id[:12]
    notification_sound = instance.get("notification_sound", "chimes.wav")

    # Update last_activity but DON'T set idle yet — that's the evaluators' job.
    # Sync instances never go idle (permanent processing until SessionEnd).
    # Golden throne / one-off: evaluators write idle on pass, or stay processing on nudge.
    now = datetime.now().isoformat()
    # Legacy instance_type derived from the golden_throne marker (its durable home):
    # 'sync' marker → sync; any other non-null marker (a golden_throne.id) →
    # golden_throne; NULL → one_off.
    _gt_marker = instance.get("golden_throne")
    if _gt_marker == "sync":
        instance_type = "sync"
    elif _gt_marker:
        instance_type = "golden_throne"
    else:
        instance_type = instance.get("instance_type", "one_off")
    # The Custodes persona never auto-idles and owns the morning keepalive path,
    # regardless of whether sync MODE is currently set. `sync` remains a valid
    # mode signal, so the gate is "Custodes persona OR still in sync mode".
    #
    # Liveness: a retired (dead identity) or already stopped/archived seat must
    # NEVER keepalive off a residual sync marker — re-injecting into its stale pane
    # is exactly the GT phantom-dispatch this filter stops. The is_custodes_persona
    # arm already excludes retired (its join filters rank), so the gate is only on
    # the sync-MODE OR-branch. Custodes identity, when live, still wins.
    _seat_is_dead = (instance.get("rank") or "") == "retired" or instance.get("status") in (
        "stopped",
        "archived",
    )
    is_sync_instance = is_custodes_persona or (instance_type == "sync" and not _seat_is_dead)
    is_subagent_instance_quick = bool(instance.get("is_subagent"))
    has_pending_background = _pending_background_tasks.get(session_id, 0) > 0

    # Determine if evaluators will run (they own the idle transition)
    will_evaluate = (
        not is_subagent_instance_quick and not has_pending_background and not is_sync_instance
    )

    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        # Stop clears hook_driven=0 — the autonomous wake that flagged this instance
        # has run its course. Idempotent (clearing an already-0 row is a no-op), so
        # this is a safe generic clear on every Stop regardless of how it was woken.
        if will_evaluate or is_sync_instance:
            await sanctioned_update_instance(
                db,
                instance_id=session_id,
                updates={"last_activity": now, "hook_driven": 0},
                mutation_type="instance_updated",
                write_source="hooks",
                actor="Stop",
            )
        else:
            await sanctioned_update_instance(
                db,
                instance_id=session_id,
                updates={"status": "idle", "last_activity": now, "hook_driven": 0},
                mutation_type="status_changed",
                write_source="hooks",
                actor="Stop",
            )
        await db.commit()

    # Interview unnamed panes on Stop. Engine-agnostic — this is the only path
    # Codex panes get (no naming-nudge.sh shim), and it is harmlessly idempotent
    # for Claude, which also fires naming-nudge.sh. The core self-guards on
    # placeholder name, missing pane, pending nudge, and the 3-nudge cap, so a
    # named pane is a cheap no-op. Fire-and-forget so Stop latency is unaffected;
    # the wrapper logs (never raises) so an unforeseen error can't strand the task.
    async def _safe_naming_nudge() -> None:
        try:
            await _require_dep("maybe_naming_nudge", _maybe_naming_nudge)(session_id)
        except Exception as exc:  # noqa: BLE001 — best-effort nudge, must not break Stop
            logger.warning("Stop: naming nudge failed for %s: %s", session_id[:12], exc)

    asyncio.create_task(_safe_naming_nudge())

    # Trinity Chunk 1: resolve any open `talk` pairs awaiting natural-stop
    # slash-copy of this target's final response. Fires for every Stop hook —
    # the turn-flip end is the right signal regardless of sync/one-off status.
    try:
        target_pane = instance.get("tmux_pane") or ""
        if target_pane:
            resolved_talks = await talk_service.fire_slash_copy_for_pane(
                target_pane,
                transcript_path=payload.get("transcript_path"),
            )
            if resolved_talks:
                logger.info(
                    "talk: slash-copied %d pair(s) for %s on Stop hook",
                    len(resolved_talks),
                    target_pane,
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("talk: slash-copy hook failed for %s: %s", session_id[:12], exc)

    # Fire async stop evaluators (action_validator, plan_auditor, etc.)
    # Skips subagents, sync instances, and intermediate stops.
    if will_evaluate:
        session_doc_id = instance.get("session_doc_id")
        stop_context = (
            payload.get("transcript_tail", "")[:4000] if payload.get("transcript_tail") else ""
        )
        # Signal TUI that evaluators are running for this instance
        _tui_signal_dir = Path.home() / ".claude" / "tui-signals"
        _tui_signal_dir.mkdir(parents=True, exist_ok=True)
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

    pid = payload.get("pid")
    is_subagent_instance = bool(instance.get("is_subagent")) or bool(pid and is_subagent_pid(pid))

    # ── Golden Throne timer arm ──
    # StopValidate may block once for self-eval, but the async Stop hook owns
    # durable persistence after the model actually goes quiet.
    if instance_type == "golden_throne":
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            await sanctioned_update_instance(
                db,
                instance_id=session_id,
                updates={"status": "idle", "last_activity": now, "hook_driven": 0},
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
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = data.get("message", {})
            if message.get("role") != "assistant" and data.get("role") != "assistant":
                continue
            content = message.get("content")
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

    if not tts_text:
        try:
            tts_text = await talk_service.slash_copy_target(
                {
                    "target_instance_id": session_id,
                    "target_working_dir": instance.get("working_dir"),
                    "target_engine": instance.get("engine") or "claude",
                    "target_pane": instance.get("tmux_pane") or "",
                    "payload_sent_at": 0,
                },
                transcript_path=transcript_path,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Hook: Stop final-response fallback failed for %s: %s", session_id[:12], exc
            )

    # Trinity Chunk 2: live Stop-hook subscriptions. Deliver before legacy
    # state_injections so a subscribed parent gets an immediate prompt instead
    # of waiting until its next PromptSubmit.
    live_stop_deliveries = await _fanout_stop_subscriptions(instance, payload, tts_text)
    if live_stop_deliveries:
        result["stop_subscriptions"] = live_stop_deliveries
    live_stop_sent = any(d.get("status") == "sent" for d in live_stop_deliveries)
    live_stop_handled = live_stop_sent or any(
        d.get("status") == "duplicate" for d in live_stop_deliveries
    )
    if not live_stop_handled:
        child_fanout = await _enqueue_child_stop_fanout(instance, payload)
        if child_fanout:
            result["parent_fanout"] = child_fanout

    # ── Subagent/intermediate/sync detection: after Chunk 2 fanout, skip user
    # notifications for non-user-visible stop events.
    if is_subagent_instance:
        result["action"] = "stop_processed_subagent"
        logger.info(
            f"Hook: Stop {session_id[:12]}... subagent — state updated/fanout processed, skipping notifications"
        )
        return result

    if _pending_background_tasks.get(session_id, 0) > 0:
        result["action"] = "stop_processed_intermediate"
        logger.info(
            f"Hook: Stop {session_id[:12]}... intermediate ({_pending_background_tasks[session_id]} background tasks pending) — skipping notifications"
        )
        return result

    if is_sync_instance:
        # Self-continuing morning session. The keepalive is gated on Custodes
        # persona identity (or residual sync mode) AND an ACTIVE morning session —
        # NOT on identity alone. Being the Custodes is NECESSARY (a normal instance
        # never reaches here) but NOT SUFFICIENT: the resting Custodes singleton
        # also reaches this branch, yet must stop cleanly once the morning session
        # is ended (POST /api/morning/end) or past the Emperor's
        # MORNING_MAX_DURATION_HOURS bound.
        from morning_session import MORNING_MAX_DURATION_HOURS, morning_session_active

        active, morning_reason = morning_session_active()
        await log_event(
            "hook_stop",
            instance_id=session_id,
            details={
                "custodes_persona": is_custodes_persona,
                "instance_type": instance_type,
                "keepalive": active,
                "morning": morning_reason,
            },
        )

        # Resolve the pane once — needed for the keepalive OR the expiry notice.
        tmux_pane = instance.get("tmux_pane")
        if not tmux_pane:
            # Re-fetch from DB in case the cached instance row didn't capture it.
            try:
                async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
                    cursor = await db.execute(
                        "SELECT tmux_pane FROM instances WHERE id = ?",
                        (session_id,),
                    )
                    row = await cursor.fetchone()
                    if row:
                        tmux_pane = row[0]
            except Exception:
                pass

        if not active:
            # No active morning session → clean Stop, no keepalive re-injection.
            # When the 2h bound just tripped, morning_session_active() already
            # auto-ended the state file; emit ONE final in-band notice and then go
            # quiet — do NOT keep re-prompting.
            if morning_reason == "expired":
                result["action"] = "stop_processed_sync_expired"
                if tmux_pane:
                    notice = MORNING_EXPIRY_NOTICE.format(hours=MORNING_MAX_DURATION_HOURS)
                    try:
                        proc = await _run_subprocess_offloop(
                            ("claude-cmd", "--pane", tmux_pane, notice),
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            timeout=10,
                        )
                        if proc.returncode != 0:
                            logger.warning(
                                f"Hook: Stop {session_id[:12]}... morning expiry notice claude-cmd failed: "
                                f"{proc.stderr.decode()[:200]}"
                            )
                    except Exception as e:
                        logger.warning(
                            f"Hook: Stop {session_id[:12]}... morning expiry notice failed: {e}"
                        )
            else:
                result["action"] = f"stop_processed_sync_idle:{morning_reason}"
            return result

        # Active, in-bound morning session → re-inject a fresh timestamped keepalive
        # so the session stays temporally bound, not turn-based.
        result["action"] = "stop_processed_sync"
        now_mst = datetime.now(ZoneInfo("America/Phoenix"))
        keepalive_prompt = MORNING_KEEPALIVE_PROMPT.format(ts=now_mst.strftime("%H:%M"))

        if not tmux_pane:
            logger.warning(f"Hook: Stop {session_id[:12]}... sync keepalive skipped — no tmux_pane")
            return result

        try:
            proc = await _run_subprocess_offloop(
                ("claude-cmd", "--pane", tmux_pane, keepalive_prompt),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                timeout=10,
            )
            if proc.returncode != 0:
                logger.warning(
                    f"Hook: Stop {session_id[:12]}... sync keepalive claude-cmd failed: "
                    f"{proc.stderr.decode()[:200]}"
                )
        except Exception as e:
            logger.warning(f"Hook: Stop {session_id[:12]}... sync keepalive delivery failed: {e}")

        return result

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

    # Host-device delivery: this session is HOSTED on the phone (Token-S24), so
    # its own final TTS belongs on its host, not on the geofence-routed comms
    # bus. This is host delivery, not an Emperor notification — it does not go
    # through dispatch_notify. (See the comms-router invariant guard test.)
    if device_id == "Token-S24":
        notify_params = {
            "banner_text": f"[{notify_surface}] finished",
            "vibe": 30,
        }
        if tts_text:
            notify_params["tts_text"] = tts_text[:300]  # comms-router-allow: phone host delivery
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

    # NOTE: No Pavlok stimulus on Stop. A Stop-hook chime is a notification, not
    # an enforcement event. Pavlok stim delivery is the explicit product of
    # enforce/cascade pathways only (see enforce.py / main.py distraction paths).
    # Mirroring every "claude_finished" Stop to a Pavlok soft buzz turned the
    # watch into a per-Stop buzzer. Ref: regression-pavlok-soft-on-tts-chime-2026-05-24.

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
                    "SELECT tmux_pane FROM instances WHERE id = ?",
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
    keep the native dialog. A golden_throne binding is a golden_throne.id marker —
    non-null and not the 'sync' sentinel (legacy instance_type='golden_throne')."""
    if session_id in VOICE_CHAT_SESSIONS:
        return True
    if not instance_row:
        return False
    marker = instance_row.get("golden_throne")
    return (bool(marker) and marker != "sync") or instance_row.get(
        "instance_type"
    ) == "golden_throne"


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
                updates={"status": "working", "last_activity": now, "stopped_at": None},
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
                "SELECT golden_throne, name AS tab_name, "
                "(SELECT slug FROM personas WHERE id = instances.persona_id) AS legion, "
                "tmux_pane, device_id, tts_voice "
                "FROM instances WHERE id = ?",
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
            "local_exec": f'"/mnt/c/Program Files/AutoHotkey/v2/AutoHotkey.exe" "//Token-NAS/Imperium/runtimes/token-os/live/ahk/voice-send-keys.ahk"{pane_arg} --navigate',
        }

    # Discord-hosted: post AskUserQuestion to Discord channel and notify phone
    _ask_handled_by_discord = False
    if tool_name == "AskUserQuestion" and session_id:
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            cursor = await db.execute(
                "SELECT discord_hosted, discord_channel, "
                "(SELECT slug FROM personas WHERE id = instances.persona_id) AS legion "
                "FROM instances WHERE id = ?",
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
                    # Also notify so Emperor knows to check Discord — through the
                    # comms middleware (spoken part geofence-routed, buzz rides along).
                    asyncio.create_task(
                        dispatch_notify(
                            "Claude is asking a question in Discord.",
                            vibe=40,
                            banner=q_parts[0][:80],
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
                # Through the comms middleware: spoken part geofence-routed,
                # buzz + beep + banner ride along (no callsite-level split).
                asyncio.create_task(
                    dispatch_notify(
                        f"Claude is asking: {q_text}",
                        vibe=40,
                        beep=30,
                        banner=q_text[:80],
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
                "SELECT notification_sound FROM instances WHERE id = ?", (session_id,)
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
            "SELECT id, golden_throne, is_subagent, victory_at, workflow_state, "
            "session_doc_id FROM instances WHERE id = ?",
            (session_id,),
        )
        instance = await cursor.fetchone()

    if not instance:
        return {}  # unknown instance — allow stop

    instance = dict(instance)
    # Legacy instance_type derived from the golden_throne marker: 'sync' → sync;
    # any other non-null marker (a golden_throne.id) → golden_throne; NULL → one_off.
    _gt_marker = instance.get("golden_throne")
    if _gt_marker == "sync":
        instance_type = "sync"
    elif _gt_marker:
        instance_type = "golden_throne"
    else:
        instance_type = "one_off"

    # ── Skip: subagents never get self-eval ──
    if instance.get("is_subagent"):
        return {}

    # ── Skip: victory already declared ──
    if instance.get("victory_at"):
        return {}

    # ── Skip: one-off instances don't need self-eval ──
    if instance_type == "one_off":
        return {}

    # ── Questions gate: session docs with non-closed questions block once ──
    if instance_type in ("golden_throne",) and instance.get("session_doc_id"):
        session_doc_path = None
        async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
            cursor = await db.execute(
                "SELECT file_path FROM session_documents WHERE id = ?",
                (instance.get("session_doc_id"),),
            )
            doc_row = await cursor.fetchone()
            if doc_row and doc_row[0]:
                session_doc_path = doc_row[0]

        if session_doc_path:
            try:
                is_clear, blockers = await asyncio.to_thread(trials_clear, Path(session_doc_path))
            except (FileNotFoundError, ValueError) as exc:
                logger.warning(
                    "StopValidate: questions gate could not read %s for %s: %s",
                    session_doc_path,
                    session_id[:12],
                    exc,
                )
                is_clear, blockers = True, []
            if not is_clear:
                _self_eval_pending[session_id] = now
                blocked_at = datetime.now().isoformat()
                top_blockers = blockers[:5]
                blocker_lines = []
                for b in top_blockers:
                    try:
                        imp = int(b.get("importance") or 0)
                    except (TypeError, ValueError):
                        imp = 0
                    blocker_lines.append(
                        f"[{imp}] {str(b.get('state') or '')}  {str(b.get('question') or '')[:80]}"
                    )
                self_eval_prompt = (
                    "Your session doc has non-closed questions. Resolve or explicitly waive blockers before stopping.\n\n"
                    "Top blockers:\n" + "\n".join(blocker_lines)
                )
                async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
                    await sanctioned_update_instance(
                        db,
                        instance_id=session_id,
                        updates={
                            "workflow_state": "blocked",
                            "workflow_updated_at": blocked_at,
                            "workflow_blocked_reason": "questions_unclosed",
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
                        details={
                            "instance_type": instance_type,
                            "reason": "questions_unclosed",
                            "blockers": blocker_lines,
                        },
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
                    "StopValidate: blocking %s (%s) on questions gate",
                    session_id[:12],
                    instance_type,
                )
                await log_event(
                    "stop_validate_block",
                    instance_id=session_id,
                    details={"instance_type": instance_type, "reason": "questions_unclosed"},
                )
                return {"decision": "block", "reason": self_eval_prompt}

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

    # ── Block: golden_throne instances get self-eval prompt ──
    # (sync instances fall through to a clean accept — the Stop handler re-injects
    # a keepalive prompt instead of blocking on self-eval.)
    if instance_type in ("golden_throne",):
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


@router.post("/api/hooks/subscribe")
async def subscribe_hook(request: HookSubscribeRequest) -> dict:
    if request.event != "stop":
        return {"success": False, "action": "unsupported_event", "event": request.event}
    if request.delivery not in {"prompt", "ephemeral"}:
        return {"success": False, "action": "unsupported_delivery", "delivery": request.delivery}
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        # Resolve each distinct pane at most once per request. The plan-menu
        # preplan subscribe sends target_pane == subscriber_pane == the same %id,
        # and _resolve_instance_for_pane is the expensive leg (a tmux show-options
        # subprocess + a SQLite lookup, plus list-panes -a for non-%id forms). The
        # original double call paid that twice for one pane; memoizing on the
        # normalized pane string collapses the common same-pane case to one
        # resolution with byte-for-byte identical results.
        _pane_cache: dict[str, dict | None] = {}

        async def _resolve_pane_once(pane: str | None) -> dict | None:
            key = _normalize_text(pane)
            if not key:
                return None
            if key not in _pane_cache:
                _pane_cache[key] = await _resolve_instance_for_pane(db, pane)
            return _pane_cache[key]

        target = await _resolve_instance_by_id(db, request.target_instance_id)
        if not target or not target.get("id"):
            target = await _resolve_pane_once(request.target_pane)
        subscriber = await _resolve_instance_by_id(db, request.subscriber_instance_id)
        if not subscriber or not subscriber.get("tmux_pane"):
            subscriber = await _resolve_pane_once(request.subscriber_pane)

        target_id = (target or {}).get("id") or _normalize_text(request.target_instance_id)
        target_pane = (target or {}).get("tmux_pane") or _normalize_text(request.target_pane)
        subscriber_id = (subscriber or {}).get("id") or _normalize_text(
            request.subscriber_instance_id
        )
        subscriber_pane = (subscriber or {}).get("tmux_pane") or _normalize_text(
            request.subscriber_pane
        )
        if not target_id:
            return {"success": False, "action": "target_unresolved"}
        if not subscriber_pane:
            return {"success": False, "action": "subscriber_unresolved"}
        sub_id = await _upsert_stop_subscription(
            db,
            target_instance_id=target_id,
            target_pane=target_pane,
            subscriber_instance_id=subscriber_id,
            subscriber_pane=subscriber_pane,
            event=request.event,
            delivery=request.delivery,
            purpose=request.purpose,
            payload=request.payload,
            oneshot=request.oneshot,
        )
        await db.commit()
    return {
        "success": True,
        "action": "subscribed",
        "subscription_id": sub_id,
        "target_instance_id": target_id,
        "target_pane": target_pane,
        "subscriber_instance_id": subscriber_id,
        "subscriber_pane": subscriber_pane,
        "event": request.event,
        "delivery": request.delivery,
        "purpose": request.purpose,
        "payload": request.payload,
        "oneshot": request.oneshot,
    }


PLANNING_STATES = {"none", "preplanning", "planning", "approving"}
PLANNING_CYCLE = {
    "none": "preplanning",
    "preplanning": "planning",
    "planning": "none",
    "approving": "none",
}
# Tools Claude blocks while in plan mode. The first one to fire after the user
# approves a plan is a poll-free, race-proof "planning ended" signal (see
# handle_post_tool_use). Bash and read tools run freely in plan mode and would
# false-clear, so they are deliberately excluded.
MUTATING_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


async def _set_planning_state(
    db,
    instance_id: str,
    new_state: str,
    source: str,
    *,
    only_if_in: tuple[str, ...] | None = None,
    write_source: str = "hooks",
    actor: str = "planning-state",
) -> dict | None:
    """Core planning_state transition shared by the /api/planning/state endpoint
    and the event-driven auto-clear paths.

    SELECTs the current state, optionally CAS-gates on ``only_if_in`` (returns
    ``None`` when the row is not in one of those states — makes re-fires
    idempotent), then writes the three ``planning_*`` fields via the sanctioned
    path (the ``trg_planning_pane_state`` trigger auto-projects ``@PLANNING_STATE``
    when the value changes; a reassert queues an explicit projection). Returns the
    event detail dict for the caller to ``log_event`` AFTER its own ``db.commit()``
    — this function neither commits nor logs (``log_event`` opens its own
    connection, so ordering must stay caller-owned). Returns ``None`` on a missing
    row, a failed gate, or an invalid ``new_state``.
    """
    cursor = await db.execute(
        "SELECT planning_state, tmux_pane FROM instances WHERE id = ?",
        (instance_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    previous = row["planning_state"] or "none"
    if only_if_in is not None and previous not in only_if_in:
        return None
    if new_state not in PLANNING_STATES:
        return None
    tmux_pane = row["tmux_pane"]
    now = datetime.now().isoformat()
    await sanctioned_update_instance(
        db,
        instance_id=instance_id,
        updates={
            "planning_state": new_state,
            "planning_updated_at": now,
            "planning_source": source,
        },
        mutation_type="planning_state_changed",
        write_source=write_source,
        actor=actor,
    )
    # The DB trigger enqueues @PLANNING_STATE when the value changes.  If the
    # state is reasserted, queue an explicit projection so tmux hints recover.
    if previous == new_state:
        await db.execute(
            """INSERT INTO pane_state_queue (instance_id, variable, value, tmux_pane)
               VALUES (?, '@PLANNING_STATE', ?, ?)""",
            (instance_id, new_state, tmux_pane),
        )
    return {
        "old_state": previous,
        "new_state": new_state,
        "source": source,
        "tmux_pane": tmux_pane,
    }


@router.get("/api/planning/state")
async def get_planning_state(instance_id: str | None = None, tmux_pane: str | None = None) -> dict:
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        instance = await _resolve_instance_by_id(db, instance_id)
        if not instance or not instance.get("id"):
            instance = await _resolve_instance_for_pane(db, tmux_pane)
        resolved_id = (instance or {}).get("id")
        if not resolved_id:
            return {
                "success": False,
                "action": "instance_unresolved",
                "tmux_pane": _normalize_text(tmux_pane),
            }

        cursor = await db.execute(
            "SELECT planning_state, planning_source, tmux_pane, engine FROM instances WHERE id = ?",
            (resolved_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"success": False, "action": "instance_not_found", "instance_id": resolved_id}

    return {
        "success": True,
        "action": "planning_state",
        "instance_id": resolved_id,
        "tmux_pane": row["tmux_pane"],
        "planning_state": row["planning_state"] or "none",
        "planning_source": row["planning_source"],
        "engine": row["engine"],
    }


@router.post("/api/planning/state")
async def set_planning_state(request: PlanningStateRequest) -> dict:
    source = _normalize_text(request.source) or "api"
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        instance = await _resolve_instance_by_id(db, request.instance_id)
        if not instance or not instance.get("id"):
            instance = await _resolve_instance_for_pane(db, request.tmux_pane)
        instance_id = (instance or {}).get("id")
        tmux_pane = (instance or {}).get("tmux_pane") or _normalize_text(request.tmux_pane)
        if not instance_id:
            return {"success": False, "action": "instance_unresolved", "tmux_pane": tmux_pane}

        cursor = await db.execute(
            "SELECT planning_state, tmux_pane, engine FROM instances WHERE id = ?",
            (instance_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return {"success": False, "action": "instance_not_found", "instance_id": instance_id}
        previous = row["planning_state"] or "none"
        if request.cycle:
            new_state = PLANNING_CYCLE.get(previous, "preplanning")
        else:
            new_state = _normalize_text(request.state) or "none"
        if new_state not in PLANNING_STATES:
            return {"success": False, "action": "invalid_state", "state": new_state}
        tmux_pane = tmux_pane or row["tmux_pane"]
        event_details = await _set_planning_state(
            db,
            instance_id,
            new_state,
            source,
            write_source="api",
        )
        await db.commit()
    if event_details:
        await log_event(
            "planning_state_changed",
            instance_id=instance_id,
            details=event_details,
        )
    return {
        "success": True,
        "action": "planning_state_changed",
        "instance_id": instance_id,
        "tmux_pane": tmux_pane,
        "previous_state": previous,
        "planning_state": new_state,
        "source": source,
        "engine": (instance or {}).get("engine") or row["engine"],
    }


@router.post("/api/hooks/unsubscribe")
async def unsubscribe_hook(request: HookUnsubscribeRequest) -> dict:
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        target = await _resolve_instance_by_id(db, request.target_instance_id)
        if not target or not target.get("id"):
            target = await _resolve_instance_for_pane(db, request.target_pane)
        subscriber = await _resolve_instance_by_id(db, request.subscriber_instance_id)
        if not subscriber or not subscriber.get("tmux_pane"):
            subscriber = await _resolve_instance_for_pane(db, request.subscriber_pane)
        target_id = (target or {}).get("id") or _normalize_text(request.target_instance_id)
        target_pane = (target or {}).get("tmux_pane") or _normalize_text(request.target_pane)
        subscriber_id = (subscriber or {}).get("id") or _normalize_text(
            request.subscriber_instance_id
        )
        subscriber_pane = (subscriber or {}).get("tmux_pane") or _normalize_text(
            request.subscriber_pane
        )
        # A selector value (--pane / --notify) is ambiguous: it can be a tmux
        # pane id OR an instance UUID. The old code only ever built a *_pane
        # clause, so `unsubscribe --pane <uuid> --notify <uuid>` matched nothing
        # (stored panes are %NN, never UUIDs) and silently removed zero rows even
        # when the watched pane was live and the notify UUID was exact. Match each
        # side on instance_id OR pane, and surface a bare UUID passed in the pane
        # slot as an id candidate so it matches the instance_id column (covers
        # live-watched + exact-notify UUIDs and dead/phantom stored ids).
        target_id_match = target_id or _normalize_text(request.target_pane)
        subscriber_id_match = subscriber_id or _normalize_text(request.subscriber_pane)
        clauses = ["event = ?", "status = 'active'"]
        params: list[str | None] = [request.event]
        have_selector = False
        if target_id_match or target_pane:
            have_selector = True
            clauses.append("(target_instance_id = ? OR target_pane = ?)")
            params.extend([target_id_match, target_pane])
        if subscriber_id_match or subscriber_pane:
            have_selector = True
            clauses.append("(subscriber_instance_id = ? OR subscriber_pane = ?)")
            params.extend([subscriber_id_match, subscriber_pane])
        if request.purpose:
            clauses.append("purpose = ?")
            params.append(request.purpose)
        if not have_selector:
            return {"success": False, "action": "no_selector"}
        now = datetime.now().isoformat()
        cursor = await db.execute(
            f"""UPDATE stop_hook_subscriptions
                SET status = 'unsubscribed', unsubscribed_at = ?, updated_at = ?
                WHERE {" AND ".join(clauses)}""",
            (now, now, *params),
        )
        await db.commit()
    return {"success": True, "action": "unsubscribed", "count": cursor.rowcount or 0}


@router.get("/api/hooks/subscriptions")
async def list_hook_subscriptions(
    target_instance_id: str | None = None,
    target_pane: str | None = None,
    subscriber_instance_id: str | None = None,
    subscriber_pane: str | None = None,
    event: str = "stop",
    status: str = "active",
    purpose: str | None = None,
) -> dict:
    clauses = ["event = ?"]
    params: list[str] = [event]
    if status != "all":
        clauses.append("status = ?")
        params.append(status)
    if target_instance_id:
        clauses.append("target_instance_id = ?")
        params.append(target_instance_id)
    if target_pane:
        clauses.append("target_pane = ?")
        params.append(target_pane)
    if subscriber_instance_id:
        clauses.append("subscriber_instance_id = ?")
        params.append(subscriber_instance_id)
    if subscriber_pane:
        clauses.append("subscriber_pane = ?")
        params.append(subscriber_pane)
    if purpose:
        clauses.append("purpose = ?")
        params.append(purpose)
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""SELECT * FROM stop_hook_subscriptions
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC, id DESC""",
            params,
        )
        rows = [dict(row) for row in await cursor.fetchall()]
    return {"success": True, "subscriptions": rows, "count": len(rows)}


@router.post("/api/hooks/reconcile")
async def reconcile_hook_subscriptions(request: HookReconcileRequest) -> dict:
    page = (request.page or "mechanicus").strip().lower()
    if page != "mechanicus":
        return {"success": False, "action": "unsupported_page", "page": request.page}
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        result = await _reconcile_mechanicus_stop_subscriptions(db)
        if result.get("created") or result.get("existing"):
            await db.commit()
    return result


@router.post("/api/hooks/prune")
async def prune_hook_subscriptions(request: HookPruneRequest) -> dict:
    """Garbage-collect active subscriptions with dead watched/notify instances."""
    async with aiosqlite.connect(DB_PATH, timeout=5.0) as db:
        return await _prune_dangling_stop_subscriptions(
            db, confirm=request.confirm, event=request.event
        )


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
