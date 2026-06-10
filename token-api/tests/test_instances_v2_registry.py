"""Canonical instance registry v2 invariants."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta

import pytest

FINAL_INSTANCE_COLUMNS = [
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

REMOVED = {
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


def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _persona(conn, slug):
    return conn.execute("SELECT id FROM personas WHERE slug = ?", (slug,)).fetchone()[0]


def _insert_instance(conn, **overrides):
    now = datetime.now().isoformat()
    values = {
        "id": str(uuid.uuid4()),
        "name": "inst",
        "engine": "claude",
        "working_dir": "/tmp",
        "device_id": "Mac-Mini",
        "origin_type": "local",
        "commander_type": "emperor",
        "commander_id": None,
        "status": "idle",
        "created_at": now,
        "last_activity": now,
        "stopped_at": None,
        "archived_at": None,
        "persona_id": None,
        "rank": "astartes",
        "session_doc_id": None,
        "continuity_binding_source": None,
        "wrapper_launch_id": None,
        "automated": 0,
        "notification_mode": "verbose",
        "interaction_mode": "text",
        "golden_throne": None,
    }
    values.update(overrides)
    cols = list(values)
    conn.execute(
        f"INSERT INTO instances ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [values[c] for c in cols],
    )
    return values["id"]


def test_instances_contains_only_final_columns(app_env):
    conn = _conn(app_env.db_path)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(instances)")]
    conn.close()
    assert cols == FINAL_INSTANCE_COLUMNS
    assert not (set(cols) & REMOVED)


def test_supporting_tables_exist_and_seed_personas(app_env):
    conn = _conn(app_env.db_path)
    tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    slugs = {r["slug"] for r in conn.execute("SELECT slug FROM personas")}
    conn.close()
    assert {"personas", "golden_throne", "aspirants"} <= tables
    assert {
        "custodes",
        "fabricator-general",
        "administratum",
        "ultramarines",
        "salamanders",
        "space-wolves",
    } <= slugs
    assert "emperor" not in slugs
    assert "chapter-master" not in slugs


def test_canonical_mirror_maps_legacy_row_to_final_fields(app_env):
    import instance_mutation

    now = datetime.now().isoformat()
    iid = str(uuid.uuid4())
    values = {
        "id": iid,
        "session_id": iid,
        "tab_name": "clear-slate",
        "working_dir": "/tmp",
        "origin_type": "local",
        "device_id": "Mac-Mini",
        "status": "processing",
        "registered_at": now,
        "last_activity": now,
        "profile_name": "ultramarines",
        "tts_mode": "voice-chat",
        "tmux_pane": "%999",
        "pane_label": "mechanicus:7",
        "dispatch_target": "mechanicus:7",
        "dispatch_window": "main:mechanicus",
        "dispatch_slot": "7",
    }
    canonical = instance_mutation._canonical_instance_values(values, persona_id=42)

    assert canonical["id"] == iid
    assert canonical["name"] == "clear-slate"
    assert canonical["status"] == "working"
    assert canonical["persona_id"] == 42
    assert canonical["interaction_mode"] == "voice_chat"
    assert not (set(canonical) & REMOVED)


async def test_sanctioned_insert_fails_loud_on_tmux_fields(app_env):
    import aiosqlite

    from instance_mutation import sanctioned_insert_instance

    now = datetime.now().isoformat()
    iid = str(uuid.uuid4())
    async with aiosqlite.connect(app_env.db_path) as db:
        with pytest.raises(ValueError, match="must not persist tmux/runtime ids"):
            await sanctioned_insert_instance(
                db,
                values={
                    "id": iid,
                    "session_id": iid,
                    "tab_name": "no-runtime-storage",
                    "working_dir": "/tmp",
                    "origin_type": "local",
                    "device_id": "Mac-Mini",
                    "status": "idle",
                    "registered_at": now,
                    "last_activity": now,
                    "tmux_pane": "%42",
                },
                mutation_type="instance_registered",
                write_source="test",
                actor="test",
            )


def test_active_persona_lock_is_rank_based(app_env):
    conn = _conn(app_env.db_path)
    pid = _persona(conn, "ultramarines")
    active = _insert_instance(conn, id="active", persona_id=pid, rank="astartes")
    retired = _insert_instance(conn, id="retired", persona_id=pid, rank="retired", status="stopped")
    rows = conn.execute(
        "SELECT id FROM instances WHERE persona_id = ? AND rank != 'retired'", (pid,)
    ).fetchall()
    conn.close()
    assert [r["id"] for r in rows] == [active]
    assert retired == "retired"


def test_singleton_reregister_retires_previous_active(app_env):
    conn = _conn(app_env.db_path)
    pid = _persona(conn, "custodes")
    _insert_instance(conn, id="old-custodes", persona_id=pid, rank="overseer")
    _insert_instance(conn, id="new-custodes", persona_id=pid, rank="overseer")
    rows = conn.execute(
        "SELECT id, rank, status FROM instances WHERE persona_id = ? ORDER BY id", (pid,)
    ).fetchall()
    conn.close()
    assert [(r["id"], r["rank"], r["status"]) for r in rows] == [
        ("new-custodes", "overseer", "idle"),
        ("old-custodes", "retired", "stopped"),
    ]


def test_chapter_commander_must_share_persona(app_env):
    conn = _conn(app_env.db_path)
    ultra = _persona(conn, "ultramarines")
    salamanders = _persona(conn, "salamanders")
    _insert_instance(conn, id="ultra-boss", persona_id=ultra, rank="astartes")
    with pytest.raises(sqlite3.IntegrityError, match="share persona_id"):
        _insert_instance(
            conn,
            id="bad-child",
            persona_id=salamanders,
            commander_type="chapter",
            commander_id="ultra-boss",
        )
    _insert_instance(
        conn,
        id="good-child",
        persona_id=ultra,
        commander_type="chapter",
        commander_id="ultra-boss",
    )
    conn.close()


def test_retiring_chapter_commander_retires_children(app_env):
    conn = _conn(app_env.db_path)
    pid = _persona(conn, "salamanders")
    _insert_instance(conn, id="boss", persona_id=pid)
    _insert_instance(
        conn, id="child", persona_id=pid, commander_type="chapter", commander_id="boss"
    )
    conn.execute("UPDATE instances SET rank = 'retired' WHERE id = 'boss'")
    row = conn.execute("SELECT rank, status FROM instances WHERE id = 'child'").fetchone()
    conn.close()
    assert (row["rank"], row["status"]) == ("retired", "stopped")


def test_archived_requires_retired_rank(app_env):
    conn = _conn(app_env.db_path)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_instance(conn, id="bad-archive", status="archived", rank="astartes")
    _insert_instance(conn, id="good-archive", status="archived", rank="retired")
    conn.close()


def test_derived_cockpit_labels_not_stored(app_env):
    from instance_registry import derived_cockpit_label

    stale = (datetime.now() - timedelta(minutes=31)).isoformat()
    assert derived_cockpit_label({"status": "working", "automated": 1}) == "interred"
    assert derived_cockpit_label({"status": "working", "automated": 0}) == "commanded"
    assert derived_cockpit_label({"status": "idle", "last_activity": stale}) == "languishing"
    conn = _conn(app_env.db_path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(instances)")}
    conn.close()
    assert "cockpit_label" not in cols


def test_persona_commander_and_golden_throne_triggers(app_env):
    conn = _conn(app_env.db_path)
    ultra = _persona(conn, "ultramarines")
    custodes = _persona(conn, "custodes")
    with pytest.raises(sqlite3.IntegrityError, match="persona commander_id"):
        _insert_instance(
            conn,
            id="bad-persona-commander",
            persona_id=ultra,
            commander_type="persona",
            commander_id="999999",
        )
    _insert_instance(
        conn,
        id="good-persona-commander",
        persona_id=ultra,
        commander_type="persona",
        commander_id=str(custodes),
    )
    with pytest.raises(sqlite3.IntegrityError, match="golden_throne"):
        _insert_instance(conn, id="bad-gt", persona_id=ultra, golden_throne="999999")
    gt_id = conn.execute("INSERT INTO golden_throne DEFAULT VALUES RETURNING id").fetchone()[0]
    _insert_instance(conn, id="good-gt", persona_id=ultra, golden_throne=str(gt_id))
    _insert_instance(conn, id="sync-gt", persona_id=ultra, golden_throne="sync")
    conn.close()


def test_session_start_dispatch_targets_bind_persona_commanders(app_env):
    import asyncio
    import sys

    hooks = sys.modules["routes.hooks"]

    async def run():
        result = await hooks.handle_session_start(
            {
                "session_id": "legion-target-worker",
                "cwd": "/tmp",
                "env": {
                    "TOKEN_API_ENGINE": "codex",
                    "TOKEN_API_LAUNCHER": "dispatch",
                    "TOKEN_API_DISPATCH_TARGET": "legion:new",
                },
            }
        )
        assert result["success"] is True

    asyncio.run(run())
    conn = _conn(app_env.db_path)
    row = conn.execute(
        """SELECT i.commander_type, p.slug AS commander_slug
             FROM instances i
             LEFT JOIN personas p ON p.id = i.commander_id
            WHERE i.id = 'legion-target-worker'"""
    ).fetchone()
    conn.close()
    assert (row["commander_type"], row["commander_slug"]) == ("persona", "custodes")
