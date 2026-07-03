"""Regression coverage for chapter-dispatch persona clobber.

A chapter commander edge is control/parentage.  It must not overwrite an
explicit worker persona and turn a worker into a singleton identity shadow.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
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


def test_chapter_insert_preserves_explicit_worker_persona_and_singleton(app_env):
    from instance_mutation import insert_instance

    conn = _conn(app_env.db_path)
    _seed_instance(conn, "fg-overseer", persona_slug="fabricator-general", rank="overseer")
    worker_persona = _persona(conn, "salamanders")
    conn.commit()
    conn.close()

    now = datetime.now().isoformat()

    async def insert_worker():
        async with aiosqlite.connect(app_env.db_path) as db:
            await insert_instance(
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

    asyncio.run(insert_worker())

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
    from instance_mutation import insert_instance_sync

    conn = _conn(app_env.db_path)
    _seed_instance(conn, "fg-overseer", persona_slug="fabricator-general", rank="overseer")
    conn.commit()

    now = datetime.now().isoformat()
    insert_instance_sync(
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


def test_fg_dispatched_worker_identity_fields_do_not_clobber_singleton_row(app_env, monkeypatch):
    """Full hook path, no live tmux: worker name/doc land on worker row only.

    This reproduces the #567 failure shape defensively: a Fabricator-General row
    exists with its own wrapper id/name/session_doc, then a dispatched worker
    starts while carrying the same wrapper id. SessionStart must register the
    worker fresh instead of re-keying or mutating the singleton row; the later
    official worker rename must also affect only the worker row.
    """

    from instance_mutation import update_instance

    hooks = sys.modules["routes.hooks"]

    vault = Path(os.environ["IMPERIUM_ENV"])
    parent_doc_path = vault / "Mars" / "Sessions" / "fabricator-general.md"
    worker_doc_path = vault / "Mars" / "Sessions" / "checkpoint-event-gate-fix.md"
    parent_doc_path.parent.mkdir(parents=True, exist_ok=True)
    parent_doc_path.write_text("# Fabricator-General\n", encoding="utf-8")
    worker_doc_path.write_text("# checkpoint-event-gate-fix\n", encoding="utf-8")

    conn = _conn(app_env.db_path)
    fg_persona = _persona(conn, "fabricator-general")
    now = datetime.now().isoformat()
    parent_doc_id = conn.execute(
        """INSERT INTO session_documents
           (file_path, title, project, status, created_at, updated_at)
           VALUES (?, 'Fabricator-General', 'Mars', 'active', ?, ?)""",
        (str(parent_doc_path), now, now),
    ).lastrowid
    worker_doc_id = conn.execute(
        """INSERT INTO session_documents
           (file_path, title, project, status, created_at, updated_at)
           VALUES (?, 'checkpoint-event-gate-fix', 'Mars', 'active', ?, ?)""",
        (str(worker_doc_path), now, now),
    ).lastrowid
    conn.execute(
        """INSERT INTO instances
           (id, name, engine, working_dir, device_id, origin_type,
            commander_type, commander_id, status, created_at, last_activity,
            persona_id, rank, session_doc_id, session_doc_policy, wrapper_launch_id)
           VALUES ('fg-overseer', 'Fabricator-General', 'codex', '/tmp/fg',
                   'Mac-Mini', 'local', 'emperor', NULL, 'working', ?, ?,
                   ?, 'overseer', ?, 'persona', 'fg-wrapper')""",
        (now, now, fg_persona, parent_doc_id),
    )
    conn.commit()
    conn.close()

    async def no_label(_pane):
        return None

    async def no_pane_occupant(_pane):
        return None

    async def no_resolve_instance_pane(_instance_id):
        return None, None

    async def no_async_write(*_args, **_kwargs):
        return None

    def no_sync_write(*_args, **_kwargs):
        return None

    monkeypatch.setattr(hooks, "_tmux_pane_label", no_label)
    monkeypatch.setattr(hooks, "_stamp_instance_id", no_async_write)
    monkeypatch.setattr(hooks, "_unstamp_instance_id", no_async_write)
    monkeypatch.setattr(hooks.shared, "instance_id_for_pane", no_pane_occupant)
    monkeypatch.setattr(hooks.shared, "resolve_instance_pane", no_resolve_instance_pane)
    monkeypatch.setattr(hooks.shared, "clear_pane_tint", no_sync_write)
    monkeypatch.setattr(hooks.shared, "apply_instance_pane_tint", no_async_write)
    monkeypatch.setattr(hooks.shared, "push_agnostic_pane_vars", no_async_write)

    async def run_start_and_worker_rename():
        result = await hooks.handle_session_start(
            {
                "session_id": "fg-dispatched-worker",
                "cwd": "/tmp/worker",
                "pid": 4242,
                "env": {
                    "TMUX_PANE": "%fg-dispatch-test",
                    "TOKEN_API_ENGINE": "claude",
                    "TOKEN_API_LAUNCHER": "dispatch",
                    # Poisoned inherited dispatcher wrapper id: must not adopt FG.
                    "TOKEN_API_WRAPPER_ID": "fg-wrapper",
                    "TOKEN_API_DISPATCH_TARGET": "mechanicus:new",
                    "TOKEN_API_DISPATCH_WINDOW": "mechanicus",
                    "TOKEN_API_DISPATCH_MODE": "new",
                    "TOKEN_API_DISPATCH_SESSION_DOC_PATH": str(worker_doc_path),
                    "TOKEN_API_TARGET_WORKING_DIR": "/tmp/worker",
                },
            }
        )
        assert result["success"] is True
        assert result["instance_id"] == "fg-dispatched-worker"
        assert result["action"] != "supplanted"

        async with aiosqlite.connect(app_env.db_path) as db:
            await update_instance(
                db,
                instance_id="fg-dispatched-worker",
                updates={"name": "checkpoint-event-gate-fix"},
                mutation_type="instance_updated",
                write_source="test",
                actor="instance-name-cli",
                wrapper_launch_id="fg-wrapper",
            )
            await db.commit()

    asyncio.run(run_start_and_worker_rename())

    conn = _conn(app_env.db_path)
    rows = {
        row["id"]: row
        for row in conn.execute(
            """SELECT id, name, session_doc_id, rank, status
               FROM instances
               WHERE id IN ('fg-overseer', 'fg-dispatched-worker')"""
        )
    }
    conn.close()

    assert set(rows) == {"fg-overseer", "fg-dispatched-worker"}
    assert rows["fg-overseer"]["name"] == "Fabricator-General"
    assert rows["fg-overseer"]["session_doc_id"] == parent_doc_id
    assert rows["fg-overseer"]["rank"] == "overseer"
    assert rows["fg-overseer"]["status"] == "working"
    assert rows["fg-dispatched-worker"]["name"] == "checkpoint-event-gate-fix"
    assert rows["fg-dispatched-worker"]["session_doc_id"] == worker_doc_id
