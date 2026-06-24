"""Instance registry invariants."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta

import pytest

EXPECTED_INSTANCE_COLUMNS = [
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


def test_instances_contains_only_expected_columns(app_env):
    from instance_registry import INSTANCE_COLUMNS

    conn = _conn(app_env.db_path)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(instances)")]
    conn.close()
    assert cols == INSTANCE_COLUMNS
    assert not (
        set(cols)
        & {
            "tab_name",
            "session_id",
            "source_ip",
            "pid",
            "legion",
            "primarch",
            "profile_name",
            "tts_mode",
            "parent_instance_id",
        }
    )


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


def test_instance_normalizer_maps_legacy_row_to_expected_fields(app_env):
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
    normalized = instance_mutation._instance_values_from_legacy_row(values, persona_id=42)

    assert normalized["id"] == iid
    assert normalized["name"] == "clear-slate"
    assert normalized["status"] == "working"
    assert normalized["persona_id"] == 42
    assert normalized["interaction_mode"] == "voice_chat"
    assert not (
        set(normalized)
        & {
            "tab_name",
            "session_id",
            "source_ip",
            "pid",
            "legion",
            "primarch",
            "profile_name",
            "tts_mode",
            "parent_instance_id",
            # pane ids are exterminated — the normalizer never projects them
            "tmux_pane",
            "pane_label",
        }
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


def test_singleton_reregister_retires_previous_stale(app_env: object) -> None:
    conn = _conn(app_env.db_path)
    pid = _persona(conn, "custodes")
    _insert_instance(conn, id="old-custodes", persona_id=pid, rank="overseer", status="stopped")
    _insert_instance(conn, id="new-custodes", persona_id=pid, rank="overseer")
    rows = conn.execute(
        "SELECT id, rank, status FROM instances WHERE persona_id = ? ORDER BY id", (pid,)
    ).fetchall()
    conn.close()
    assert [(r["id"], r["rank"], r["status"]) for r in rows] == [
        ("new-custodes", "overseer", "idle"),
        ("old-custodes", "retired", "stopped"),
    ]


def test_emperor_singleton_insert_cannot_steal_working_incumbent_regardless_commander_type(
    app_env: object,
) -> None:
    """RED: an emperor-path singleton insert must not retire/replace a working
    incumbent row for the same singleton persona, even if that incumbent carries
    a non-emperor commander_type. The live apply/migration gate owns deploying
    this trigger to agents.db; this pins only the in-memory/test schema.
    """
    conn = _conn(app_env.db_path)
    pid = _persona(conn, "custodes")
    commander_pid = _persona(conn, "ultramarines")
    _insert_instance(conn, id="chapter-parent", persona_id=None, status="working")

    cases = [
        ("incumbent-emperor", "emperor", None),
        ("incumbent-persona", "persona", str(commander_pid)),
        ("incumbent-chapter", "chapter", "chapter-parent"),
    ]
    for incumbent_id, commander_type, commander_id in cases:
        conn.execute("DELETE FROM instances WHERE id != 'chapter-parent'")
        _insert_instance(
            conn,
            id=incumbent_id,
            persona_id=pid,
            rank="overseer",
            status="working",
            commander_type=commander_type,
            commander_id=commander_id,
        )

        with pytest.raises(sqlite3.IntegrityError, match="live singleton incumbent exists"):
            _insert_instance(
                conn,
                id=f"thief-{commander_type}",
                persona_id=pid,
                rank="overseer",
                status="idle",
                commander_type="emperor",
            )

        rows = conn.execute(
            "SELECT id, rank, status, commander_type FROM instances WHERE persona_id = ? ORDER BY id",
            (pid,),
        ).fetchall()
        assert [(r["id"], r["rank"], r["status"], r["commander_type"]) for r in rows] == [
            (incumbent_id, "overseer", "working", commander_type)
        ]
    conn.close()


def test_chapter_commander_requires_active_parent_not_shared_persona(app_env):
    conn = _conn(app_env.db_path)
    ultra = _persona(conn, "ultramarines")
    salamanders = _persona(conn, "salamanders")
    _insert_instance(conn, id="ultra-boss", persona_id=ultra, rank="astartes")

    # Chapter edges express control/parentage, not identity.  A dispatched worker
    # may keep an explicitly assigned persona that differs from its commander.
    _insert_instance(
        conn,
        id="salamander-child",
        persona_id=salamanders,
        commander_type="chapter",
        commander_id="ultra-boss",
    )

    conn.execute("UPDATE instances SET rank = 'retired' WHERE id = 'ultra-boss'")
    with pytest.raises(sqlite3.IntegrityError, match="chapter commander must be active"):
        _insert_instance(
            conn,
            id="orphan-child",
            persona_id=salamanders,
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


def test_rank_stamp_trigger_overrides_astartes_clobber(app_env):
    """A custodes row inserted at rank='astartes' (the mirror's column default) is
    stamped back to its registry default_rank='overseer' by the AFTER INSERT trigger."""
    conn = _conn(app_env.db_path)
    custodes = _persona(conn, "custodes")
    _insert_instance(conn, id="clobbered", persona_id=custodes, rank="astartes")
    row = conn.execute("SELECT rank FROM instances WHERE id = 'clobbered'").fetchone()
    conn.close()
    assert row["rank"] == "overseer"


def test_rank_stamp_collapses_duplicate_custodes_to_one_overseer(app_env):
    """Two custodes inserted at the astartes default collapse to exactly one
    non-retired row at rank='overseer' (singleton guard + rank stamp together)."""
    conn = _conn(app_env.db_path)
    custodes = _persona(conn, "custodes")
    _insert_instance(conn, id="cust-a", persona_id=custodes, rank="astartes")
    _insert_instance(conn, id="cust-b", persona_id=custodes, rank="astartes")
    live = conn.execute(
        "SELECT id, rank FROM instances WHERE persona_id = ? AND rank != 'retired'",
        (custodes,),
    ).fetchall()
    conn.close()
    assert [(r["id"], r["rank"]) for r in live] == [("cust-b", "overseer")]


def test_mirror_upsert_clobber_is_restamped_to_overseer(app_env):
    """The canonical mirror upserts with ON CONFLICT DO UPDATE SET rank=excluded.rank,
    carrying the astartes default on every custodes mutation. The AFTER UPDATE stamp
    must re-stamp the surviving row back to overseer."""
    conn = _conn(app_env.db_path)
    custodes = _persona(conn, "custodes")
    _insert_instance(conn, id="cust-live", persona_id=custodes, rank="overseer")
    assert (
        conn.execute("SELECT rank FROM instances WHERE id='cust-live'").fetchone()[0] == "overseer"
    )

    now = datetime.now().isoformat()
    # Mimic mirror_instance_to_canonical's upsert clobbering rank back to 'astartes'.
    conn.execute(
        """INSERT INTO instances
               (id, name, device_id, origin_type, commander_type, status,
                created_at, last_activity, persona_id, rank, automated,
                notification_mode, interaction_mode)
           VALUES (?, 'cust', 'Mac-Mini', 'local', 'emperor', 'working',
                   ?, ?, ?, 'astartes', 0, 'verbose', 'text')
           ON CONFLICT(id) DO UPDATE SET rank = excluded.rank, status = excluded.status""",
        ("cust-live", now, now, custodes),
    )
    row = conn.execute("SELECT rank, status FROM instances WHERE id='cust-live'").fetchone()
    conn.close()
    assert (row["rank"], row["status"]) == ("overseer", "working")


def test_reconciliation_collapses_pretrigger_astartes_rows(app_env):
    """Rows that predate the rank-stamp trigger (inserted during the trigger-less
    bulk rebuild) are reconciled at init: the most-recently-active non-retired row
    per non-astartes persona is stamped to its default_rank and the rest retire."""
    import asyncio

    import aiosqlite

    import db_schema

    async def run():
        async with aiosqlite.connect(app_env.db_path) as db:
            custodes = (
                await (await db.execute("SELECT id FROM personas WHERE slug='custodes'")).fetchone()
            )[0]
            # Drop the collapse/stamp triggers to mimic the trigger-less rebuild path.
            for trg in (
                "trg_instances_stamp_persona_rank",
                "trg_instances_stamp_persona_rank_update",
                "trg_instances_singleton_guard",
                "trg_instances_singleton_guard_update",
            ):
                await db.execute(f"DROP TRIGGER IF EXISTS {trg}")
            for iid, ts in (
                ("c-old", "2025-01-01T00:00:00"),
                ("c-new", "2025-06-01T00:00:00"),
                ("c-mid", "2025-03-01T00:00:00"),
            ):
                await db.execute(
                    """INSERT INTO instances
                           (id, name, device_id, origin_type, commander_type, status,
                            created_at, last_activity, persona_id, rank, automated,
                            notification_mode, interaction_mode)
                       VALUES (?, 'cust', 'Mac-Mini', 'local', 'emperor', 'idle',
                               ?, ?, ?, 'astartes', 0, 'verbose', 'text')""",
                    (iid, ts, ts, custodes),
                )
            await db.commit()
            # Re-running ensure recreates the triggers and runs the reconciliation UPDATE.
            await db_schema._ensure_instances(db)
            await db.commit()
            db.row_factory = aiosqlite.Row
            rows = [
                dict(r)
                for r in await (
                    await db.execute(
                        "SELECT id, rank FROM instances WHERE persona_id = ? AND rank != 'retired'",
                        (custodes,),
                    )
                ).fetchall()
            ]
        return rows

    rows = asyncio.run(run())
    assert [(r["id"], r["rank"]) for r in rows] == [("c-new", "overseer")]


def test_resolve_live_persona_instance_finds_synced_less_custodes(app_env):
    """Regression: a Custodes row with NO sync marker (just persona=custodes +
    rank=overseer) is still resolved by personas.resolve_live_persona_instance — and
    a retired/stopped custodes is not."""
    import asyncio

    import aiosqlite

    import personas

    conn = _conn(app_env.db_path)
    custodes = _persona(conn, "custodes")
    _insert_instance(conn, id="retired-cust", persona_id=custodes, rank="retired", status="stopped")
    _insert_instance(conn, id="live-cust", persona_id=custodes, rank="overseer", status="working")
    conn.commit()
    conn.close()

    async def run():
        async with aiosqlite.connect(app_env.db_path) as db:
            return await personas.resolve_live_persona_instance(db, "custodes")

    resolved = asyncio.run(run())
    assert resolved is not None
    assert resolved["id"] == "live-cust"
    assert resolved["rank"] == "overseer"
    assert resolved["status"] == "working"


def test_resolve_live_persona_instance_excludes_chapter_children(app_env):
    """Regression: a custodes *chapter* child (subagent sharing the persona_id),
    even one that is more recently active and stamped overseer, must NOT shadow the
    overseer singleton. The resolver's ``commander_type != 'chapter'`` filter is the
    objective-critical guard (live archive.db had four custodes chapter children
    under the overseer)."""
    import asyncio

    import aiosqlite

    import personas

    conn = _conn(app_env.db_path)
    custodes = _persona(conn, "custodes")
    # The overseer singleton, active but with an OLDER last_activity.
    _insert_instance(
        conn,
        id="live-cust",
        persona_id=custodes,
        rank="overseer",
        status="working",
        last_activity="2025-01-01T00:00:00",
    )
    # A chapter child commanded by the overseer, more recently active and even
    # stamped overseer — it would win on both rank and recency if not excluded.
    _insert_instance(
        conn,
        id="chapter-cust",
        persona_id=custodes,
        commander_type="chapter",
        commander_id="live-cust",
        rank="overseer",
        status="working",
        last_activity="2025-12-31T00:00:00",
    )
    conn.commit()
    conn.close()

    async def run():
        async with aiosqlite.connect(app_env.db_path) as db:
            return await personas.resolve_live_persona_instance(db, "custodes")

    resolved = asyncio.run(run())
    assert resolved is not None
    assert resolved["id"] == "live-cust"


def test_session_start_dispatch_targets_bind_persona_commanders(app_env):
    import asyncio
    import sys

    hooks = sys.modules["routes.hooks"]

    async def run():
        result = await hooks.handle_session_start(
            {
                "session_id": "mechanicus-target-worker",
                "cwd": "/tmp",
                "env": {
                    "TOKEN_API_ENGINE": "codex",
                    "TOKEN_API_LAUNCHER": "dispatch",
                    "TOKEN_API_DISPATCH_TARGET": "mechanicus:new",
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
            WHERE i.id = 'mechanicus-target-worker'"""
    ).fetchone()
    conn.close()
    # Mechanicus is the single merged worker stack; the Fabricator-General anchors
    # it and commands every freshly dispatched worker (the retired legion page's
    # custodes orchestrator no longer commands workers).
    assert (row["commander_type"], row["commander_slug"]) == ("persona", "fabricator-general")


def test_banish_chapter_instance_moves_to_black_shields_without_retiring(app_env):
    import asyncio

    conn = _conn(app_env.db_path)
    try:
        blood_angels = _persona(conn, "blood-angels")
        commander = _insert_instance(conn, id="commander", persona_id=blood_angels, rank="overseer")
        child = _insert_instance(
            conn,
            id="child",
            persona_id=blood_angels,
            commander_type="chapter",
            commander_id=commander,
            rank="astartes",
            status="idle",
        )
        conn.commit()
    finally:
        conn.close()

    result = asyncio.run(app_env.main.banish_instance(child))

    assert result == {"instance_id": child, "status": "stopped", "persona": "black-shields"}
    conn = _conn(app_env.db_path)
    try:
        row = conn.execute(
            """SELECT i.status, i.rank, p.slug, i.commander_type, i.commander_id
               FROM instances i JOIN personas p ON p.id = i.persona_id
               WHERE i.id = ?""",
            (child,),
        ).fetchone()
        assert tuple(row) == ("stopped", "astartes", "black-shields", "emperor", None)
    finally:
        conn.close()


def test_retire_instance_sets_rank_retired_and_stopped(app_env):
    import asyncio

    conn = _conn(app_env.db_path)
    try:
        iid = _insert_instance(conn, id="retire-me", status="idle", rank="astartes")
        conn.commit()
    finally:
        conn.close()

    result = asyncio.run(app_env.main.retire_instance(iid))

    assert result == {"instance_id": iid, "rank": "retired", "status": "stopped"}
    conn = _conn(app_env.db_path)
    try:
        row = conn.execute(
            "SELECT status, rank, golden_throne FROM instances WHERE id = ?", (iid,)
        ).fetchone()
        assert tuple(row) == ("stopped", "retired", None)
    finally:
        conn.close()
