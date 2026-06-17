"""Regression coverage for chapter-dispatch persona clobber.

A chapter commander edge is control/parentage.  It must not overwrite an
explicit worker persona and turn a worker into a singleton identity shadow.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime
from typing import Any

import aiosqlite


def _conn(db_path: Any) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _persona(conn: sqlite3.Connection, slug: str) -> str:
    row = conn.execute("SELECT id FROM personas WHERE slug = ?", (slug,)).fetchone()
    assert row is not None, slug
    return row[0]


def _seed_instance(
    conn: sqlite3.Connection,
    instance_id: str,
    *,
    persona_slug: str | None = None,
    rank: str = "astartes",
    status: str = "working",
) -> None:
    persona_id = _persona(conn, persona_slug) if persona_slug else None
    now = datetime.now().isoformat()
    conn.execute(
        """INSERT INTO instances
           (id, name, engine, working_dir, device_id, origin_type,
            commander_type, commander_id, status, created_at, last_activity,
            persona_id, rank)
           VALUES (?, ?, 'claude', '/tmp', 'Mac-Mini', 'local',
                   'emperor', NULL, ?, ?, ?, ?, ?)""",
        (instance_id, instance_id, status, now, now, persona_id, rank),
    )


def _identity_row(conn: sqlite3.Connection, instance_id: str) -> sqlite3.Row:
    row = conn.execute(
        """SELECT i.id, i.rank, i.status, i.commander_type, i.commander_id,
                  p.slug AS persona_slug
             FROM instances i
             LEFT JOIN personas p ON p.id = i.persona_id
            WHERE i.id = ?""",
        (instance_id,),
    ).fetchone()
    assert row is not None, instance_id
    return row


async def test_chapter_insert_preserves_explicit_worker_persona_and_singleton(app_env):
    from instance_mutation import sanctioned_insert_instance

    conn = _conn(app_env.db_path)
    _seed_instance(conn, "fg-overseer", persona_slug="fabricator-general", rank="overseer")
    worker_persona = _persona(conn, "salamanders")
    conn.commit()
    conn.close()

    now = datetime.now().isoformat()
    async with aiosqlite.connect(app_env.db_path) as db:
        await sanctioned_insert_instance(
            db,
            values={
                "id": "salamander-worker",
                "name": "salamander-worker",
                "engine": "claude",
                "working_dir": "/tmp",
                "device_id": "Mac-Mini",
                "origin_type": "dispatch",
                "commander_type": "chapter",
                "commander_id": "fg-overseer",
                "status": "idle",
                "created_at": now,
                "last_activity": now,
                "persona_id": worker_persona,
                "rank": "astartes",
            },
            mutation_type="instance_registered",
            write_source="test",
            actor="test",
        )
        await db.commit()

    conn = _conn(app_env.db_path)
    worker = _identity_row(conn, "salamander-worker")
    fg = _identity_row(conn, "fg-overseer")
    conn.close()

    assert worker["persona_slug"] == "salamanders"
    assert worker["rank"] == "astartes"
    assert (worker["commander_type"], worker["commander_id"]) == ("chapter", "fg-overseer")
    assert fg["persona_slug"] == "fabricator-general"
    assert fg["rank"] == "overseer"
    assert fg["status"] == "working"


def test_chapter_insert_falls_back_to_commander_persona_when_worker_has_none(app_env):
    from instance_mutation import sanctioned_insert_instance_sync

    conn = _conn(app_env.db_path)
    _seed_instance(conn, "fg-overseer", persona_slug="fabricator-general", rank="overseer")
    conn.commit()

    now = datetime.now().isoformat()
    sanctioned_insert_instance_sync(
        conn,
        values={
            "id": "orphan-persona-worker",
            "name": "orphan-persona-worker",
            "engine": "claude",
            "working_dir": "/tmp",
            "device_id": "Mac-Mini",
            "origin_type": "dispatch",
            "commander_type": "chapter",
            "commander_id": "fg-overseer",
            "status": "idle",
            "created_at": now,
            "last_activity": now,
            "persona_id": None,
            "rank": "astartes",
        },
        mutation_type="instance_registered",
        write_source="test",
        actor="test",
    )
    conn.commit()

    worker = _identity_row(conn, "orphan-persona-worker")
    fg = _identity_row(conn, "fg-overseer")
    conn.close()

    assert worker["persona_slug"] == "fabricator-general"
    assert (worker["commander_type"], worker["commander_id"]) == ("chapter", "fg-overseer")
    assert fg["rank"] == "overseer"
    assert fg["status"] == "working"


def test_session_start_parent_binding_preserves_token_api_persona(app_env, monkeypatch):
    """Full hook path: fake pane/stamp only; never touch the live tmux session."""

    hooks = sys.modules["routes.hooks"]

    conn = _conn(app_env.db_path)
    _seed_instance(conn, "fg-overseer", persona_slug="fabricator-general", rank="overseer")
    conn.commit()
    conn.close()

    async def no_label(_pane):
        return None

    async def no_stamp(*_args, **_kwargs):
        return None

    async def no_pane_occupant(_pane):
        return None

    monkeypatch.setattr(hooks, "_tmux_pane_label", no_label)
    monkeypatch.setattr(hooks, "_stamp_instance_id", no_stamp)
    monkeypatch.setattr(hooks.shared, "instance_id_for_pane", no_pane_occupant)

    async def run():
        return await hooks.handle_session_start(
            {
                "session_id": "hook-worker",
                "cwd": "/tmp",
                "env": {
                    "TMUX_PANE": "%dispatch-test",
                    "TOKEN_API_ENGINE": "claude",
                    "TOKEN_API_LAUNCHER": "dispatch",
                    "TOKEN_API_PARENT_INSTANCE_ID": "fg-overseer",
                    "TOKEN_API_PERSONA": "salamanders",
                },
            }
        )

    result = asyncio.run(run())
    assert result["success"] is True

    conn = _conn(app_env.db_path)
    worker = _identity_row(conn, "hook-worker")
    fg = _identity_row(conn, "fg-overseer")
    conn.close()

    assert worker["persona_slug"] == "salamanders"
    assert (worker["commander_type"], worker["commander_id"]) == ("chapter", "fg-overseer")
    assert worker["rank"] == "astartes"
    assert fg["persona_slug"] == "fabricator-general"
    assert fg["rank"] == "overseer"
    assert fg["status"] == "working"
