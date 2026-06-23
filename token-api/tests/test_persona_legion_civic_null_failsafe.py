"""P1 regression: `legion=civic` PATCH must never null a singleton persona slug.

Source bug: a `legion=civic` write (civic is an ALLOWED_LEGION but maps to no
persona; the legacy `legion` column died into `persona_id`) nulled the persona
slug on the koronus:pax / koronus:orchestrator singleton rows. The resolver then
reported "no live instance" and suppressed every send
(`persona_unregistered_suppressed attempts=45`) while the pane was demonstrably
alive.

Two halves, mirroring the fix split:

  #2 DB fail-safe (the real fix) — a writer-agnostic DB trigger refuses to null an
     existing persona binding. Even if some other writer fires the bad write, the
     slug is preserved (prior valid state intact). Tested directly against the
     schema with a raw `persona_id = NULL` UPDATE.

  #1 verify-only registration — the koronus:pax / orchestrator SessionStart
     registration binds persona via `primarch` and never issues a persona-nulling
     `legion=civic` write, and the `/api/instances/{id}/legion` endpoint with civic
     on an already-bound singleton preserves the binding.

Uses a real temp sqlite DB seeded through the canonical `instances` table with
fake ids — no live tmux panes are touched.
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


def _persona_id(db_path, slug):
    conn = _conn(db_path)
    row = conn.execute("SELECT id FROM personas WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    return row[0] if row else None


def _insert_singleton(db_path, instance_id, persona_slug, *, rank="overseer", status="working"):
    conn = sqlite3.connect(db_path)
    persona_id = conn.execute("SELECT id FROM personas WHERE slug = ?", (persona_slug,)).fetchone()[
        0
    ]
    conn.execute(
        """INSERT INTO instances
           (id, name, engine, working_dir, device_id, origin_type, commander_type,
            commander_id, status, created_at, last_activity, persona_id, rank)
           VALUES (?, ?, 'claude', '/tmp', 'Mac-Mini', 'local', 'emperor',
                   NULL, ?, '2026-06-23T09:00:00', '2026-06-23T09:00:00', ?, ?)""",
        (instance_id, instance_id, status, persona_id, rank),
    )
    conn.commit()
    conn.close()
    return persona_id


def _persona_slug(db_path, instance_id):
    conn = _conn(db_path)
    row = conn.execute(
        """SELECT p.slug AS persona_slug
             FROM instances i
             LEFT JOIN personas p ON p.id = i.persona_id
            WHERE i.id = ?""",
        (instance_id,),
    ).fetchone()
    conn.close()
    return row["persona_slug"] if row else None


# ── #2 DB fail-safe — the DB refuses to null an existing persona binding ───────


def test_raw_null_update_cannot_clobber_pax_persona(app_env: SimpleNamespace) -> None:
    """The exact corruption: a write nulls persona_id on the live pax singleton row.

    Reproduces the slug-null by issuing a raw `persona_id = NULL` UPDATE (what the
    `legion=civic` resolution did when civic resolved to no persona). After the fix
    the DB trigger restores the prior binding, so the slug stays `pax`.
    """
    _insert_singleton(app_env.db_path, "pax-live", "pax")
    assert _persona_slug(app_env.db_path, "pax-live") == "pax"

    conn = sqlite3.connect(app_env.db_path)
    conn.execute("UPDATE instances SET persona_id = NULL WHERE id = ?", ("pax-live",))
    conn.commit()
    conn.close()

    # Binding preserved — the DB refused to null the slug.
    assert _persona_slug(app_env.db_path, "pax-live") == "pax"


def test_null_clobber_failsafe_lets_sibling_columns_land(app_env: SimpleNamespace) -> None:
    """The fail-safe preserves persona_id but does NOT veto the rest of the write."""
    _insert_singleton(app_env.db_path, "orch-live", "orchestrator")

    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        "UPDATE instances SET persona_id = NULL, status = ?, last_activity = ? WHERE id = ?",
        ("idle", "2026-06-23T10:00:00", "orch-live"),
    )
    conn.commit()
    row = (
        _conn(app_env.db_path)
        .execute("SELECT status, last_activity FROM instances WHERE id = ?", ("orch-live",))
        .fetchone()
    )
    conn.close()

    assert _persona_slug(app_env.db_path, "orch-live") == "orchestrator"
    assert row["status"] == "idle"
    assert row["last_activity"] == "2026-06-23T10:00:00"


def test_failsafe_allows_rebind_to_a_real_persona(app_env: SimpleNamespace) -> None:
    """A legitimate rebind to a different real persona still works (only NULL is guarded)."""
    _insert_singleton(app_env.db_path, "rebind-me", "pax")
    custodes_id = _persona_id(app_env.db_path, "custodes")

    conn = sqlite3.connect(app_env.db_path)
    conn.execute("UPDATE instances SET persona_id = ? WHERE id = ?", (custodes_id, "rebind-me"))
    conn.commit()
    conn.close()

    assert _persona_slug(app_env.db_path, "rebind-me") == "custodes"


def test_failsafe_inert_when_no_prior_binding(app_env: SimpleNamespace) -> None:
    """A row that never had a persona is unaffected (NULL → NULL is not a clobber)."""
    conn = sqlite3.connect(app_env.db_path)
    conn.execute(
        """INSERT INTO instances
           (id, name, engine, working_dir, device_id, origin_type, commander_type,
            commander_id, status, created_at, last_activity, persona_id, rank)
           VALUES ('no-persona', 'no-persona', 'claude', '/tmp', 'Mac-Mini', 'local',
                   'emperor', NULL, 'working', '2026-06-23T09:00:00',
                   '2026-06-23T09:00:00', NULL, 'astartes')""",
    )
    conn.execute("UPDATE instances SET persona_id = NULL WHERE id = 'no-persona'")
    conn.commit()
    conn.close()
    assert _persona_slug(app_env.db_path, "no-persona") is None


# ── #1 verify-only registration — civic never nulls the singleton binding ──────


def _label_resolver(label):
    async def resolve(_pane):
        return label

    return resolve


def _no_pane_occupant(monkeypatch, hooks):
    async def none(_pane):
        return None

    monkeypatch.setattr(hooks.shared, "instance_id_for_pane", none)


def _start_session(hooks, session_id, env=None):
    payload_env = {"TMUX_PANE": "%pp", "TOKEN_API_ENGINE": "claude"}
    payload_env.update(env or {})

    async def run():
        return await hooks.handle_session_start(
            {"session_id": session_id, "cwd": "/tmp", "env": payload_env}
        )

    return asyncio.run(run())


def test_pax_sessionstart_civic_legion_binds_persona_not_null(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """koronus:pax registers under legion=civic but binds persona via primarch=pax.

    The civic legion label must NOT drive a persona-nulling write: the row ends up
    bound to the `pax` persona, never NULL.
    """
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "_tmux_pane_label", _label_resolver("koronus:pax"))
    _no_pane_occupant(monkeypatch, hooks)

    result = _start_session(hooks, "pax-reg")
    assert result["success"] is True
    assert _persona_slug(app_env.db_path, "pax-reg") == "pax"


def test_legion_endpoint_civic_preserves_bound_singleton_persona(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PATCH /legion {"legion":"civic"} on a bound pax row must not null the slug.

    civic is an ALLOWED_LEGION with no persona mapping; the endpoint must leave the
    existing persona binding intact rather than clobbering it.
    """
    import httpx
    from httpx import ASGITransport

    main = app_env.main
    _insert_singleton(app_env.db_path, "pax-patch", "pax")

    async def run():
        transport = ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.patch("/api/instances/pax-patch/legion", json={"legion": "civic"})

    resp = asyncio.run(run())
    assert resp.status_code == 200
    assert _persona_slug(app_env.db_path, "pax-patch") == "pax"
