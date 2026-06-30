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
  path's ``insert_instance`` was never made idempotent.

Fix under test:
1. The registration INSERT retries on ``database is locked`` at its narrow,
   side-effect-free boundary inside ``handle_session_start`` (not a blanket
   handler replay, which would double-apply committed work).
2. ``handle_session_start`` converges idempotently on a UNIQUE insert race —
   scoped to the instance primary key, so an unrelated UNIQUE failure still
   surfaces.
3. A SessionStart write that still fails surfaces as HTTP 503 (fail loud) so the
   client retry re-fires, instead of a silent 200 that strands the pane.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(app_env: SimpleNamespace) -> TestClient:
    return TestClient(app_env.main.app, raise_server_exceptions=False)


def _ids(db_path: Path) -> dict[str, str]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id, status FROM instances").fetchall()
    conn.close()
    return {row[0]: row[1] for row in rows}


async def _noop_event(*_a: object, **_k: object) -> None:
    # The handler's nested `log_event` opens a *separate* connection that blocks on
    # the handler's own open write transaction for the full 5s busy timeout (it
    # logs "event log dropped" and moves on). That self-contention is pre-existing
    # latency, irrelevant to the write paths under test — stub it for speed.
    return None


def test_registration_insert_retries_on_database_locked(
    app_env: SimpleNamespace, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient ``database is locked`` on the INSERT must be retried in-band.

    The row must end up written and the handler must report success — without a
    blanket handler replay (the retry lives at the INSERT boundary).
    """
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "_HOOK_DB_LOCKED_BACKOFF", 0.0, raising=False)
    monkeypatch.setattr(hooks, "log_event", _noop_event)

    real_insert = hooks.insert_instance
    calls = {"n": 0}

    async def flaky_insert(db: object, **kwargs: object) -> object:
        calls["n"] += 1
        if calls["n"] < 3:
            raise aiosqlite.OperationalError("database is locked")
        return await real_insert(db, **kwargs)

    monkeypatch.setattr(hooks, "insert_instance", flaky_insert)

    resp = client.post("/api/hooks/SessionStart", json={"session_id": "lock-1", "cwd": "/tmp"})

    assert resp.status_code == 200
    assert resp.json().get("success") is True
    assert calls["n"] == 3, "the INSERT should have been retried until it succeeded"
    assert "lock-1" in _ids(app_env.db_path), "registration row must persist after retry"


def test_session_start_idempotent_on_unique_insert_race(
    app_env: SimpleNamespace, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent winner of the INSERT race must converge, not raise UNIQUE.

    The winning row is committed on a *separate* connection (as in the real
    cross-request race) before our INSERT collides.
    """
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "log_event", _noop_event)

    real_insert = hooks.insert_instance
    state = {"raised": False}

    async def colliding_insert(db: object, **kwargs: object) -> object:
        if not state["raised"]:
            state["raised"] = True
            # The concurrent winner commits this id on its own connection, then our
            # INSERT collides — matching the actual two-request race.
            async with aiosqlite.connect(app_env.db_path) as winner:
                winner.row_factory = aiosqlite.Row
                await real_insert(winner, **kwargs)
                await winner.commit()
            raise aiosqlite.IntegrityError("UNIQUE constraint failed: instances.id")
        return await real_insert(db, **kwargs)

    monkeypatch.setattr(hooks, "insert_instance", colliding_insert)

    resp = client.post("/api/hooks/SessionStart", json={"session_id": "race-1", "cwd": "/tmp"})

    assert resp.status_code == 200
    body = resp.json()
    assert body.get("success") is True, "UNIQUE race must converge idempotently"
    assert body.get("action") in {"already_registered", "registered", "reregistered"}
    assert "race-1" in _ids(app_env.db_path), "the raced row must exist exactly once"


def test_non_instance_id_unique_failure_is_not_swallowed(
    app_env: SimpleNamespace, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A UNIQUE failure on something other than instances.id must surface.

    The idempotent recovery is scoped to the instance primary key; an unrelated
    integrity violation (e.g. the audit-trail row) must not be masked as
    already_registered — it fails loud (503) instead.
    """
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "log_event", _noop_event)

    async def unrelated_unique_failure(db: object, **kwargs: object) -> object:
        raise aiosqlite.IntegrityError("UNIQUE constraint failed: instance_mutations.write_txn_id")

    monkeypatch.setattr(hooks, "insert_instance", unrelated_unique_failure)

    resp = client.post("/api/hooks/SessionStart", json={"session_id": "other-1", "cwd": "/tmp"})

    assert resp.status_code == 503, "an unrelated UNIQUE violation must not be swallowed"
    assert "other-1" not in _ids(app_env.db_path)


def test_session_start_write_failure_fails_loud_503(
    app_env: SimpleNamespace, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrecoverable SessionStart write must fail loud (HTTP 503), not 200.

    A 200 ``{success: False}`` defeats the client ``curl --retry`` safety net and
    strands the pane with no row.
    """
    hooks = sys.modules["routes.hooks"]

    async def always_locked(payload: dict) -> dict:
        raise aiosqlite.OperationalError("database is locked")

    monkeypatch.setattr(hooks, "handle_session_start", always_locked)

    resp = client.post("/api/hooks/SessionStart", json={"session_id": "fail-1", "cwd": "/tmp"})

    assert resp.status_code == 503, "a dropped registration must surface, not return 200"


def test_non_session_start_hook_stays_best_effort(
    app_env: SimpleNamespace, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-loud is scoped to SessionStart; other hooks stay fire-and-forget 200.

    PostToolUse/PreToolUse are background best-effort — a failure there must not
    turn into a 503 that the (retry-less) background curl can't use.
    """
    hooks = sys.modules["routes.hooks"]

    async def always_locked(payload: dict) -> dict:
        raise aiosqlite.OperationalError("database is locked")

    monkeypatch.setattr(hooks, "handle_post_tool_use", always_locked)

    resp = client.post("/api/hooks/PostToolUse", json={"session_id": "ptu-1", "cwd": "/tmp"})

    assert resp.status_code == 200
    assert resp.json().get("success") is False
