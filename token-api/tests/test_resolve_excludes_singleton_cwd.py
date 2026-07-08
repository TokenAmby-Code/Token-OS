"""/api/instances/resolve fails closed on the CWD fallback: never a singleton row.

Workers and singleton persona seats (Custodes, Fabricator-General, Administratum,
Pax — any ``personas.default_rank != 'astartes'``) routinely share a working_dir (the
vault, the main repo). A cwd fallback that could land on a singleton is identity theft:
a worker whose wrapper_id lookup missed would resolve AS the singleton. Singletons
resolve by wrapper_id only; an unmatched worker 404s rather than adopt a singleton id.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import uuid
from datetime import datetime

import pytest
from fastapi import HTTPException


def _persona_ids(db_path):
    conn = sqlite3.connect(db_path)
    try:
        singleton = conn.execute(
            "SELECT id, slug FROM personas WHERE default_rank != 'astartes' LIMIT 1"
        ).fetchone()
        worker = conn.execute(
            "SELECT id, slug FROM personas WHERE default_rank = 'astartes' LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert singleton is not None, "expected a seeded singleton persona"
    assert worker is not None, "expected a seeded astartes persona"
    return singleton, worker


def _seed(db_path, *, persona_id, cwd, status, wrapper_id=None):
    iid = str(uuid.uuid4())
    now = datetime.now().isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """INSERT INTO instances
                   (id, device_id, persona_id, commander_type, rank, status,
                    working_dir, wrapper_launch_id, created_at, last_activity)
               VALUES (?, 'Mac-Mini', ?, 'emperor', 'astartes', ?, ?, ?, ?, ?)""",
            (iid, persona_id, status, cwd, wrapper_id, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return iid


def test_cwd_fallback_never_returns_singleton_when_worker_shares_dir(app_env):
    main = sys.modules["main"]
    (singleton_pid, _singleton_slug), (worker_pid, _worker_slug) = _persona_ids(app_env.db_path)

    shared_cwd = "/Volumes/Imperium/Imperium-ENV"
    # Singleton is 'working' (the PREFERRED status) and would win a naive cwd match.
    singleton_id = _seed(
        app_env.db_path, persona_id=singleton_pid, cwd=shared_cwd, status="working"
    )
    worker_id = _seed(app_env.db_path, persona_id=worker_pid, cwd=shared_cwd, status="idle")

    result = asyncio.run(main.resolve_instance(cwd=shared_cwd))

    # The astartes worker wins the cwd fallback; the singleton is never returned.
    assert result["id"] == worker_id
    assert result["id"] != singleton_id


def test_cwd_fallback_fails_closed_when_only_singleton_shares_dir(app_env):
    main = sys.modules["main"]
    (singleton_pid, _singleton_slug), _worker = _persona_ids(app_env.db_path)

    solo_cwd = "/Volumes/Imperium/only-singleton-here"
    _seed(app_env.db_path, persona_id=singleton_pid, cwd=solo_cwd, status="working")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(main.resolve_instance(cwd=solo_cwd))
    assert exc.value.status_code == 404


def test_singleton_still_resolves_by_wrapper_id(app_env):
    main = sys.modules["main"]
    (singleton_pid, _singleton_slug), _worker = _persona_ids(app_env.db_path)

    solo_cwd = "/Volumes/Imperium/wrapper-resolves"
    singleton_id = _seed(
        app_env.db_path,
        persona_id=singleton_pid,
        cwd=solo_cwd,
        status="working",
        wrapper_id="wrap-singleton",
    )

    # wrapper_id is the sanctioned path for a singleton — it still resolves.
    result = asyncio.run(main.resolve_instance(wrapper_id="wrap-singleton"))
    assert result["id"] == singleton_id
