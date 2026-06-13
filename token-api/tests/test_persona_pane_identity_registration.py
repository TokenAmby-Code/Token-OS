"""Persona pane identity derivation regressions (2026-06-12 reboot wave).

R-M1 — legion:malcador map miss. tmuxctl stamps @PANE_ID=legion:malcador on the
advisor seat (builder.py) and assertions expect persona malcador on its row,
but PERSONA_PANE_IDENTITY had no entry for the label — a fresh SessionStart in
the pane resolved the label and still registered as a generic astartes row
(live 8ff5aef5: pane_label='legion:malcador', no persona identity).

R-M2 — chapter-child poisoning. A persona relaunch chain (the old persona
session dispatching/resuming its successor) leaks the predecessor into
TOKEN_API_PARENT_INSTANCE_ID. Honoring it registers the fresh persona row with
commander_type='chapter', which exempts the row from the singleton guard, the
default-rank stamp triggers, and resolve_live_persona_instance — so the dead
predecessor stays the resolvable singleton (live custodes 6a8773e9 registered
rank=astartes, commanded by its own zombie d865db2e; enforcement TTS routed to
the corpse). Persona singletons must register Emperor-commanded, always.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from types import SimpleNamespace

import pytest


def _conn(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _insert_instance(
    db_path,
    instance_id,
    *,
    persona_slug=None,
    rank="astartes",
    status="working",
    commander_type="emperor",
    commander_id=None,
    created_at="2026-06-09T18:13:48",
):
    conn = sqlite3.connect(db_path)
    persona_id = None
    if persona_slug:
        persona_id = conn.execute(
            "SELECT id FROM personas WHERE slug = ?", (persona_slug,)
        ).fetchone()[0]
    conn.execute(
        """INSERT INTO instances
           (id, name, engine, working_dir, device_id, origin_type, commander_type,
            commander_id, status, created_at, last_activity, persona_id, rank)
           VALUES (?, ?, 'claude', '/tmp', 'Mac-Mini', 'local', ?,
                   ?, ?, ?, '2026-06-10T09:36:15', ?, ?)""",
        (
            instance_id,
            instance_id,
            commander_type,
            commander_id,
            status,
            created_at,
            persona_id,
            rank,
        ),
    )
    conn.commit()
    conn.close()


def _row(db_path, instance_id):
    conn = _conn(db_path)
    row = conn.execute(
        """SELECT i.rank, i.status, i.commander_type, i.commander_id, i.hook_driven,
                  p.slug AS persona_slug
             FROM instances i
             LEFT JOIN personas p ON p.id = i.persona_id
            WHERE i.id = ?""",
        (instance_id,),
    ).fetchone()
    conn.close()
    return row


def _start_session(hooks, session_id, env=None):
    payload_env = {"TMUX_PANE": "%pp", "TOKEN_API_ENGINE": "claude"}
    payload_env.update(env or {})

    async def run():
        return await hooks.handle_session_start(
            {"session_id": session_id, "cwd": "/tmp", "env": payload_env}
        )

    return asyncio.run(run())


def _label_resolver(label):
    async def resolve(_pane):
        return label

    return resolve


def _no_pane_occupant(monkeypatch, hooks):
    async def none(_pane):
        return None

    monkeypatch.setattr(hooks.shared, "instance_id_for_pane", none)


# ── R-M1: legion:malcador derives the advisor seat identity ────────────────────


def test_malcador_pane_registers_with_primarch_identity(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "_tmux_pane_label", _label_resolver("legion:malcador"))
    _no_pane_occupant(monkeypatch, hooks)

    result = _start_session(hooks, "malc-1")
    assert result["success"] is True

    row = _row(app_env.db_path, "malc-1")
    assert row["persona_slug"] == "malcador"
    # personas seeds malcador with default_rank='primarch'; the stamp trigger
    # must promote the freshly inserted row off the 'astartes' column default.
    assert row["rank"] == "primarch"
    assert row["commander_type"] == "emperor"


# ── R-M2: persona panes never register as chapter children ─────────────────────


def test_persona_pane_parent_env_does_not_register_chapter_child(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "dispatcher-fg")
    monkeypatch.setattr(hooks, "_tmux_pane_label", _label_resolver("legion:malcador"))
    _no_pane_occupant(monkeypatch, hooks)

    result = _start_session(hooks, "malc-2", env={"TOKEN_API_PARENT_INSTANCE_ID": "dispatcher-fg"})
    assert result["success"] is True

    row = _row(app_env.db_path, "malc-2")
    assert row["commander_type"] == "emperor"
    assert row["commander_id"] is None
    assert row["rank"] == "primarch"
    # The suppressed parent must also be invisible to the dispatch→worker
    # classification: a non-custodes agent parent would otherwise flag
    # hook_driven=1.
    assert row["hook_driven"] == 0


def test_custodes_relaunch_over_zombie_predecessor_absorbs_it(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Incident reproduction: the zombie predecessor holds rank=overseer with
    # status='working' (it never stopped); the relaunch env carries the zombie
    # as parent. The persona-singleton supplant absorbs the zombie row into the
    # new session id; the result must be exactly one Emperor-commanded custodes
    # row at overseer — never a chapter child of the zombie, and no second row
    # left behind for the resolver to find.
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "zombie-cust", persona_slug="custodes", rank="overseer")
    monkeypatch.setattr(hooks, "_tmux_pane_label", _label_resolver("legion:custodes"))
    _no_pane_occupant(monkeypatch, hooks)

    result = _start_session(
        hooks, "fresh-cust", env={"TOKEN_API_PARENT_INSTANCE_ID": "zombie-cust"}
    )
    assert result["success"] is True

    fresh = _row(app_env.db_path, "fresh-cust")
    assert fresh["commander_type"] == "emperor"
    assert fresh["commander_id"] is None
    assert fresh["persona_slug"] == "custodes"
    assert fresh["rank"] == "overseer"

    assert _row(app_env.db_path, "zombie-cust") is None
    conn = _conn(app_env.db_path)
    live = conn.execute(
        """SELECT COUNT(*) FROM instances i JOIN personas p ON p.id = i.persona_id
            WHERE p.slug = 'custodes' AND i.rank != 'retired'"""
    ).fetchone()[0]
    conn.close()
    assert live == 1


def test_persona_supplant_does_not_restore_poisoned_prior_parent(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The supplant path restores blank launch fields from the prior persona row
    # (old_parent_id is its commander edge). When the prior row was already
    # poisoned into a chapter child, supplanting it must shed the poison —
    # _effective_parent suppresses the restore and the supplant updates assert
    # emperor sovereignty. Mirrors the live incident shape: the chapter commander
    # is the persona's own (still-active) zombie predecessor, which the commander
    # integrity guard requires to be active and same-persona.
    hooks = sys.modules["routes.hooks"]
    _insert_instance(
        app_env.db_path,
        "dead-dispatcher",
        persona_slug="custodes",
        rank="overseer",
    )
    _insert_instance(
        app_env.db_path,
        "stale-cust",
        persona_slug="custodes",
        status="stopped",
        commander_type="chapter",
        commander_id="dead-dispatcher",
        created_at="2026-06-11T08:00:00",
    )
    monkeypatch.setattr(hooks, "_tmux_pane_label", _label_resolver("legion:custodes"))
    _no_pane_occupant(monkeypatch, hooks)

    result = _start_session(hooks, "fresh-cust-2")
    assert result["success"] is True

    # Persona-singleton supplant migrates the stale row to the new session id.
    row = _row(app_env.db_path, "fresh-cust-2")
    assert row is not None
    assert row["commander_type"] == "emperor"
    assert row["commander_id"] is None
    assert row["rank"] == "overseer"
