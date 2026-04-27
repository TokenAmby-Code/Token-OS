import json
import logging
import sqlite3
import subprocess
import uuid
from datetime import datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger("token_api")

SERVICE_VERSION_FALLBACK = "0.1.0"
RECONCILIATION_SUSPICIOUS = {
    "unprovenanced_write",
    "state_drift",
    "projection_drift",
}

INSTANCE_MUTATION_FIELDS = {
    "tab_name",
    "status",
    "last_activity",
    "legion",
    "synced",
    "input_lock",
    "tts_mode",
    "tts_voice",
    "notification_sound",
    "session_doc_id",
    "session_doc_policy",
    "continuity_binding_source",
    "workflow_state",
    "workflow_updated_at",
    "dispatch_target",
    "dispatch_window",
    "dispatch_mode",
    "dispatch_slot",
    "dispatch_session_doc_path",
    "target_working_dir",
    "launch_mode",
    "wrapper_launch_id",
    "tmux_pane",
    "working_dir",
    "device_id",
    "pid",
    "stopped_at",
    "instance_type",
    "follow_up_sop",
    "zealotry",
    "victory_at",
    "victory_reason",
    "discord_hosted",
    "discord_channel",
    "transplant_target_session",
    "closure_surface",
    "closure_required",
    "stop_allowed",
    "next_required_action",
    "next_action_owner",
    "primarch",
    "transplant_expected",
}

LEGION_PANE_COLORS = {
    "custodes": "#302800",
    "mechanicus": "#300808",
    "civic": "#083010",
    "astartes": "default",
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
    previous_factory = getattr(db, "row_factory", None)
    db.row_factory = aiosqlite.Row
    try:
        cursor = await db.execute("SELECT * FROM claude_instances WHERE id = ?", (instance_id,))
        row = await cursor.fetchone()
    finally:
        db.row_factory = previous_factory
    return dict(row) if row else None


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
        cursor = db.execute("SELECT * FROM claude_instances WHERE id = ?", (instance_id,))
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
    before_row = await _fetch_instance_row(db, instance_id)
    if before_row is None:
        raise LookupError(f"Instance not found: {instance_id}")

    tracked = tracked_fields or INSTANCE_MUTATION_FIELDS
    changed_fields = []
    for field, value in updates.items():
        if field in tracked and before_row.get(field) != value:
            changed_fields.append(field)

    assignments = ", ".join(f"{field} = ?" for field in updates)
    params = list(updates.values())
    clause = where_clause or "id = ?"
    params.extend(list(where_params or (instance_id,)))
    cursor = await db.execute(f"UPDATE claude_instances SET {assignments} WHERE {clause}", params)

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
    before_row = _fetch_instance_row_sync(db, instance_id)
    if before_row is None:
        raise LookupError(f"Instance not found: {instance_id}")

    tracked = tracked_fields or INSTANCE_MUTATION_FIELDS
    changed_fields = []
    for field, value in updates.items():
        if field in tracked and before_row.get(field) != value:
            changed_fields.append(field)

    assignments = ", ".join(f"{field} = ?" for field in updates)
    params = list(updates.values())
    clause = where_clause or "id = ?"
    params.extend(list(where_params or (instance_id,)))
    cursor = db.execute(f"UPDATE claude_instances SET {assignments} WHERE {clause}", params)

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
    columns = list(values.keys())
    placeholders = ", ".join("?" for _ in columns)
    await db.execute(
        f"INSERT INTO claude_instances ({', '.join(columns)}) VALUES ({placeholders})",
        [values[column] for column in columns],
    )

    tracked = tracked_fields or INSTANCE_MUTATION_FIELDS
    field_names = [field for field in columns if field in tracked]
    after = {field: values.get(field) for field in field_names}
    write_txn_id = str(uuid.uuid4())
    await _append_instance_mutation(
        db,
        instance_id=values["id"],
        mutation_type=mutation_type,
        write_source=write_source,
        actor=actor,
        wrapper_launch_id=wrapper_launch_id or values.get("wrapper_launch_id"),
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

    cursor = await db.execute("DELETE FROM claude_instances WHERE id = ?", (instance_id,))
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
            "SELECT id, variable, value, tmux_pane FROM pane_state_queue WHERE instance_id = ? ORDER BY id DESC LIMIT 10",
            (instance_id,),
        )
        for row in await cur.fetchall():
            item = dict(row)
            pending["queue_rows"].append({"type": "pane_state", **item})
            if item["variable"] == "@CC_STATE":
                pending["cc_state"] = True

        cur = await db.execute(
            "SELECT id, legion, tmux_pane FROM pane_recolor_queue WHERE instance_id = ? ORDER BY id DESC LIMIT 10",
            (instance_id,),
        )
        for row in await cur.fetchall():
            item = dict(row)
            pending["queue_rows"].append({"type": "pane_recolor", **item})
            pending["legion_tint"] = True
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

    findings.extend(_collect_state_findings(row))

    pending_projection = await _get_pending_projection(db, instance_id)
    observed_projection = _read_tmux_projection(row.get("tmux_pane"))
    expected_cc_state = row.get("status")
    expected_pane_bg = LEGION_PANE_COLORS.get(row.get("legion") or "astartes", "default")
    projection_findings = []

    if row.get("tmux_pane"):
        if not observed_projection["pane_exists"]:
            projection_findings.append(
                {
                    "category": "projection_drift",
                    "message": "instance tmux pane is missing",
                    "fields": ["tmux_pane"],
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
                        "message": "pane legion tint does not match instance legion",
                        "fields": ["legion"],
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
    if "state_drift" in categories:
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
