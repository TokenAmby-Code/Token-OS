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


# ── Pax: the civic overseer seat on the koronus page ───────────────────────────


def test_pax_pane_registers_with_overseer_identity(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fresh SessionStart in the koronus:pax pane IS Pax: its identity is derived
    # from PERSONA_PANE_IDENTITY (primarch='pax' → the `pax` personas row), and
    # the rank-stamp trigger must promote the freshly inserted row off the
    # 'astartes' column default to 'overseer'. Emperor-commanded, like every
    # persona singleton.
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "_tmux_pane_label", _label_resolver("koronus:pax"))
    _no_pane_occupant(monkeypatch, hooks)

    result = _start_session(hooks, "pax-1")
    assert result["success"] is True

    row = _row(app_env.db_path, "pax-1")
    assert row["persona_slug"] == "pax"
    assert row["rank"] == "overseer"
    assert row["commander_type"] == "emperor"


def test_pax_pane_parent_env_does_not_register_chapter_child(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A persona relaunch chain leaks the predecessor into
    # TOKEN_API_PARENT_INSTANCE_ID; honoring it would register Pax as a chapter
    # child (exempt from the singleton guard and rank-stamp triggers). The pax
    # singleton must register Emperor-commanded, always.
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "dispatcher-pax")
    monkeypatch.setattr(hooks, "_tmux_pane_label", _label_resolver("koronus:pax"))
    _no_pane_occupant(monkeypatch, hooks)

    result = _start_session(hooks, "pax-2", env={"TOKEN_API_PARENT_INSTANCE_ID": "dispatcher-pax"})
    assert result["success"] is True

    row = _row(app_env.db_path, "pax-2")
    assert row["persona_slug"] == "pax"
    assert row["rank"] == "overseer"
    assert row["commander_type"] == "emperor"
    assert row["commander_id"] is None
    assert row["hook_driven"] == 0


def test_orchestrator_pane_registers_with_overseer_identity(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The koronus:orchestrator pane IS the civic Orchestrator seat: identity is
    # derived from PERSONA_PANE_IDENTITY (primarch='orchestrator' → the
    # `orchestrator` personas row), promoted off the astartes default to overseer.
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "_tmux_pane_label", _label_resolver("koronus:orchestrator"))
    _no_pane_occupant(monkeypatch, hooks)

    result = _start_session(hooks, "orch-1")
    assert result["success"] is True

    row = _row(app_env.db_path, "orch-1")
    assert row["persona_slug"] == "orchestrator"
    assert row["rank"] == "overseer"
    assert row["commander_type"] == "emperor"


def test_pax_pane_off_koronus_page_falls_back_to_astartes(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Off-page fallback: a civic seat promoted to palace/somnium (or any non-koronus
    # pane) has no PERSONA_PANE_IDENTITY entry, so it must NOT register as the pax
    # overseer singleton. It falls through to a normal astartes registration so it
    # obeys the standard tint + TTS rules.
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "_tmux_pane_label", _label_resolver("palace:N"))
    _no_pane_occupant(monkeypatch, hooks)

    result = _start_session(hooks, "pax-offpage")
    assert result["success"] is True

    row = _row(app_env.db_path, "pax-offpage")
    assert row["persona_slug"] != "pax"
    assert row["rank"] == "astartes"


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


def test_custodes_paneless_start_resolves_label_for_stamp_and_gold_tint(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Custodes SessionStart can arrive with no TMUX_PANE while still carrying
    # pane_label=legion:custodes. The hook must resolve that stable label through
    # the live pane oracle and use the effective pane for both @INSTANCE_ID stamp
    # and personas.pane_tint application.
    hooks = sys.modules["routes.hooks"]
    stamps: list[tuple[str | None, str | None]] = []
    tint_calls: list[tuple[str | None, str | None]] = []

    async def resolve_label(target):
        return "%custodes" if target == "legion:custodes" else None

    async def stamp(pane, session_id, **_kwargs):
        stamps.append((pane, session_id))

    def apply_pane_tint(pane, tint, **_kwargs):
        tint_calls.append((pane, tint))

    _no_pane_occupant(monkeypatch, hooks)
    monkeypatch.setattr(hooks.shared, "resolve_tmux_pane_id", resolve_label)
    monkeypatch.setattr(hooks, "_stamp_instance_id", stamp)
    monkeypatch.setattr(hooks.shared, "apply_pane_tint", apply_pane_tint)

    result = asyncio.run(
        hooks.handle_session_start(
            {
                "session_id": "cust-paneless",
                "cwd": "/tmp",
                "pane_label": "legion:custodes",
                "env": {"TOKEN_API_ENGINE": "claude"},
            }
        )
    )

    row = _row(app_env.db_path, "cust-paneless")
    assert row["persona_slug"] == "custodes"
    assert row["rank"] == "overseer"
    assert result["pane_tint"] == "#302800"
    assert ("%custodes", "cust-paneless") in stamps
    assert ("%custodes", "#302800") in tint_calls
    assert all(tint != "default" for _pane, tint in tint_calls)


def test_persona_supplant_does_not_steal_live_prior_parent(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The poisoned stale child shape is also the emperor-path theft vector: a
    # stopped chapter child can be selected for supplant, rewritten to emperor
    # sovereignty, and thereby retire/replace the still-working incumbent. The
    # singleton guard must fail closed and leave the live incumbent untouched.
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

    with pytest.raises(sqlite3.IntegrityError, match="live singleton incumbent exists"):
        _start_session(hooks, "fresh-cust-2")

    incumbent = _row(app_env.db_path, "dead-dispatcher")
    assert incumbent is not None
    assert incumbent["commander_type"] == "emperor"
    assert incumbent["commander_id"] is None
    assert incumbent["rank"] == "overseer"
    assert incumbent["status"] == "working"

    assert _row(app_env.db_path, "fresh-cust-2") is None
    stale = _row(app_env.db_path, "stale-cust")
    assert stale is not None
    assert stale["commander_type"] == "chapter"
    assert stale["commander_id"] == "dead-dispatcher"
    assert stale["status"] == "stopped"
