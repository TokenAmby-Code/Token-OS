"""Persona pane identity derivation regressions (2026-06-12 reboot wave).

R-M1 — legion:malcador map miss. tmuxctl stamps @PANE_ID=legion:malcador on the
advisor seat (builder.py) and assertions expect primarch='malcador' on its row,
but PERSONA_PANE_IDENTITY had no entry for the label — a fresh SessionStart in
the pane resolved the label and still registered as a generic astartes row
(live 8ff5aef5: pane_label='legion:malcador', primarch='').

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


def _insert_legacy(
    db_path, instance_id, *, primarch=None, legion="astartes", status="idle", parent=None
):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO claude_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            profile_name, tts_voice, notification_sound, status, primarch, legion,
            parent_instance_id)
           VALUES (?, ?, ?, '/tmp', 'local', 'Mac-Mini', ?, 'v', 's', ?, ?, ?, ?)""",
        (
            instance_id,
            f"{instance_id}-session",
            instance_id,
            # Mirror reality: persona rows carry their persona slug as profile_name
            # (slug_from_legacy reads it first when projecting persona_id).
            primarch or "p",
            status,
            primarch,
            legion,
            parent,
        ),
    )
    conn.commit()
    conn.close()


def _insert_canonical(db_path, instance_id, *, persona_slug, rank, status="working"):
    conn = sqlite3.connect(db_path)
    persona_id = conn.execute("SELECT id FROM personas WHERE slug = ?", (persona_slug,)).fetchone()[
        0
    ]
    conn.execute(
        """INSERT INTO instances
           (id, name, engine, working_dir, device_id, origin_type, commander_type,
            status, created_at, last_activity, persona_id, rank)
           VALUES (?, ?, 'claude', '/tmp', 'Mac-Mini', 'local', 'emperor',
                   ?, '2026-06-09T18:13:48', '2026-06-10T09:36:15', ?, ?)""",
        (instance_id, instance_id, status, persona_id, rank),
    )
    conn.commit()
    conn.close()


def _canonical_row(db_path, instance_id):
    conn = _conn(db_path)
    row = conn.execute(
        """SELECT i.rank, i.status, i.commander_type, i.commander_id, p.slug AS persona_slug
             FROM instances i
             LEFT JOIN personas p ON p.id = i.persona_id
            WHERE i.id = ?""",
        (instance_id,),
    ).fetchone()
    conn.close()
    return row


def _legacy_row(db_path, instance_id):
    conn = _conn(db_path)
    row = conn.execute(
        "SELECT primarch, legion, parent_instance_id, hook_driven FROM claude_instances WHERE id = ?",
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

    legacy = _legacy_row(app_env.db_path, "malc-1")
    assert legacy["primarch"] == "malcador"
    assert legacy["legion"] == "astartes"

    row = _canonical_row(app_env.db_path, "malc-1")
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
    _insert_legacy(app_env.db_path, "dispatcher-fg", legion="fabricator")
    monkeypatch.setattr(hooks, "_tmux_pane_label", _label_resolver("legion:malcador"))
    _no_pane_occupant(monkeypatch, hooks)

    result = _start_session(hooks, "malc-2", env={"TOKEN_API_PARENT_INSTANCE_ID": "dispatcher-fg"})
    assert result["success"] is True

    legacy = _legacy_row(app_env.db_path, "malc-2")
    # Stored as '' or NULL depending on the insert path; every reader gates on
    # truthiness, never on NULL-ness.
    assert not legacy["parent_instance_id"]
    # No agent parent → not autonomously driven (hook_driven dispatch column).
    assert legacy["hook_driven"] == 0

    row = _canonical_row(app_env.db_path, "malc-2")
    assert row["commander_type"] == "emperor"
    assert row["commander_id"] is None
    assert row["rank"] == "primarch"


def test_custodes_relaunch_over_zombie_predecessor_retires_it(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Incident reproduction: the zombie predecessor holds rank=overseer with
    # status='working' (it never stopped) and has NO legacy row, so the legacy
    # primarch-singleton supplant cannot fire; the relaunch env carries the
    # zombie as parent. The fresh registration must ignore the parent, take the
    # overseer rank via the stamp trigger, and the singleton guard must retire
    # the zombie — never register as the zombie's chapter child.
    hooks = sys.modules["routes.hooks"]
    _insert_canonical(app_env.db_path, "zombie-cust", persona_slug="custodes", rank="overseer")
    monkeypatch.setattr(hooks, "_tmux_pane_label", _label_resolver("legion:custodes"))
    _no_pane_occupant(monkeypatch, hooks)

    result = _start_session(
        hooks, "fresh-cust", env={"TOKEN_API_PARENT_INSTANCE_ID": "zombie-cust"}
    )
    assert result["success"] is True

    fresh = _canonical_row(app_env.db_path, "fresh-cust")
    assert fresh["commander_type"] == "emperor"
    assert fresh["commander_id"] is None
    assert fresh["persona_slug"] == "custodes"
    assert fresh["rank"] == "overseer"

    zombie = _canonical_row(app_env.db_path, "zombie-cust")
    assert zombie["rank"] == "retired"
    assert zombie["status"] == "stopped"


def test_persona_supplant_does_not_restore_poisoned_prior_parent(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The supplant/update paths restore blank launch fields from the prior row
    # (`parent_instance_id or old_inst[...]`). When the prior persona row was
    # already poisoned with a parent, supplanting it must not re-inherit the
    # poison — _effective_parent suppresses every restore for persona panes.
    hooks = sys.modules["routes.hooks"]
    _insert_legacy(
        app_env.db_path,
        "stale-cust",
        primarch="custodes",
        legion="custodes",
        status="stopped",
        parent="dead-dispatcher",
    )
    monkeypatch.setattr(hooks, "_tmux_pane_label", _label_resolver("legion:custodes"))
    _no_pane_occupant(monkeypatch, hooks)

    result = _start_session(hooks, "fresh-cust-2")
    assert result["success"] is True

    # Primarch-singleton supplant migrates the stale row to the new session id.
    legacy = _legacy_row(app_env.db_path, "fresh-cust-2")
    assert legacy is not None
    assert not legacy["parent_instance_id"]

    row = _canonical_row(app_env.db_path, "fresh-cust-2")
    assert row["commander_type"] == "emperor"
    assert row["rank"] == "overseer"
