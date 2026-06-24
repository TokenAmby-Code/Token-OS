"""Regression tests for singleton persona bypass resolvers.

These direct SQL paths must use the same live-singleton definition as
personas.resolve_live_persona_instance: active, non-retired, non-chapter.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import datetime
from os import PathLike
from typing import Any

_INSTANCE_INSERT_COLUMNS = frozenset(
    {
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
        "persona_id",
        "rank",
        "golden_throne",
    }
)


def _conn(db_path: str | PathLike[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _persona(conn: sqlite3.Connection, slug: str) -> str:
    row = conn.execute("SELECT id FROM personas WHERE slug = ?", (slug,)).fetchone()
    assert row is not None, slug
    return row[0]


def _insert_instance(conn: sqlite3.Connection, **overrides: Any) -> str:
    from instance_mutation import sanctioned_insert_instance_sync

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
        "status": "working",
        "created_at": now,
        "last_activity": now,
        "stopped_at": None,
        "persona_id": None,
        "rank": "overseer",
        "golden_throne": None,
    }
    values.update(overrides)
    cols = list(values)
    invalid_cols = [col for col in cols if col not in _INSTANCE_INSERT_COLUMNS]
    assert not invalid_cols, f"unexpected instances columns: {invalid_cols}"
    sanctioned_insert_instance_sync(
        conn,
        values=values,
        mutation_type="instance_registered",
        write_source="test",
        actor="test",
    )
    return values["id"]


def _seed_shadowed_singleton(
    db_path: str | PathLike[str], slug: str, *, live_id: str, child_id: str, retired_id: str
) -> None:
    """Seed a live singleton plus newer shadow rows for the same persona.

    If a resolver is missing either canonical filter, it will select one of the
    shadows because they are more recently active than the true singleton.
    """
    conn = _conn(db_path)
    persona_id = _persona(conn, slug)
    _insert_instance(
        conn,
        id=live_id,
        name=f"{slug}-live",
        persona_id=persona_id,
        commander_type="emperor",
        commander_id=None,
        rank="overseer",
        status="working",
        last_activity="2025-01-01T00:00:00",
    )
    _insert_instance(
        conn,
        id=retired_id,
        name=f"{slug}-retired",
        persona_id=persona_id,
        commander_type="emperor",
        commander_id=None,
        rank="retired",
        # Malformed historical rows can carry retired rank without stopped status;
        # rank, not status, is the identity death marker this regression pins.
        status="working",
        last_activity="2025-12-30T00:00:00",
    )
    _insert_instance(
        conn,
        id=child_id,
        name=f"{slug}-child",
        persona_id=persona_id,
        commander_type="chapter",
        commander_id=live_id,
        rank="overseer",
        status="working",
        last_activity="2025-12-31T00:00:00",
    )
    conn.commit()
    conn.close()


def test_administratum_bypass_resolver_uses_canonical_singleton_filter(
    app_env: Any, monkeypatch: Any
) -> None:
    main = app_env.main
    _seed_shadowed_singleton(
        app_env.db_path,
        "administratum",
        live_id="admin-live",
        child_id="admin-child",
        retired_id="admin-retired",
    )

    # Pane geometry is resolved live from the oracle (instances.tmux_pane is gone).
    async def fake_resolve_pane(instance_id):
        return ("%10", "council:administratum") if instance_id == "admin-live" else (None, None)

    monkeypatch.setattr(main.shared, "resolve_instance_pane", fake_resolve_pane)

    resolved = asyncio.run(main._resolve_administratum_instance())

    assert resolved is not None
    assert resolved["id"] == "admin-live"
    assert resolved["tmux_pane"] == "%10"


def test_administratum_resolver_rejects_poisoned_worker_persona_row(
    app_env: Any, monkeypatch: Any
) -> None:
    main = app_env.main
    conn = _conn(app_env.db_path)
    for trigger in ("trg_instances_singleton_guard", "trg_instances_singleton_guard_update"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    conn.commit()
    admin = _persona(conn, "administratum")
    _insert_instance(
        conn,
        id="admin-good",
        name="admin-good",
        persona_id=admin,
        commander_type="emperor",
        commander_id=None,
        rank="overseer",
        status="working",
        last_activity="2025-01-01T00:00:00",
    )
    # Poison reproduction: a mechanicus worker row wearing Administratum persona
    # and newer than the true recorder. It must never receive state hooks.
    _insert_instance(
        conn,
        id="admin-poison-worker",
        name="mechanicus-worker-poison",
        persona_id=admin,
        commander_type="persona",
        commander_id=_persona(conn, "fabricator-general"),
        rank="overseer",
        status="working",
        last_activity="2025-12-31T00:00:00",
    )
    conn.commit()
    conn.close()

    async def fake_resolve_pane(instance_id):
        if instance_id == "admin-poison-worker":
            return "%121", "mechanicus:1"
        if instance_id == "admin-good":
            return "%30", "council:administratum"
        return None, None

    monkeypatch.setattr(main.shared, "resolve_instance_pane", fake_resolve_pane)

    resolved = asyncio.run(main._resolve_administratum_instance())

    assert resolved is not None
    assert resolved["id"] == "admin-good"
    assert resolved["tmux_pane"] == "%30"


def test_administratum_resolver_uses_rowless_admin_pane_not_poisoned_worker(
    app_env: Any, monkeypatch: Any
) -> None:
    main = app_env.main
    conn = _conn(app_env.db_path)
    admin = _persona(conn, "administratum")
    _insert_instance(
        conn,
        id="admin-poison-worker",
        name="mechanicus-worker-poison",
        persona_id=admin,
        commander_type="persona",
        commander_id=_persona(conn, "fabricator-general"),
        rank="overseer",
        status="working",
        last_activity="2025-12-31T00:00:00",
    )
    conn.commit()
    conn.close()

    async def fake_resolve_pane(instance_id):
        if instance_id == "admin-poison-worker":
            return "%121", "mechanicus:1"
        return None, None

    async def fake_find_admin_pane():
        return "%30"

    async def fake_instance_id_for_pane(pane):
        return "admin-rowless-live" if pane == "%30" else None

    monkeypatch.setattr(main.shared, "resolve_instance_pane", fake_resolve_pane)
    monkeypatch.setattr(main, "_find_administratum_tmux_pane", fake_find_admin_pane)
    monkeypatch.setattr(main.shared, "instance_id_for_pane", fake_instance_id_for_pane)

    resolved = asyncio.run(main._resolve_administratum_instance())

    assert resolved is not None
    assert resolved["id"] == "admin-rowless-live"
    assert resolved["tmux_pane"] == "%30"


def test_custodes_morning_brief_injects_into_canonical_singleton(
    app_env: Any, monkeypatch: Any
) -> None:
    main = app_env.main
    _seed_shadowed_singleton(
        app_env.db_path,
        "custodes",
        live_id="cust-live",
        child_id="cust-child",
        retired_id="cust-retired",
    )

    async def fake_tmux_pane_exists(_pane):
        return True

    async def fake_find_custodes_tmux_pane():
        raise AssertionError("DB row should resolve without pane-marker fallback")

    # Pane geometry is resolved live from the oracle (instances.tmux_pane is gone);
    # the canonical singleton must resolve to its live pane without the tmux fallback.
    async def fake_resolve_pane(instance_id):
        return ("%10", "council:custodes") if instance_id == "cust-live" else (None, None)

    async def fake_inject(_prompt, pane, *, instance_id=None):
        return {"dispatched": True, "pane": pane, "instance_id": instance_id}

    import morning_session

    monkeypatch.setattr(main.shared, "resolve_instance_pane", fake_resolve_pane)
    monkeypatch.setattr(main, "_tmux_pane_exists", fake_tmux_pane_exists)
    monkeypatch.setattr(main, "_find_custodes_tmux_pane", fake_find_custodes_tmux_pane)
    monkeypatch.setattr(main, "_inject_custodes_prompt_to_pane", fake_inject)
    monkeypatch.setattr(morning_session, "gather_context", lambda: {})
    monkeypatch.setattr(morning_session, "get_daily_thread_id", lambda _today: None)
    monkeypatch.setattr(morning_session, "build_prompt", lambda _ctx: "brief")

    result = asyncio.run(main.custodes_morning_brief(main.MorningBriefRequest(date="2026-06-17")))

    assert result["mode"] == "inject"
    assert result["instance_id"] == "cust-live"
    assert result["target_pane"] == "%10"
    assert result["delivery"]["instance_id"] == "cust-live"
