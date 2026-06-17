"""Empirical reproduction of the Custodes-specified shadow scenario for
resolve_live_persona_instance, FG (fabricator-general):

  PRIMARY  : overseer singleton + live chapter children sharing persona_id
             -> must resolve to the OVERSEER, ignoring chapter children.
  SECONDARY: active overseer + a retired row sharing persona_id
             -> must resolve to the active overseer, never the retired row.
  EDGE     : ONLY chapter children alive (no emperor-commanded overseer)
             -> documents what the resolver returns (None).
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import datetime
from typing import Any

import aiosqlite

import personas


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
        "persona_id": None,
        "rank": "astartes",
    }
    values.update(overrides)
    cols = list(values)
    conn.execute(
        f"INSERT INTO instances ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [values[c] for c in cols],
    )
    return values["id"]


async def _resolve(db_path, slug):
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
    # Two dispatched workers parented to FG, sharing persona_id, MORE recent.
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
    """Why the chapter filter is always safe: the schema's chapter_persona_guard
    forbids a chapter child whose commander is not a live row sharing its
    persona_id. So whenever a chapter child exists, its emperor/persona-commanded
    overseer also exists — resolve_live_persona_instance is never left with only
    chapter rows to choose from."""
    conn = _conn(app_env.db_path)
    fg = _persona(conn, "fabricator-general")
    raised = False
    try:
        _insert_instance(
            conn,
            id="fg-orphan-child",
            persona_id=fg,
            commander_type="chapter",
            commander_id="fg-over",
            rank="overseer",
            status="working",
        )
    except sqlite3.IntegrityError as exc:
        raised = True
        assert "chapter commander must be active and share persona_id" in str(exc)
    finally:
        conn.close()
    assert raised, "schema must forbid an orphan chapter child (no live commander)"
