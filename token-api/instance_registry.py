"""Instance registry schema constants and archive import helpers.

Runtime writes use canonical present-tense ``instances`` columns only. Archive and
migration imports may translate historical rows, but normal insert/update paths
must reject dead launch, voice, and projection fields instead of mapping them.

The ``instances`` table is durable live-agent registry state: identity,
lifecycle, workflow, notification, and Golden Throne binding. Launch envelopes,
pane geometry, transplant markers, and copied persona audio settings are not
instance state.
"""

from __future__ import annotations

import json
from datetime import datetime

DEFAULT_INSTANCE_NAME = "needs-name"

INSTANCE_COLUMNS = [
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
    "input_lock",
    "discord_hosted",
    "discord_channel",
    "discord_bot",
    # workflow / planning / closure
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
    # golden-throne engine state
    "zealotry",
    "gt_resume_count",
    "gt_resume_window_started_at",
    "gt_last_resume_at",
    "gt_no_op_counter",
    "gt_no_op_summaries_json",
    "gt_last_dispatch_fingerprint",
    "follow_up_sop",
    "stop_allowed",
]

FORBIDDEN_RUNTIME_INSTANCE_FIELDS = {
    # dead projections / historical request names
    "tab_name",
    "session_id",
    "source_ip",
    "pid",
    "legion",
    "primarch",
    "profile_name",
    "tts_mode",
    "instance_type",
    "synced",
    "parent_instance_id",
    "registered_at",
    "tmux_pane",
    "pane_label",
    "pane_tint",
    "color",
    "chip_color",
    "advisor",
    "operator_proxy",
    # launch geometry/provenance is event/input context, not instance identity
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
    # persona voice/sound live on personas, not copied onto instances
    "tts_voice",
    "notification_sound",
}

# Runtime writers may only address the canonical physical schema. This alias is
# intentionally boring so mutation/write code never knows old column names as
# writable destinations.
RUNTIME_WRITE_INSTANCE_COLUMNS = list(INSTANCE_COLUMNS)
RUNTIME_WRITE_INSTANCE_FIELDS = set(RUNTIME_WRITE_INSTANCE_COLUMNS)

# Columns from extracted historical instance shapes with no live home.
REMOVED_INSTANCE_COLUMNS = {
    "tab_name",  # -> instances.name
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
VALID_RANKS = {"astartes", "scribe", "overseer", "primarch", "retired"}
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


def archive_row_to_instance_values(row: dict | None, persona_id: int | None = None) -> dict:
    """Map an archived/historical instance row into current table columns.

    This is for schema migration and archive import only. Runtime write helpers
    must not call it.
    """
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
        # New/default names are never synthesized from ids, session docs, cwd,
        # persona, dates, or dispatch metadata.  Official renames are explicit
        # updates after registration.
        "name": row.get("tab_name") or row.get("name") or DEFAULT_INSTANCE_NAME,
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
    # Copy only fields that are canonical in the present schema. Extracted
    # launch/audio/transplant fields remain archive-only.
    for column in INSTANCE_COLUMNS:
        if column not in values and column in row:
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
