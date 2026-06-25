"""Mechanicus-worker persona + the keystone biconditional invariant.

Decree: Terra/Ultramar/Mechanicus Worker Persona.md. Covers the voiceless shared
coat seed, the self-enforcing write-time triggers (commander -> FG  <=>  persona =
mechanicus-worker, plus mechanicus-worker => automated), the singleton exemption,
and the read-time validation guard.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime

import aiosqlite
import pytest

from personas import (
    assign_astartes_persona,
    selectable_astartes_personas,
    validate_mechanicus_invariant,
    validate_mechanicus_invariant_sync,
)

MECH_TRIGGERS = (
    "trg_instances_mech_commander_to_persona",
    "trg_instances_mech_commander_to_persona_update",
    "trg_instances_mech_persona_to_commander",
    "trg_instances_mech_persona_to_commander_update",
)


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
        "origin_type": "dispatch",
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
    conn.commit()
    return values["id"]


def _row(conn, iid):
    return conn.execute(
        "SELECT commander_type, commander_id, persona_id, automated, rank "
        "FROM instances WHERE id = ?",
        (iid,),
    ).fetchone()


# ── The seed ────────────────────────────────────────────────────────────────


def test_mechanicus_worker_seeded_voiceless_and_shared(app_env):
    conn = _conn(app_env.db_path)
    row = conn.execute("SELECT * FROM personas WHERE slug = 'mechanicus-worker'").fetchone()
    conn.close()
    assert row is not None, "mechanicus-worker persona must be seeded"
    assert row["tts_voice"] is None, "voiceless: must never draw a chapter voice"
    assert row["tts_rate"] is None
    assert row["notification_sound"] is None
    assert row["assignment_pool"] is None, "not in the astartes voice mutex"
    assert row["assignment_order"] is None
    # default_rank='astartes' is the deliberate "non-singleton / shared" encoding.
    assert row["default_rank"] == "astartes"
    assert row["pane_tint"] == "#182126", "uniform mechanicus steel tint"
    assert row["chip_color"] == "#8b1a1a"


async def test_mechanicus_worker_never_drawn_into_voice_pool(app_env):
    async with aiosqlite.connect(app_env.db_path) as db:
        db.row_factory = aiosqlite.Row
        # No active instances => first primary astartes is offered, never mechanicus.
        assigned, _ = await assign_astartes_persona(db)
        assert assigned["slug"] != "mechanicus-worker"
        selectable = {p["slug"] for p in await selectable_astartes_personas(db)}
        assert "mechanicus-worker" not in selectable


# ── Write-time force: commander -> FG  =>  persona = mechanicus-worker ─────────


def test_commander_fg_forces_mechanicus_persona_on_insert(app_env):
    conn = _conn(app_env.db_path)
    fg = _persona(conn, "fabricator-general")
    mech = _persona(conn, "mechanicus-worker")
    iid = _insert_instance(conn, commander_type="persona", commander_id=fg)
    ct, ci, pid, automated, _ = _row(conn, iid)
    conn.close()
    assert (ct, ci) == ("persona", fg)
    assert pid == mech, "dispatch sets commander=FG; the trigger assigns the coat"
    assert automated == 1


def test_commander_fg_rewrites_chapter_voice_category_error(app_env):
    # A silent FG-commanded worker holding a chapter (voiced) lock is a category
    # error; the trigger strips the voice slot and reassigns the voiceless coat.
    conn = _conn(app_env.db_path)
    fg = _persona(conn, "fabricator-general")
    mech = _persona(conn, "mechanicus-worker")
    ultra = _persona(conn, "ultramarines")
    iid = _insert_instance(conn, commander_type="persona", commander_id=fg, persona_id=ultra)
    _, _, pid, automated, _ = _row(conn, iid)
    conn.close()
    assert pid == mech
    assert automated == 1


# ── Write-time force: persona = mechanicus-worker  =>  commander -> FG ─────────


def test_mechanicus_persona_forces_fg_commander_on_insert(app_env):
    conn = _conn(app_env.db_path)
    fg = _persona(conn, "fabricator-general")
    mech = _persona(conn, "mechanicus-worker")
    iid = _insert_instance(conn, persona_id=mech)  # commander defaults to emperor
    ct, ci, pid, automated, _ = _row(conn, iid)
    conn.close()
    assert (ct, ci) == ("persona", fg)
    assert pid == mech
    assert automated == 1, "secondary invariant: mechanicus-worker => automated"


def test_steady_state_row_is_not_churned(app_env):
    conn = _conn(app_env.db_path)
    fg = _persona(conn, "fabricator-general")
    mech = _persona(conn, "mechanicus-worker")
    iid = _insert_instance(
        conn, commander_type="persona", commander_id=fg, persona_id=mech, automated=1
    )
    ct, ci, pid, automated, _ = _row(conn, iid)
    conn.close()
    assert (ct, ci, pid, automated) == ("persona", fg, mech, 1)


# ── Update paths force both directions ────────────────────────────────────────


def test_update_commander_to_fg_forces_mechanicus(app_env):
    conn = _conn(app_env.db_path)
    fg = _persona(conn, "fabricator-general")
    mech = _persona(conn, "mechanicus-worker")
    iid = _insert_instance(conn)  # plain emperor/no-persona row
    conn.execute(
        "UPDATE instances SET commander_type='persona', commander_id=? WHERE id=?", (fg, iid)
    )
    conn.commit()
    _, _, pid, automated, _ = _row(conn, iid)
    conn.close()
    assert pid == mech
    assert automated == 1


def test_update_persona_to_mechanicus_forces_fg_commander(app_env):
    conn = _conn(app_env.db_path)
    fg = _persona(conn, "fabricator-general")
    mech = _persona(conn, "mechanicus-worker")
    iid = _insert_instance(conn)
    conn.execute("UPDATE instances SET persona_id=? WHERE id=?", (mech, iid))
    conn.commit()
    ct, ci, pid, automated, _ = _row(conn, iid)
    conn.close()
    assert (ct, ci, pid, automated) == ("persona", fg, mech, 1)


# ── Shared / non-exclusive: many instances hold the coat at once ──────────────


def test_mechanicus_worker_is_shared_not_singleton(app_env):
    conn = _conn(app_env.db_path)
    fg = _persona(conn, "fabricator-general")
    mech = _persona(conn, "mechanicus-worker")
    for _ in range(5):
        _insert_instance(conn, commander_type="persona", commander_id=fg)
    live = conn.execute(
        "SELECT COUNT(*) FROM instances WHERE persona_id=? AND rank!='retired'", (mech,)
    ).fetchone()[0]
    conn.close()
    assert live == 5, "no lock-and-retire: every worker keeps the coat simultaneously"


def test_retired_worker_keeps_its_coat(app_env):
    conn = _conn(app_env.db_path)
    fg = _persona(conn, "fabricator-general")
    mech = _persona(conn, "mechanicus-worker")
    iid = _insert_instance(conn, commander_type="persona", commander_id=fg)
    conn.execute("UPDATE instances SET rank='retired', status='stopped' WHERE id=?", (iid,))
    conn.commit()
    ct, ci, pid, _, rank = _row(conn, iid)
    conn.close()
    assert (ct, ci, pid, rank) == ("persona", fg, mech, "retired")


# ── The singletons are exempt (FG is not its own commander) ───────────────────


def test_fg_own_row_is_exempt(app_env):
    conn = _conn(app_env.db_path)
    fg = _persona(conn, "fabricator-general")
    iid = _insert_instance(conn, persona_id=fg, rank="overseer")
    ct, ci, pid, _, _ = _row(conn, iid)
    conn.close()
    assert (ct, ci, pid) == ("emperor", None, fg), "FG singleton must not be rewritten"


def test_admin_singleton_exempt_even_if_commanded_by_fg(app_env):
    conn = _conn(app_env.db_path)
    fg = _persona(conn, "fabricator-general")
    admin = _persona(conn, "administratum")
    mech = _persona(conn, "mechanicus-worker")
    # Even a (pathological) admin row reporting to FG must not be coerced to the
    # worker coat — the biconditional is worker-tier only.
    iid = _insert_instance(
        conn, persona_id=admin, rank="overseer", commander_type="persona", commander_id=fg
    )
    _, _, pid, _, _ = _row(conn, iid)
    conn.close()
    assert pid == admin
    assert pid != mech


# ── Read-time validation guard (belt + suspenders) ────────────────────────────


def test_query_guard_clean_in_steady_state(app_env):
    conn = _conn(app_env.db_path)
    fg = _persona(conn, "fabricator-general")
    _insert_instance(conn, commander_type="persona", commander_id=fg)  # -> forced mech
    _insert_instance(conn, persona_id=_persona(conn, "ultramarines"), rank="astartes")
    _insert_instance(conn, persona_id=fg, rank="overseer")  # exempt singleton
    violations = validate_mechanicus_invariant_sync(conn)
    conn.close()
    assert violations == [], "write triggers keep the guard empty in steady state"


def test_query_guard_flags_violations_when_triggers_bypassed(app_env):
    # Simulate rows that predate / bypass the write triggers (e.g. a raw bulk
    # rebuild), then prove the read-time guard catches both invariant breaks.
    conn = _conn(app_env.db_path)
    for name in MECH_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.commit()
    fg = _persona(conn, "fabricator-general")
    mech = _persona(conn, "mechanicus-worker")
    ultra = _persona(conn, "ultramarines")
    # (1) commander -> FG but not the coat: biconditional break.
    a = _insert_instance(conn, commander_type="persona", commander_id=fg, persona_id=ultra)
    # (2) coat but no FG commander and automated=0: biconditional + not_automated.
    b = _insert_instance(conn, persona_id=mech, automated=0)
    violations = {v["id"]: v["reasons"] for v in validate_mechanicus_invariant_sync(conn)}
    conn.close()
    assert "biconditional" in violations.get(a, [])
    assert set(violations.get(b, [])) == {"biconditional", "not_automated"}


def test_query_guard_exempts_singletons_when_triggers_bypassed(app_env):
    conn = _conn(app_env.db_path)
    for name in MECH_TRIGGERS:
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
    conn.commit()
    fg = _persona(conn, "fabricator-general")
    admin = _persona(conn, "administratum")
    iid = _insert_instance(
        conn, persona_id=admin, rank="overseer", commander_type="persona", commander_id=fg
    )
    violations = [v["id"] for v in validate_mechanicus_invariant_sync(conn)]
    conn.close()
    assert iid not in violations, "overseer singletons are out of the worker-tier guard"


async def test_query_guard_async_matches_sync(app_env):
    async with aiosqlite.connect(app_env.db_path) as db:
        cur = await db.execute("SELECT id FROM personas WHERE slug='fabricator-general'")
        fg = (await cur.fetchone())[0]
        now = datetime.now().isoformat()
        await db.execute(
            "INSERT INTO instances (id,name,device_id,origin_type,commander_type,commander_id,"
            "status,created_at,last_activity,rank,automated,notification_mode,interaction_mode) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                "inst",
                "Mac-Mini",
                "dispatch",
                "persona",
                fg,
                "idle",
                now,
                now,
                "astartes",
                0,
                "verbose",
                "text",
            ),
        )
        await db.commit()
        violations = await validate_mechanicus_invariant(db)
    assert violations == [], "forced to the coat on insert => guard is clean"


def test_force_no_ops_and_never_corrupts_when_target_persona_missing(app_env):
    # Defensive: if a required persona is somehow absent, a force trigger must
    # no-op (EXISTS guard) rather than write the slug subquery's NULL into the row
    # (A nulling persona_id) or abort the parent write (B nulling commander_id,
    # which would fail the commander_type/commander_id CHECK).
    conn = _conn(app_env.db_path)
    fg = _persona(conn, "fabricator-general")
    ultra = _persona(conn, "ultramarines")
    # Remove the unreferenced mechanicus-worker seed to simulate the missing state.
    conn.execute("DELETE FROM personas WHERE slug='mechanicus-worker'")
    conn.commit()
    # A: an FG-commanded row keeps its (non-mech) persona; persona_id is NOT nulled.
    a = _insert_instance(conn, commander_type="persona", commander_id=fg, persona_id=ultra)
    ct, ci, pid, _, _ = _row(conn, a)
    assert (ct, ci, pid) == ("persona", fg, ultra)

    # B: with FG absent and mechanicus-worker present, inserting the coat must
    # no-op (not abort on the commander CHECK by nulling commander_id).
    conn.execute("DELETE FROM personas WHERE slug='fabricator-general'")
    conn.execute(
        "INSERT INTO personas (id, slug, display_name, default_rank, tts_voice) "
        "VALUES ('mech-x', 'mechanicus-worker', 'Mechanicus Worker', 'astartes', NULL)"
    )
    conn.commit()
    b = _insert_instance(conn, persona_id="mech-x")
    ct, ci, pid, _, _ = _row(conn, b)
    assert (ct, ci, pid) == ("emperor", None, "mech-x"), "B no-ops when FG missing"
    conn.close()


@pytest.mark.parametrize("slug", ["fabricator-general", "mechanicus-worker"])
def test_required_personas_present(app_env, slug):
    conn = _conn(app_env.db_path)
    found = conn.execute("SELECT 1 FROM personas WHERE slug=?", (slug,)).fetchone()
    conn.close()
    assert found is not None
