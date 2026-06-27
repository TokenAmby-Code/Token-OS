import json
import logging
import os
import sqlite3
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

import aiosqlite

from instance_registry import (
    DEFAULT_INSTANCE_NAME,
    INSTANCE_COLUMNS,
    legacy_row_to_instance_values,
    slug_from_legacy,
)
from pane_surface import is_placeholder_tab_name
from personas import persona_tint_for_instance

logger = logging.getLogger("token_api")

SERVICE_VERSION_FALLBACK = "0.1.0"
RECONCILIATION_SUSPICIOUS = {
    "unprovenanced_write",
    "state_drift",
    "projection_drift",
    "illegal_instance_name",
}

OFFICIAL_INSTANCE_NAME_ACTORS = frozenset(
    {
        "instance-name-cli",
        "naming-interview",
        "session-doc-name",
    }
)

# Tracked mutation surface = durable identity + runtime annex. Legacy column names
# (tab_name/legion/synced/instance_type/primarch/parent_instance_id/tts_mode/pid)
# died with the legacy instance table extraction; their durable homes are name/persona_id/
# golden_throne/commander_*/notification_mode+interaction_mode.
INSTANCE_MUTATION_FIELDS = {
    # identity
    "name",
    "engine",
    "status",
    "last_activity",
    "stopped_at",
    "archived_at",
    "working_dir",
    "device_id",
    "origin_type",
    "persona_id",
    "rank",
    "commander_type",
    "commander_id",
    "automated",
    "notification_mode",
    "interaction_mode",
    "golden_throne",
    "human_anchored_at",
    "human_anchor_source",
    "session_doc_id",
    "continuity_binding_source",
    "wrapper_launch_id",
    # runtime annex
    "input_lock",
    "tts_voice",
    "notification_sound",
    "session_doc_policy",
    "workflow_state",
    "workflow_updated_at",
    "workflow_blocked_reason",
    "dispatch_target",
    "dispatch_window",
    "dispatch_mode",
    "dispatch_slot",
    "dispatch_session_doc_path",
    "target_working_dir",
    "launch_mode",
    "launcher",
    "follow_up_sop",
    "zealotry",
    "gt_resume_count",
    "gt_resume_window_started_at",
    "gt_last_resume_at",
    "gt_no_op_counter",
    "gt_no_op_summaries_json",
    "gt_last_dispatch_fingerprint",
    "victory_at",
    "victory_reason",
    "discord_hosted",
    "discord_channel",
    "discord_bot",
    "transplant_target_session",
    "transplant_expected",
    "closure_surface",
    "closure_required",
    "stop_allowed",
    "next_required_action",
    "next_action_owner",
    "planning_state",
    "planning_updated_at",
    "planning_source",
    "pr_url",
    "pr_state",
    "hook_driven",
    "is_subagent",
}

# Canonical registry columns. These deliberately exclude every raw tmux pane
# id and tmuxctl positional/cardinal field. Token-API may return live runtime
# resolution data, but the DB must not persist it; tmuxctl is the oracle.
CANONICAL_INSTANCE_FIELDS = set(INSTANCE_COLUMNS)


async def _persona_id_for_row(db, row: dict) -> int | None:
    slug = slug_from_legacy(row)
    if not slug:
        return row.get("persona_id")
    cursor = await db.execute("SELECT id FROM personas WHERE slug = ?", (slug,))
    found = await cursor.fetchone()
    return found[0] if found else row.get("persona_id")


def _persona_id_for_row_sync(db, row: dict) -> int | None:
    slug = slug_from_legacy(row)
    if not slug:
        return row.get("persona_id")
    cursor = db.execute("SELECT id FROM personas WHERE slug = ?", (slug,))
    found = cursor.fetchone()
    return found[0] if found else row.get("persona_id")


def _instance_values_from_legacy_row(row: dict | None, persona_id: int | None = None) -> dict:
    if not row:
        return {}
    if set(INSTANCE_COLUMNS).issubset(row.keys()):
        values = {key: row.get(key) for key in INSTANCE_COLUMNS}
    else:
        values = legacy_row_to_instance_values(row, persona_id)
    return {key: values.get(key) for key in INSTANCE_COLUMNS if key in values}


def is_official_instance_name_actor(actor: str | None) -> bool:
    return (actor or "").strip() in OFFICIAL_INSTANCE_NAME_ACTORS


def _coerce_insert_name(values: dict) -> dict:
    """Force every insert through the single placeholder name.

    Official naming is an explicit post-registration update, never an insert-time
    derivation.
    """
    if values.get("name") == DEFAULT_INSTANCE_NAME:
        return values
    return {**values, "name": DEFAULT_INSTANCE_NAME}


def _assert_name_update_authorized(
    updates: dict, *, actor: str, current_name: str | None = None
) -> None:
    if "name" not in updates:
        return
    if updates.get("name") == current_name:
        return
    if updates.get("name") == DEFAULT_INSTANCE_NAME:
        return
    if is_placeholder_tab_name(updates.get("name")):
        raise ValueError("instances.name cannot be set to a deprecated placeholder")
    if is_official_instance_name_actor(actor):
        return
    raise ValueError(
        "instances.name may only be set to a non-placeholder value by the official "
        "rename path (instance-name-cli, naming-interview, session-doc-name)"
    )


async def _prepare_chapter_commander(db, values: dict) -> dict:
    if values.get("commander_type") != "chapter" or not values.get("commander_id"):
        return values
    commander_id = values["commander_id"]
    cursor = await db.execute("SELECT persona_id FROM instances WHERE id = ?", (commander_id,))
    commander = await cursor.fetchone()
    if commander:
        # A chapter edge conveys control, not identity.  Preserve an already
        # resolved worker persona (from TOKEN_API_PERSONA / TOKEN_API_LEGION /
        # dispatch context); inherit the commander's persona only as the legacy
        # fallback for rows that arrived with no persona at all.
        if values.get("persona_id") is None:
            values["persona_id"] = commander[0]
    else:
        # A chapter edge must point at a live commander row in `instances`
        # (the legacy legacy instance table fallback died with the extraction).
        values["commander_type"] = "emperor"
        values["commander_id"] = None
    return values


def _prepare_chapter_commander_sync(db, values: dict) -> dict:
    if values.get("commander_type") != "chapter" or not values.get("commander_id"):
        return values
    commander_id = values["commander_id"]
    commander = db.execute(
        "SELECT persona_id FROM instances WHERE id = ?", (commander_id,)
    ).fetchone()
    if commander:
        # A chapter edge conveys control, not identity.  Preserve an already
        # resolved worker persona; inherit the commander's persona only as the
        # fallback for rows that arrived with no persona at all.
        if values.get("persona_id") is None:
            values["persona_id"] = commander[0]
    else:
        values["commander_type"] = "emperor"
        values["commander_id"] = None
    return values


# The dual-write mirror layer (mirror_instance_to_legacy*) died with the
# legacy instance table extraction: every sanctioned write now lands directly on
# `instances`, the one physical table.


async def create_golden_throne_binding(
    db,
    *,
    zealotry: int | None = None,
    follow_up_sop: str | None = None,
    stop_allowed: int | None = None,
) -> str:
    """Insert a golden_throne row and return its id as the instances.golden_throne
    marker value. The guard trigger requires the marker to be NULL, 'sync', or a
    real golden_throne.id — callers must create the row BEFORE setting the marker.
    """
    cursor = await db.execute(
        """INSERT INTO golden_throne (zealotry, follow_up_sop, stop_allowed)
           VALUES (?, ?, ?)""",
        (
            zealotry if zealotry is not None else 4,
            follow_up_sop,
            stop_allowed if stop_allowed is not None else 1,
        ),
    )
    return str(cursor.lastrowid)


async def _fetch_instance_record(db, instance_id: str) -> dict | None:
    previous_factory = getattr(db, "row_factory", None)
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute("SELECT * FROM instances WHERE id = ?", (instance_id,))
        row = await cursor.fetchone()
    finally:
        db.row_factory = previous_factory
    return dict(row) if row else None


async def sanctioned_update_instance_record(
    db,
    *,
    instance_id: str,
    updates: dict,
    mutation_type: str,
    write_source: str,
    actor: str,
    wrapper_launch_id: str | None = None,
) -> dict:
    before_row = await _fetch_instance_record(db, instance_id)
    if before_row is None:
        raise LookupError(f"Instance not found: {instance_id}")
    _assert_name_update_authorized(updates, actor=actor, current_name=before_row.get("name"))

    changed_fields = [field for field, value in updates.items() if before_row.get(field) != value]
    assignments = ", ".join(f"{field} = ?" for field in updates)
    cursor = await db.execute(
        f"UPDATE instances SET {assignments} WHERE id = ?",
        [*updates.values(), instance_id],
    )
    if cursor.rowcount == 0:
        raise LookupError(f"Sanctioned instance-record update matched no rows for {instance_id}")

    after_row = dict(before_row)
    after_row.update(updates)
    write_txn_id = str(uuid.uuid4())
    if changed_fields:
        await _append_instance_mutation(
            db,
            instance_id=instance_id,
            mutation_type=mutation_type,
            write_source=write_source,
            actor=actor,
            wrapper_launch_id=wrapper_launch_id or after_row.get("wrapper_launch_id"),
            write_txn_id=write_txn_id,
            field_names=changed_fields,
            before=_subset_from_row(before_row, changed_fields),
            after=_subset_from_row(after_row, changed_fields),
        )
    return {
        "write_txn_id": write_txn_id,
        "changed_fields": changed_fields,
        "before": before_row,
        "after": after_row,
    }


def _detect_service_version() -> str:
    repo_root = Path(__file__).resolve().parent
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        text = result.stdout.strip()
        if text:
            return text
    except Exception:
        pass
    return SERVICE_VERSION_FALLBACK


SERVICE_VERSION = _detect_service_version()


def _json_dumps(value):
    return json.dumps(value) if value is not None else None


def _parse_json_column(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


async def _fetch_instance_row(db, instance_id: str) -> dict | None:
    return await _fetch_instance_record(db, instance_id)


def _subset_from_row(row: dict | None, fields: list[str]) -> dict | None:
    if row is None:
        return None
    snapshot = {field: row.get(field) for field in fields}
    return snapshot


async def _append_instance_mutation(
    db,
    *,
    instance_id: str,
    mutation_type: str,
    write_source: str,
    actor: str,
    wrapper_launch_id: str | None,
    write_txn_id: str,
    field_names: list[str],
    before: dict | None,
    after: dict | None,
):
    await db.execute(
        """INSERT INTO instance_mutations
           (instance_id, mutation_type, write_source, write_txn_id, actor, service_version,
            wrapper_launch_id, field_names_json, before_json, after_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            instance_id,
            mutation_type,
            write_source,
            write_txn_id,
            actor,
            SERVICE_VERSION,
            wrapper_launch_id,
            _json_dumps(field_names),
            _json_dumps(before),
            _json_dumps(after),
            datetime.now().isoformat(),
        ),
    )


def _fetch_instance_row_sync(db, instance_id: str) -> dict | None:
    previous_factory = getattr(db, "row_factory", None)
    db.row_factory = sqlite3.Row
    try:
        cursor = db.execute("SELECT * FROM instances WHERE id = ?", (instance_id,))
        row = cursor.fetchone()
    finally:
        db.row_factory = previous_factory
    return dict(row) if row else None


def _append_instance_mutation_sync(
    db,
    *,
    instance_id: str,
    mutation_type: str,
    write_source: str,
    actor: str,
    wrapper_launch_id: str | None,
    write_txn_id: str,
    field_names: list[str],
    before: dict | None,
    after: dict | None,
):
    db.execute(
        """INSERT INTO instance_mutations
           (instance_id, mutation_type, write_source, write_txn_id, actor, service_version,
            wrapper_launch_id, field_names_json, before_json, after_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            instance_id,
            mutation_type,
            write_source,
            write_txn_id,
            actor,
            SERVICE_VERSION,
            wrapper_launch_id,
            _json_dumps(field_names),
            _json_dumps(before),
            _json_dumps(after),
            datetime.now().isoformat(),
        ),
    )


async def sanctioned_update_instance(
    db,
    *,
    instance_id: str,
    updates: dict,
    mutation_type: str,
    write_source: str,
    actor: str,
    wrapper_launch_id: str | None = None,
    workflow_events: list[dict] | None = None,
    tracked_fields: set[str] | None = None,
    where_clause: str | None = None,
    where_params: tuple | list | None = None,
) -> dict:
    if not updates:
        raise ValueError("no fields to update")
    force_clear_human_anchor = updates.get("status") in {"stopped", "archived"}
    if force_clear_human_anchor:
        updates = {
            **updates,
            "human_anchored_at": None,
            "human_anchor_source": None,
        }
    before_row = await _fetch_instance_row(db, instance_id)
    if before_row is None:
        raise LookupError(f"Instance not found: {instance_id}")
    _assert_name_update_authorized(updates, actor=actor, current_name=before_row.get("name"))

    tracked = set(tracked_fields) if tracked_fields is not None else set(INSTANCE_MUTATION_FIELDS)
    if force_clear_human_anchor:
        tracked.update({"human_anchored_at", "human_anchor_source"})
    changed_fields = []
    for field, value in updates.items():
        if field in tracked and before_row.get(field) != value:
            changed_fields.append(field)

    assignments = ", ".join(f"{field} = ?" for field in updates)
    params = list(updates.values())
    clause = where_clause or "id = ?"
    params.extend(list(where_params or (instance_id,)))
    cursor = await db.execute(f"UPDATE instances SET {assignments} WHERE {clause}", params)

    if cursor.rowcount == 0:
        raise LookupError(f"Sanctioned update matched no rows for instance {instance_id}")

    after_row = dict(before_row)
    after_row.update(updates)
    write_txn_id = str(uuid.uuid4())

    if changed_fields:
        await _append_instance_mutation(
            db,
            instance_id=instance_id,
            mutation_type=mutation_type,
            write_source=write_source,
            actor=actor,
            wrapper_launch_id=wrapper_launch_id or after_row.get("wrapper_launch_id"),
            write_txn_id=write_txn_id,
            field_names=changed_fields,
            before=_subset_from_row(before_row, changed_fields),
            after=_subset_from_row(after_row, changed_fields),
        )

    for event in workflow_events or []:
        await db.execute(
            """INSERT INTO workflow_events (instance_id, workflow_state, event_type, event_owner, details_json)
               VALUES (?, ?, ?, ?, ?)""",
            (
                instance_id,
                event.get("workflow_state"),
                event["event_type"],
                event.get("event_owner"),
                _json_dumps(event.get("details")),
            ),
        )

    return {
        "write_txn_id": write_txn_id,
        "changed_fields": changed_fields,
        "before": before_row,
        "after": after_row,
    }


def sanctioned_update_instance_sync(
    db,
    *,
    instance_id: str,
    updates: dict,
    mutation_type: str,
    write_source: str,
    actor: str,
    wrapper_launch_id: str | None = None,
    tracked_fields: set[str] | None = None,
    where_clause: str | None = None,
    where_params: tuple | list | None = None,
) -> dict:
    if not updates:
        raise ValueError("no fields to update")
    force_clear_human_anchor = updates.get("status") in {"stopped", "archived"}
    if force_clear_human_anchor:
        updates = {
            **updates,
            "human_anchored_at": None,
            "human_anchor_source": None,
        }
    before_row = _fetch_instance_row_sync(db, instance_id)
    if before_row is None:
        raise LookupError(f"Instance not found: {instance_id}")
    _assert_name_update_authorized(updates, actor=actor, current_name=before_row.get("name"))

    tracked = set(tracked_fields) if tracked_fields is not None else set(INSTANCE_MUTATION_FIELDS)
    if force_clear_human_anchor:
        tracked.update({"human_anchored_at", "human_anchor_source"})
    changed_fields = []
    for field, value in updates.items():
        if field in tracked and before_row.get(field) != value:
            changed_fields.append(field)

    assignments = ", ".join(f"{field} = ?" for field in updates)
    params = list(updates.values())
    clause = where_clause or "id = ?"
    params.extend(list(where_params or (instance_id,)))
    cursor = db.execute(f"UPDATE instances SET {assignments} WHERE {clause}", params)

    if cursor.rowcount == 0:
        raise LookupError(f"Sanctioned update matched no rows for instance {instance_id}")

    after_row = dict(before_row)
    after_row.update(updates)
    write_txn_id = str(uuid.uuid4())

    if changed_fields:
        _append_instance_mutation_sync(
            db,
            instance_id=instance_id,
            mutation_type=mutation_type,
            write_source=write_source,
            actor=actor,
            wrapper_launch_id=wrapper_launch_id or after_row.get("wrapper_launch_id"),
            write_txn_id=write_txn_id,
            field_names=changed_fields,
            before=_subset_from_row(before_row, changed_fields),
            after=_subset_from_row(after_row, changed_fields),
        )

    return {
        "write_txn_id": write_txn_id,
        "changed_fields": changed_fields,
        "before": before_row,
        "after": after_row,
    }


async def sanctioned_insert_instance(
    db,
    *,
    values: dict,
    mutation_type: str,
    write_source: str,
    actor: str,
    wrapper_launch_id: str | None = None,
    tracked_fields: set[str] | None = None,
) -> dict:
    """Insert a new row into `instances` (the one physical table).

    Accepts both instance-shaped and transitional legacy-shaped value dicts;
    everything is normalized through legacy_row_to_instance_values
    (legion/profile_name resolve to persona_id, parent_instance_id to a
    chapter commander edge, tts_mode to notification/interaction modes).
    """
    instance_values = _instance_values_from_legacy_row(
        values, await _persona_id_for_row(db, values)
    )
    instance_values = await _prepare_chapter_commander(db, instance_values)
    instance_values = _coerce_insert_name(instance_values)
    if not instance_values.get("id"):
        raise ValueError("sanctioned_insert_instance requires an id")
    columns = [column for column in INSTANCE_COLUMNS if column in instance_values]
    placeholders = ", ".join("?" for _ in columns)
    await db.execute(
        f"INSERT INTO instances ({', '.join(columns)}) VALUES ({placeholders})",
        [instance_values[column] for column in columns],
    )

    tracked = tracked_fields or INSTANCE_MUTATION_FIELDS
    field_names = [field for field in columns if field in tracked]
    after = {field: instance_values.get(field) for field in field_names}
    write_txn_id = str(uuid.uuid4())
    await _append_instance_mutation(
        db,
        instance_id=instance_values["id"],
        mutation_type=mutation_type,
        write_source=write_source,
        actor=actor,
        wrapper_launch_id=wrapper_launch_id or instance_values.get("wrapper_launch_id"),
        write_txn_id=write_txn_id,
        field_names=field_names,
        before=None,
        after=after,
    )
    return {
        "write_txn_id": write_txn_id,
        "changed_fields": field_names,
        "after": after,
    }


def sanctioned_insert_instance_sync(
    db,
    *,
    values: dict,
    mutation_type: str,
    write_source: str,
    actor: str,
    wrapper_launch_id: str | None = None,
    tracked_fields: set[str] | None = None,
) -> dict:
    """Sync twin of sanctioned_insert_instance."""
    instance_values = _instance_values_from_legacy_row(values, _persona_id_for_row_sync(db, values))
    instance_values = _prepare_chapter_commander_sync(db, instance_values)
    instance_values = _coerce_insert_name(instance_values)
    if not instance_values.get("id"):
        raise ValueError("sanctioned_insert_instance_sync requires an id")
    columns = [column for column in INSTANCE_COLUMNS if column in instance_values]
    placeholders = ", ".join("?" for _ in columns)
    db.execute(
        f"INSERT INTO instances ({', '.join(columns)}) VALUES ({placeholders})",
        [instance_values[column] for column in columns],
    )

    tracked = tracked_fields or INSTANCE_MUTATION_FIELDS
    field_names = [field for field in columns if field in tracked]
    after = {field: instance_values.get(field) for field in field_names}
    write_txn_id = str(uuid.uuid4())
    _append_instance_mutation_sync(
        db,
        instance_id=instance_values["id"],
        mutation_type=mutation_type,
        write_source=write_source,
        actor=actor,
        wrapper_launch_id=wrapper_launch_id or instance_values.get("wrapper_launch_id"),
        write_txn_id=write_txn_id,
        field_names=field_names,
        before=None,
        after=after,
    )
    return {
        "write_txn_id": write_txn_id,
        "changed_fields": field_names,
        "after": after,
    }


async def sanctioned_delete_instance(
    db,
    *,
    instance_id: str,
    mutation_type: str,
    write_source: str,
    actor: str,
    wrapper_launch_id: str | None = None,
    tracked_fields: set[str] | None = None,
) -> dict:
    before_row = await _fetch_instance_row(db, instance_id)
    if before_row is None:
        raise LookupError(f"Instance not found: {instance_id}")

    cursor = await db.execute("DELETE FROM instances WHERE id = ?", (instance_id,))
    if cursor.rowcount == 0:
        raise LookupError(f"Sanctioned delete matched no rows for instance {instance_id}")

    tracked = tracked_fields or INSTANCE_MUTATION_FIELDS
    field_names = [field for field in before_row.keys() if field in tracked]
    write_txn_id = str(uuid.uuid4())
    await _append_instance_mutation(
        db,
        instance_id=instance_id,
        mutation_type=mutation_type,
        write_source=write_source,
        actor=actor,
        wrapper_launch_id=wrapper_launch_id or before_row.get("wrapper_launch_id"),
        write_txn_id=write_txn_id,
        field_names=field_names,
        before=_subset_from_row(before_row, field_names),
        after=None,
    )
    return {
        "write_txn_id": write_txn_id,
        "changed_fields": field_names,
        "before": before_row,
    }


async def get_instance_mutations(db, instance_id: str, *, limit: int = 20) -> list[dict]:
    previous_factory = getattr(db, "row_factory", None)
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute(
            """SELECT id, instance_id, mutation_type, write_source, write_txn_id, actor, service_version,
                      wrapper_launch_id, field_names_json, before_json, after_json, created_at
               FROM instance_mutations
               WHERE instance_id = ?
               ORDER BY created_at DESC, id DESC
               LIMIT ?""",
            (instance_id, limit),
        )
        rows = await cursor.fetchall()
    finally:
        db.row_factory = previous_factory

    mutations = []
    for row in rows:
        item = dict(row)
        item["field_names"] = _parse_json_column(item.pop("field_names_json"))
        item["before"] = _parse_json_column(item.pop("before_json"))
        item["after"] = _parse_json_column(item.pop("after_json"))
        mutations.append(item)
    return mutations


async def _get_pending_projection(db, instance_id: str) -> dict:
    pending = {"cc_state": False, "legion_tint": False, "queue_rows": []}
    previous_factory = getattr(db, "row_factory", None)
    db.row_factory = aiosqlite.Row
    try:
        cur = await db.execute(
            "SELECT id, variable, value FROM pane_state_queue WHERE instance_id = ? ORDER BY id DESC LIMIT 10",
            (instance_id,),
        )
        for row in await cur.fetchall():
            item = dict(row)
            pending["queue_rows"].append({"type": "pane_state", **item})
            if item["variable"] == "@CC_STATE":
                pending["cc_state"] = True

        # Legion tint is applied synchronously at lifecycle events (no recolor
        # queue), so there is never a pending tint row to project. legion_tint
        # stays False; a pane_bg/legion mismatch is now a real finding, not a
        # transient queued-but-not-yet-painted state.
    finally:
        db.row_factory = previous_factory
    return pending


def _tmux_query(*args: str) -> str | None:
    try:
        proc = subprocess.run(
            ["tmux", *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
            env={**os.environ, "IMPERIUM_TMUX_RAW": "1"},
        )
    except Exception:
        return None
    text = proc.stdout.strip()
    return text or None


def _read_tmux_projection(tmux_pane: str | None) -> dict:
    if not tmux_pane:
        return {"pane_exists": False, "cc_state": None, "pane_bg": None}
    pane_id = _tmux_query("display-message", "-p", "-t", tmux_pane, "#{pane_id}")
    if not pane_id:
        return {"pane_exists": False, "cc_state": None, "pane_bg": None}
    cc_state = _tmux_query("show-options", "-p", "-v", "-t", tmux_pane, "@CC_STATE")
    pane_bg = _tmux_query("display-message", "-p", "-t", tmux_pane, "#{pane_bg}")
    return {"pane_exists": True, "cc_state": cc_state, "pane_bg": pane_bg}


def _collect_state_findings(row: dict) -> list[dict]:
    findings = []
    if row.get("workflow_state") == "closed" and row.get("status") != "stopped":
        findings.append(
            {
                "category": "state_drift",
                "message": "workflow_state=closed but status is not stopped",
                "fields": ["workflow_state", "status"],
            }
        )
    if row.get("continuity_binding_source") == "dispatch" and not row.get("session_doc_id"):
        findings.append(
            {
                "category": "state_drift",
                "message": "dispatch continuity binding has no session_doc_id",
                "fields": ["continuity_binding_source", "session_doc_id"],
            }
        )
    if row.get("session_doc_policy") == "dispatch_explicit" and not row.get("dispatch_target"):
        findings.append(
            {
                "category": "state_drift",
                "message": "dispatch_explicit policy has no dispatch_target",
                "fields": ["session_doc_policy", "dispatch_target"],
            }
        )
    return findings


def _current_row_matches_sanctioned_fields(
    row: dict, mutations: list[dict]
) -> tuple[bool, list[str], dict]:
    if not mutations:
        return False, [], {}
    latest_by_field = {}
    for mutation in mutations:
        after = mutation.get("after") or {}
        for field, expected in after.items():
            latest_by_field.setdefault(
                field,
                {
                    "expected": expected,
                    "write_txn_id": mutation.get("write_txn_id"),
                },
            )
    mismatches = [
        field for field, meta in latest_by_field.items() if row.get(field) != meta["expected"]
    ]
    return not mismatches, mismatches, latest_by_field


async def _has_official_name_provenance(db, instance_id: str, row: dict) -> bool:
    name = row.get("name")
    if name == DEFAULT_INSTANCE_NAME:
        return True
    cursor = await db.execute(
        """
        SELECT actor, field_names_json, after_json
        FROM instance_mutations
        WHERE instance_id = ?
        ORDER BY created_at DESC, id DESC
        """,
        (instance_id,),
    )
    for mutation in await cursor.fetchall():
        try:
            fields = json.loads(mutation[1] or "[]") or []
            after = json.loads(mutation[2] or "{}") or {}
        except Exception:
            continue
        if (
            "name" in fields
            and after.get("name") == name
            and is_official_instance_name_actor(mutation[0])
        ):
            return True
    return False


async def reconcile_instance(db, instance_id: str) -> dict | None:
    row = await _fetch_instance_row(db, instance_id)
    if row is None:
        return None

    mutations = await get_instance_mutations(db, instance_id, limit=10)
    latest = mutations[0] if mutations else None
    matches_latest, provenance_mismatches, latest_by_field = _current_row_matches_sanctioned_fields(
        row, mutations
    )
    findings = []

    if latest is None or not matches_latest:
        fields = provenance_mismatches or sorted((latest or {}).get("after", {}).keys())
        findings.append(
            {
                "category": "unprovenanced_write",
                "message": "current instance row diverges from the latest sanctioned mutation"
                if latest
                else "instance has no sanctioned mutation history",
                "fields": fields,
                "write_txn_id": latest.get("write_txn_id") if latest else None,
                "field_write_txn_ids": {
                    field: latest_by_field.get(field, {}).get("write_txn_id") for field in fields
                },
            }
        )

    if not await _has_official_name_provenance(db, instance_id, row):
        findings.append(
            {
                "category": "illegal_instance_name",
                "message": "instance name was not produced by an official rename path",
                "fields": ["name"],
                "observed": row.get("name"),
            }
        )

    findings.extend(_collect_state_findings(row))

    # Pane geometry is never stored: resolve it live from the @INSTANCE_ID stamp
    # via the tmuxctl oracle. A miss/dead/unstamped pane returns None and we skip
    # projection checks entirely (no pane → nothing to project against).
    from shared import resolve_instance_pane

    live_pane, _live_role = await resolve_instance_pane(instance_id)
    pending_projection = await _get_pending_projection(db, instance_id)
    observed_projection = _read_tmux_projection(live_pane)
    expected_cc_state = row.get("status")
    expected_pane_bg = await persona_tint_for_instance(db, instance_id)
    projection_findings = []

    if live_pane:
        if not observed_projection["pane_exists"]:
            projection_findings.append(
                {
                    "category": "projection_drift",
                    "message": "instance tmux pane is missing",
                    "fields": ["status"],
                }
            )
        else:
            current_cc_state = observed_projection.get("cc_state")
            if current_cc_state != expected_cc_state:
                projection_findings.append(
                    {
                        "category": "projection_drift",
                        "message": "pane @CC_STATE does not match instance status",
                        "fields": ["status"],
                        "expected": expected_cc_state,
                        "observed": current_cc_state,
                    }
                )
            current_bg = observed_projection.get("pane_bg")
            if current_bg not in {expected_pane_bg, None}:
                projection_findings.append(
                    {
                        "category": "projection_drift",
                        "message": "pane persona tint does not match instance persona",
                        "fields": ["persona_id"],
                        "expected": expected_pane_bg,
                        "observed": current_bg,
                    }
                )

    pending = bool(projection_findings) and (
        pending_projection["cc_state"] or pending_projection["legion_tint"]
    )
    if pending:
        findings.extend(
            {
                **finding,
                "category": "pending_projection",
                "pending": True,
            }
            for finding in projection_findings
        )
    else:
        findings.extend(projection_findings)

    status = "clean"
    categories = {finding["category"] for finding in findings}
    if "illegal_instance_name" in categories:
        status = "illegal_instance_name"
    elif "state_drift" in categories:
        status = "state_drift"
    elif "projection_drift" in categories:
        status = "projection_drift"
    elif "pending_projection" in categories:
        status = "pending_projection"
    elif "unprovenanced_write" in categories:
        status = "unprovenanced_write"

    return {
        "instance_id": instance_id,
        "status": status,
        "current_row": {
            field: row.get(field) for field in sorted(INSTANCE_MUTATION_FIELDS | {"id"})
        },
        "latest_sanctioned_mutation": latest,
        "findings": findings,
        "pending_projection": pending_projection,
        "observed_projection": observed_projection,
        "expected_projection": {
            "cc_state": expected_cc_state,
            "pane_bg": expected_pane_bg,
        },
        "last_write_txn_id": latest.get("write_txn_id") if latest else None,
        "last_sanctioned_write_source": latest.get("write_source") if latest else None,
        "last_sanctioned_write_time": latest.get("created_at") if latest else None,
        "recent_mutations": mutations,
    }
