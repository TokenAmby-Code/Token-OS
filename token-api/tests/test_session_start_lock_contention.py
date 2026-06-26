"""SessionStart SQLite write-lock contention — root-cause regression tests.

Forensics (2026-06-26): the live token-api log showed bursts of
``TIMER: Failed to write sample: database is locked`` /
``TIMER: Failed to save to DB: database is locked`` and HTTP 503s on
``POST /api/hooks/SessionStart``. WAL mode and a 5s busy_timeout were already in
place, so a writer only errors when another connection holds the single SQLite
write lock for LONGER than the busy_timeout.

Root cause: ``handle_session_start`` opened its connection in the default
*deferred* isolation level, so the registration INSERT took the WAL write lock
and the handler then held it across slow tmux/SMB side effects
(``_stamp_instance_id`` → tmux subprocess, ``resolve_session_doc_for_start`` →
vault/SMB I/O, pane tint, frontmatter writes) before its first commit. While the
lock was held for those multi-second awaits, every other writer — the timer
sampler, ``log_event``, and *other* concurrent SessionStart handlers — was
starved and eventually errored ``database is locked``.

Fix under test: hook/route handlers connect through ``shared.hook_db()``, an
autocommit (``isolation_level=None``) connection with an explicit busy_timeout.
Each write commits immediately, so the write lock is never held across a slow
non-DB await and concurrent writers are no longer starved.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient


async def _noop_event(*_a: object, **_k: object) -> None:
    return None


def test_hook_db_is_autocommit_with_busy_timeout(app_env: SimpleNamespace) -> None:
    """``shared.hook_db()`` must be autocommit and carry a real busy_timeout.

    Autocommit (``isolation_level is None``) is what guarantees the write lock is
    released between statements; the busy_timeout is the headroom that lets the
    rare genuine collision wait instead of erroring.
    """
    shared = app_env.shared

    async def check() -> None:
        async with shared.hook_db() as db:
            assert db.isolation_level is None, "hook_db must be autocommit"
            cur = await db.execute("PRAGMA busy_timeout")
            row = await cur.fetchone()
            assert row is not None and row[0] >= 5000, f"busy_timeout too low: {row}"

    asyncio.run(check())


def test_sessionstart_releases_write_lock_during_slow_side_effects(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SessionStart must not hold the write lock across its slow side effects.

    We park the handler inside ``_stamp_instance_id`` — which on the new-instance
    path runs AFTER the registration INSERT — and, from another thread, fire a
    concurrent writer with a short (250 ms) busy_timeout. On the buggy deferred
    path the handler still holds the write lock from the INSERT, so the probe is
    starved and raises ``database is locked``. With the autocommit fix the INSERT
    has already committed, the lock is free, and the probe succeeds.
    """
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "log_event", _noop_event)

    entered = threading.Event()
    probe_done = threading.Event()

    async def slow_stamp(*_a: object, **_k: object) -> None:
        # Mid-handler, after the durable INSERT. Hold the window open so the
        # concurrent probe can observe whether the write lock is still held.
        entered.set()
        await asyncio.get_event_loop().run_in_executor(None, probe_done.wait, 5.0)
        return None

    monkeypatch.setattr(hooks, "_stamp_instance_id", slow_stamp)

    result: dict[str, object] = {}

    def run_post() -> None:
        client = TestClient(app_env.main.app, raise_server_exceptions=False)
        resp = client.post(
            "/api/hooks/SessionStart",
            json={"session_id": "lock-probe", "cwd": "/tmp"},
        )
        result["status"] = resp.status_code

    poster = threading.Thread(target=run_post)
    poster.start()

    assert entered.wait(timeout=10.0), "handler never reached the slow side effect"

    probe_error: str | None = None
    try:
        conn = sqlite3.connect(app_env.db_path)
        conn.execute("PRAGMA busy_timeout=250")
        conn.execute("INSERT INTO events (event_type) VALUES ('lock_probe')")
        conn.commit()
        conn.close()
    except sqlite3.OperationalError as exc:
        probe_error = str(exc)
    finally:
        probe_done.set()

    poster.join(timeout=10.0)

    assert probe_error is None, (
        "concurrent writer starved while SessionStart held the write lock across "
        f"a slow side effect: {probe_error}"
    )
    assert result.get("status") == 200
