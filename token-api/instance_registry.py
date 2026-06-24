"""Instance registry helpers.

The ``instances`` table is the durable registry and — post legacy instance table
exterminatus — the ONE physical instance table. It has two column tiers:

* IDENTITY_COLUMNS: the durable instance registry charter (persona/rank/commander/
  origin). Authoritative, never derived from anywhere else.
* RUNTIME_ANNEX_COLUMNS: transitional runtime/workflow fields inherited from
  the extracted legacy instance table. Each is slated for per-column
  demolition as its successor lands (tmux @INSTANCE_ID stamps for pane
  geometry, the golden_throne table for GT state, status enum for workflow/
  planning). New code must not grow this list.

The legacy the legacy instance table table itself lives in archive.db only (see
db_schema.extract_legacy instance table / restore_legacy instance table_from_archive).
"""

from __future__ import annotations

import json
from datetime import datetime

IDENTITY_COLUMNS = [
    "id",
    "name",
    "engine",
    "working_dir",
    "device_id",
    "origin_type",
    "commander_type",
    "commander_id",
    "status",
    "created_at",
    "last_activity",
    "stopped_at",
    "archived_at",
    "persona_id",
    "rank",
    "session_doc_id",
    "continuity_binding_source",
    "wrapper_launch_id",
    "automated",
    "notification_mode",
    "interaction_mode",
    "golden_throne",
    "human_anchored_at",
    "human_anchor_source",
]

# Transitional runtime annex (see module docstring). Order matters: it is the
# physical column order in the CREATE TABLE.
RUNTIME_ANNEX_COLUMNS = [
    # tmux/dispatch geometry — dies when @INSTANCE_ID-stamp resolution lands.
    # tmux_pane/pane_label are GONE: pane ids are never persisted (too volatile),
    # the tmuxctl runtime oracle resolves geometry live from @INSTANCE_ID stamps.
    "dispatch_target",
    "dispatch_window",
    "dispatch_mode",
    "dispatch_slot",
    "dispatch_session_doc_path",
    "target_working_dir",
    "launch_mode",
    "launcher",
    "transplant_target_session",
    "transplant_expected",
    "input_lock",
    # per-instance voice overrides
    "tts_voice",
    "notification_sound",
    # discord hosting
    "discord_hosted",
    "discord_channel",
    "discord_bot",
    # workflow / planning / closure — dies into the status enum
    "workflow_state",
    "workflow_updated_at",
    "workflow_blocked_reason",
    "next_required_action",
    "next_action_owner",
    "planning_state",
    "planning_updated_at",
    "planning_source",
    "closure_surface",
    "closure_required",
    "session_doc_policy",
    "pr_url",
    "pr_state",
    "victory_at",
    "victory_reason",
    # provenance flags kept distinct from `automated` (semantics differ)
    "is_subagent",
    "hook_driven",
    # golden-throne engine state — dies into the golden_throne table
    "zealotry",
    "gt_resume_count",
    "gt_resume_window_started_at",
    "gt_last_resume_at",
    "follow_up_sop",
    "stop_allowed",
]

INSTANCE_COLUMNS = IDENTITY_COLUMNS + RUNTIME_ANNEX_COLUMNS

# Legacy legacy instance table columns with NO live home: their values exist only in
# archive.db. Reads repoint to the instance-table derivation noted inline.
REMOVED_INSTANCE_COLUMNS = {
    "tab_name",  # -> instances.name (API responses alias `name AS tab_name`)
    "session_id",  # archive-only
    "source_ip",  # archive-only
    "pid",  # archive-only
    "legion",  # -> persona_id (JOIN personas ON slug)
    "primarch",  # -> persona_id
    "profile_name",  # -> persona_id
    "tts_mode",  # -> notification_mode + interaction_mode
    "instance_type",  # -> golden_throne marker (NULL | 'sync' | golden_throne.id)
    "synced",  # -> golden_throne = 'sync'
    "parent_instance_id",  # -> commander_type='chapter' + commander_id
    "registered_at",  # -> created_at
}

VALID_ORIGIN_TYPES = {"local", "ssh", "cron", "dispatch", "api", "perpetual"}
VALID_COMMANDER_TYPES = {"emperor", "persona", "chapter"}
VALID_STATUSES = {
    "idle",
    "working",
    "questioning",
    "preplanning",
    "planning",
    "compacting",
    "reviewing",
    "victorious",
    "stopped",
    "archived",
}
VALID_RANKS = {"astartes", "overseer", "primarch", "retired"}
VALID_NOTIFICATION_MODES = {"verbose", "muted", "silent"}
VALID_INTERACTION_MODES = {"text", "voice_chat"}

LEGACY_PERSONA_ALIASES = {
    "fabricator": "fabricator-general",
    "mechanicus:fabricator-general": "fabricator-general",
    "mechanicus:administratum": "administratum",
    "council:administratum": "administratum",
    "council:custodes": "custodes",
    "council:malcador": "malcador",
    "council:pax": "pax",
    "mechanicus:orchestrator": "orchestrator",
    "mechanicus:worker": "agentic-worker",
    "mechanicus": "administratum",
}


def normalize_status(status: str | None) -> str:
    value = (status or "idle").strip().lower()
    if value == "processing":
        return "working"
    if value in VALID_STATUSES:
        return value
    return "idle"


def normalize_origin_type(value: str | None) -> str:
    value = (value or "local").strip().lower()
    return value if value in VALID_ORIGIN_TYPES else "local"


def normalize_notification_mode(tts_mode: str | None) -> str:
    value = (tts_mode or "verbose").strip().lower().replace("_", "-")
    if value == "silent":
        return "silent"
    if value in {"muted", "mute"}:
        return "muted"
    return "verbose"


def normalize_interaction_mode(tts_mode: str | None) -> str:
    return (
        "voice_chat" if (tts_mode or "").strip().lower() in {"voice-chat", "voice_chat"} else "text"
    )


def normalize_rank(value: str | None, *, status: str | None = None) -> str:
    raw = (value or "astartes").strip().lower()
    if status == "archived":
        return "retired"
    return raw if raw in VALID_RANKS else "astartes"


def slug_from_legacy(row: dict | None) -> str | None:
    if not row:
        return None
    candidates = [row.get("profile_name"), row.get("primarch"), row.get("legion")]
    for candidate in candidates:
        value = (candidate or "").strip().lower()
        if value:
            return LEGACY_PERSONA_ALIASES.get(value, value)
    return None


def golden_throne_binding(row: dict) -> str | None:
    # 'sync' here is a runtime MODE value for the golden_throne column, never an
    # identity source. Custodes (and other singletons) are resolved by persona +
    # rank via personas.resolve_live_persona_instance — nothing finds the Custodes
    # by golden_throne='sync'/synced/instance_type after the sync-decouple change.
    instance_type = (row.get("instance_type") or "").strip().lower()
    if instance_type == "sync" or row.get("synced") == 1:
        return "sync"
    return None


def legacy_row_to_instance_values(row: dict | None, persona_id: int | None = None) -> dict:
    """Map a legacy instance table row into instances-table columns."""
    if not row:
        return {}
    status = normalize_status(row.get("status"))
    is_chapter_child = bool(row.get("parent_instance_id")) or bool(row.get("is_subagent"))
    if row.get("commander_type"):
        # explicit instance-table shape wins over the legacy parent_instance_id derivation
        commander_type = row["commander_type"]
        commander_id = row.get("commander_id")
    elif row.get("parent_instance_id"):
        commander_type = "chapter"
        commander_id = row.get("parent_instance_id")
    else:
        commander_type = "emperor"
        commander_id = None
    created = row.get("registered_at") or row.get("created_at") or datetime.now().isoformat()
    values = {
        "id": row.get("id") or row.get("session_id"),
        "name": row.get("tab_name") or row.get("name") or row.get("id") or row.get("session_id"),
        "engine": row.get("engine"),
        "working_dir": row.get("working_dir"),
        "device_id": row.get("device_id") or "unknown",
        "origin_type": normalize_origin_type(row.get("origin_type")),
        "commander_type": commander_type,
        "commander_id": commander_id,
        "status": status,
        "created_at": created,
        "last_activity": row.get("last_activity") or created,
        "stopped_at": row.get("stopped_at"),
        "archived_at": row.get("archived_at"),
        "persona_id": persona_id if persona_id is not None else row.get("persona_id"),
        "rank": normalize_rank(row.get("rank"), status=status),
        "session_doc_id": row.get("session_doc_id"),
        "continuity_binding_source": row.get("continuity_binding_source"),
        "wrapper_launch_id": row.get("wrapper_launch_id"),
        "automated": row["automated"]
        if row.get("automated") is not None
        else (1 if (row.get("hook_driven") or is_chapter_child) else 0),
        "notification_mode": row.get("notification_mode")
        or normalize_notification_mode(row.get("tts_mode")),
        "interaction_mode": row.get("interaction_mode")
        or normalize_interaction_mode(row.get("tts_mode")),
        "golden_throne": row.get("golden_throne") or golden_throne_binding(row),
        "human_anchored_at": row.get("human_anchored_at"),
        "human_anchor_source": row.get("human_anchor_source"),
    }
    # Runtime annex passthrough: any annex column present on the legacy row
    # carries over verbatim (the extraction backfill and transitional
    # legacy-shaped insert paths both rely on this).
    for column in RUNTIME_ANNEX_COLUMNS:
        if column in row:
            values[column] = row.get(column)
    return values


def derived_cockpit_label(
    row: dict, *, stale_minutes: int = 30, now: datetime | None = None
) -> str | None:
    status = row.get("status")
    if status == "working" and int(row.get("automated") or 0):
        return "interred"
    if status == "working":
        return "commanded"
    if status == "idle" and row.get("last_activity"):
        try:
            last = datetime.fromisoformat(str(row["last_activity"]).replace("Z", "+00:00"))
            current = now or datetime.now(last.tzinfo)
            if (current - last).total_seconds() > stale_minutes * 60:
                return "languishing"
        except Exception:
            return None
    return None


def metadata_json(value: dict | None) -> str:
    return json.dumps(value or {}, sort_keys=True)
