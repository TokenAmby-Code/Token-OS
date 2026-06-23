"""SessionStart registration write resilience + fail-loud.

Forensics (2026-06-23): ``GET /api/instances?status=active`` returned count=0
with 6 live agent panes. ``events.hook_error`` showed SessionStart failing with
``database is locked`` (WAL writer contention under fleet load) and
``UNIQUE constraint failed: instances.id`` (concurrent re-fire race). The generic
``dispatch_hook`` wrapper swallowed both into an HTTP 200 ``{success: False}`` —
so the row was never written AND the client ``curl --retry`` (PR #225) never
re-fired, because curl only retries on connection/5xx errors, not a 200 with an
error body. Generic worker panes have no persona-sweep reconciler (#225 only
sweeps ``PERSONA_LABELS``), so they stay row-less until the next full
``tx restart``.

New gap, not a regression of #225/#198:
- #225 added a client ``curl --retry`` plus a persona-only sweep; the retry is
  defeated by the 200-swallow and the sweep doesn't cover generic workers.
- #198 made ``POST /api/instances/register`` idempotent on UNIQUE, but the *hook*
  path's ``sanctioned_insert_instance`` was never made idempotent.

Fix under test:
1. ``dispatch_hook`` retries the handler on ``database is locked`` (bounded
   backoff) so transient WAL writer contention self-heals in-band.
2. ``handle_session_start`` converges idempotently on a UNIQUE insert race
   instead of surfacing it as a swallowed ``handler_error``.
3. A SessionStart write that still fails surfaces as HTTP 503 (fail loud) so the
   client retry re-fires, instead of a silent 200 that strands the pane.
"""

from __future__ import annotations

import sqlite3
import sys

import aiosqlite
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(app_env):
    return TestClient(app_env.main.app, raise_server_exceptions=False)


def _ids(db_path):
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id, status FROM instances").fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}


def test_session_start_retries_on_database_locked(app_env, client, monkeypatch):
    """A transient ``database is locked`` must be retried in-band, not swallowed.

    The row must end up written and the handler must report success.
    """
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "_HOOK_DB_LOCKED_BACKOFF", 0.0, raising=False)

    # The real handler's nested `log_event` opens a *separate* connection that
    # blocks on this handler's own open write transaction for the full 5s busy
    # timeout (it logs "event log dropped" and moves on). That self-contention is
    # pre-existing latency, irrelevant to the retry path under test — stub it so
    # the test exercises the retry, not the busy-timeout wait.
    async def _noop_event(*_a, **_k):
        return None

    monkeypatch.setattr(hooks, "log_event", _noop_event)

    real = hooks.handle_session_start
    calls = {"n": 0}

    async def flaky(payload):
        calls["n"] += 1
        if calls["n"] < 3:
            raise aiosqlite.OperationalError("database is locked")
        return await real(payload)

    monkeypatch.setattr(hooks, "handle_session_start", flaky)

    resp = client.post("/api/hooks/SessionStart", json={"session_id": "lock-1", "cwd": "/tmp"})

    assert resp.status_code == 200
    assert resp.json().get("success") is True
    assert calls["n"] == 3, "handler should have been retried until it succeeded"
    assert "lock-1" in _ids(app_env.db_path), "registration row must persist after retry"


def test_session_start_idempotent_on_unique_insert_race(app_env, client, monkeypatch):
    """A concurrent winner of the INSERT race must converge, not raise UNIQUE.

    Simulates the winner committing the row between this call's existing-row
    SELECT and its own INSERT.
    """
    hooks = sys.modules["routes.hooks"]

    real_insert = hooks.sanctioned_insert_instance
    state = {"raised": False}

    async def colliding_insert(db, **kwargs):
        if not state["raised"]:
            state["raised"] = True
            # The concurrent winner commits this id, then our INSERT collides.
            await real_insert(db, **kwargs)
            await db.commit()
            raise aiosqlite.IntegrityError("UNIQUE constraint failed: instances.id")
        return await real_insert(db, **kwargs)

    monkeypatch.setattr(hooks, "sanctioned_insert_instance", colliding_insert)

    resp = client.post("/api/hooks/SessionStart", json={"session_id": "race-1", "cwd": "/tmp"})

    assert resp.status_code == 200
    body = resp.json()
    assert body.get("success") is True, "UNIQUE race must converge idempotently"
    assert body.get("action") in {"already_registered", "registered", "reregistered"}
    rows = _ids(app_env.db_path)
    assert "race-1" in rows, "the raced row must exist exactly once"


def test_session_start_write_failure_fails_loud_503(app_env, client, monkeypatch):
    """An unrecoverable SessionStart write must fail loud (HTTP 503), not 200.

    A 200 ``{success: False}`` defeats the client ``curl --retry`` safety net and
    strands the pane with no row.
    """
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "_HOOK_DB_LOCKED_BACKOFF", 0.0, raising=False)

    async def always_locked(payload):
        raise aiosqlite.OperationalError("database is locked")

    monkeypatch.setattr(hooks, "handle_session_start", always_locked)

    resp = client.post("/api/hooks/SessionStart", json={"session_id": "fail-1", "cwd": "/tmp"})

    assert resp.status_code == 503, "a dropped registration must surface, not return 200"


def test_non_session_start_hook_stays_best_effort(app_env, client, monkeypatch):
    """Fail-loud is scoped to SessionStart; other hooks stay fire-and-forget 200.

    PostToolUse/PreToolUse are background best-effort — a persistent lock there
    must not turn into a 503 that the (retry-less) background curl can't use.
    """
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "_HOOK_DB_LOCKED_BACKOFF", 0.0, raising=False)

    async def always_locked(payload):
        raise aiosqlite.OperationalError("database is locked")

    monkeypatch.setattr(hooks, "handle_post_tool_use", always_locked)

    resp = client.post("/api/hooks/PostToolUse", json={"session_id": "ptu-1", "cwd": "/tmp"})

    assert resp.status_code == 200
    assert resp.json().get("success") is False
