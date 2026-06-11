"""Canonical instance registry v2 helpers.

The ``instances`` table is the durable registry. It intentionally excludes tmux
runtime identity and legacy workflow/PR/victory/planning fields.
"""

from __future__ import annotations

import json
from datetime import datetime

RUNTIME_TMUX_FIELDS = {
    "tmux_pane",
    "pane_label",
    "dispatch_target",
    "dispatch_window",
    "dispatch_slot",
}

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
]

REMOVED_INSTANCE_COLUMNS = {
    "tab_name",
    "session_id",
    "source_ip",
    "pid",
    "tmux_pane",
    "pane_label",
    "dispatch_target",
    "dispatch_window",
    "dispatch_slot",
    "legion",
    "primarch",
    "profile_name",
    "tts_voice",
    "notification_sound",
    "tts_mode",
    "is_subagent",
    "parent_instance_id",
    "session_doc_policy",
    "zealotry",
    "gt_resume_count",
    "gt_resume_window_started_at",
    "gt_last_resume_at",
    "follow_up_sop",
    "stop_allowed",
    "victory_at",
    "victory_reason",
    "pr_url",
    "pr_state",
    "workflow_state",
    "workflow_updated_at",
    "workflow_blocked_reason",
    "next_required_action",
    "next_action_owner",
    "planning_state",
    "planning_updated_at",
    "planning_source",
    "transplant_target_session",
    "transplant_expected",
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
    "mechanicus:admin": "administratum",
    "legion:custodes": "custodes",
    "legion:malcador": "malcador",
    "mechanicus": "administratum",
}


def assert_no_runtime_tmux_fields(values: dict, *, context: str) -> None:
    forbidden = RUNTIME_TMUX_FIELDS & set(values.keys())
    if forbidden:
        raise ValueError(
            f"{context} must not persist tmux/runtime ids: " + ", ".join(sorted(forbidden))
        )


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
    instance_type = (row.get("instance_type") or "").strip().lower()
    if instance_type == "sync" or row.get("synced") == 1:
        return "sync"
    return None


def legacy_row_to_instance_values(row: dict | None, persona_id: int | None = None) -> dict:
    """Map a legacy claude_instances row into final instances columns."""
    if not row:
        return {}
    status = normalize_status(row.get("status"))
    is_chapter_child = bool(row.get("parent_instance_id")) or bool(row.get("is_subagent"))
    if row.get("parent_instance_id"):
        commander_type = "chapter"
        commander_id = row.get("parent_instance_id")
    else:
        commander_type = "emperor"
        commander_id = None
    created = row.get("registered_at") or row.get("created_at") or datetime.now().isoformat()
    return {
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
        "persona_id": persona_id,
        "rank": normalize_rank(row.get("rank"), status=status),
        "session_doc_id": row.get("session_doc_id"),
        "continuity_binding_source": row.get("continuity_binding_source"),
        "wrapper_launch_id": row.get("wrapper_launch_id"),
        "automated": 1 if (row.get("hook_driven") or is_chapter_child) else 0,
        "notification_mode": normalize_notification_mode(row.get("tts_mode")),
        "interaction_mode": normalize_interaction_mode(row.get("tts_mode")),
        "golden_throne": golden_throne_binding(row),
    }


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
