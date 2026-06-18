"""Empirical reproduction of the Custodes-specified shadow scenario for
resolve_live_persona_instance, FG (fabricator-general):

  PRIMARY  : overseer singleton + live chapter children
             -> must resolve to the OVERSEER, ignoring chapter children.
  SECONDARY: active overseer + a retired row sharing persona_id
             -> must resolve to the active overseer, never the retired row.
  EDGE     : orphan chapter-child insert (no live commander)
             -> must fail with sqlite3.IntegrityError via chapter commander guard.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import datetime
from os import PathLike
from typing import Any

import aiosqlite

import personas

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
        "persona_id",
        "rank",
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
        "status": "idle",
        "created_at": now,
        "last_activity": now,
        "persona_id": None,
        "rank": "astartes",
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


async def _resolve(db_path: str | PathLike[str], slug: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        return await personas.resolve_live_persona_instance(db, slug)


def test_fg_primary_chapter_children_do_not_shadow_overseer(app_env: Any) -> None:
    conn = _conn(app_env.db_path)
    fg = _persona(conn, "fabricator-general")
    # The real overseer: emperor-commanded, OLDER last_activity.
    _insert_instance(
        conn,
        id="fg-over",
        persona_id=fg,
        rank="overseer",
        status="working",
        last_activity="2025-01-01T00:00:00",
    )
    # Two dispatched workers parented to FG, MORE recent.  Same-persona chapter
    # children remain legal but must not shadow the singleton resolver.
    _insert_instance(
        conn,
        id="fg-child-1",
        persona_id=fg,
        commander_type="chapter",
        commander_id="fg-over",
        rank="overseer",
        status="working",
        last_activity="2025-12-30T00:00:00",
    )
    _insert_instance(
        conn,
        id="fg-child-2",
        persona_id=fg,
        commander_type="chapter",
        commander_id="fg-over",
        rank="overseer",
        status="working",
        last_activity="2025-12-31T00:00:00",
    )
    conn.commit()
    conn.close()

    resolved = asyncio.run(_resolve(app_env.db_path, "fabricator-general"))
    assert resolved is not None, "overseer must resolve even with chapter children present"
    assert resolved["id"] == "fg-over", f"chapter child shadowed overseer: {resolved}"


def test_fg_secondary_retired_row_not_selected(app_env: Any) -> None:
    conn = _conn(app_env.db_path)
    fg = _persona(conn, "fabricator-general")
    _insert_instance(
        conn,
        id="fg-retired",
        persona_id=fg,
        rank="retired",
        status="stopped",
        last_activity="2025-12-31T00:00:00",
    )
    _insert_instance(
        conn,
        id="fg-live",
        persona_id=fg,
        rank="overseer",
        status="working",
        last_activity="2025-01-01T00:00:00",
    )
    conn.commit()
    conn.close()

    resolved = asyncio.run(_resolve(app_env.db_path, "fabricator-general"))
    assert resolved is not None
    assert resolved["id"] == "fg-live"


def test_chapter_child_cannot_exist_without_live_overseer(app_env: Any) -> None:
    """Why the chapter filter is always safe: the schema's commander guard
    forbids a chapter child whose commander is not a live row.  So whenever a
    chapter child exists, its emperor/persona-commanded commander also exists —
    resolve_live_persona_instance is never left with only chapter rows to choose
    from."""
    conn = _conn(app_env.db_path)
    fg = _persona(conn, "fabricator-general")
    raised = False
    try:
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO instances
               (id, name, engine, working_dir, device_id, origin_type,
                commander_type, commander_id, status, created_at, last_activity,
                persona_id, rank)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "fg-orphan-child",
                "inst",
                "claude",
                "/tmp",
                "Mac-Mini",
                "local",
                "chapter",
                "fg-over",
                "working",
                now,
                now,
                fg,
                "overseer",
            ),
        )
    except sqlite3.IntegrityError as exc:
        raised = True
        assert "chapter commander must be active" in str(exc)
    finally:
        conn.close()
    assert raised, "schema must forbid an orphan chapter child (no live commander)"
