"""
Reusable database query helpers for Token-API.

Extracts the most repeated inline SQL patterns from main.py.
Each helper opens its own connection (matching existing patterns)
or accepts an existing connection via the `db` parameter.
"""

from pathlib import Path
from typing import Optional

import aiosqlite

from db_schema import DEFAULT_DB_PATH

DB_PATH = DEFAULT_DB_PATH


async def get_instance(
    instance_id: str,
    *,
    db: Optional[aiosqlite.Connection] = None,
    db_path: Path = DB_PATH,
) -> Optional[dict]:
    """Fetch a claude_instances row by id. Returns dict or None."""
    async def _query(conn: aiosqlite.Connection) -> Optional[dict]:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT * FROM claude_instances WHERE id = ?",
            (instance_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    if db is not None:
        return await _query(db)
    async with aiosqlite.connect(db_path) as conn:
        return await _query(conn)


async def count_active_instances(
    *,
    exclude_subagents: bool = True,
    db: Optional[aiosqlite.Connection] = None,
    db_path: Path = DB_PATH,
) -> int:
    """Count instances with status IN ('processing', 'idle').

    exclude_subagents=True (default): only top-level instances.
    exclude_subagents=False: all instances including subagents.
    """
    if exclude_subagents:
        sql = "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle') AND COALESCE(is_subagent, 0) = 0"
    else:
        sql = "SELECT COUNT(*) FROM claude_instances WHERE status IN ('processing', 'idle')"

    async def _query(conn: aiosqlite.Connection) -> int:
        cursor = await conn.execute(sql)
        row = await cursor.fetchone()
        return row[0] if row else 0

    if db is not None:
        return await _query(db)
    async with aiosqlite.connect(db_path) as conn:
        return await _query(conn)


async def count_instances_for_doc(
    session_doc_id: int,
    *,
    db: Optional[aiosqlite.Connection] = None,
    db_path: Path = DB_PATH,
) -> int:
    """Count instances linked to a specific session document."""
    async def _query(conn: aiosqlite.Connection) -> int:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM claude_instances WHERE session_doc_id = ?",
            (session_doc_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    if db is not None:
        return await _query(db)
    async with aiosqlite.connect(db_path) as conn:
        return await _query(conn)
