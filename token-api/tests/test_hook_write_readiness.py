"""Behavioral pins for hook-write serialization and readiness."""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
import sys
from pathlib import Path

TOKEN_API_DIR = Path(__file__).resolve().parents[1]
if str(TOKEN_API_DIR) not in sys.path:
    sys.path.insert(0, str(TOKEN_API_DIR))


def test_agents_db_write_probe_rejects_active_writer(tmp_path):
    db_connections = importlib.import_module("db_connections")
    db_path = tmp_path / "agents.db"
    with sqlite3.connect(db_path) as setup:
        setup.execute("CREATE TABLE specimen (id INTEGER PRIMARY KEY)")

    holder = sqlite3.connect(db_path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        readiness = db_connections.probe_sqlite_write_readiness(db_path)
    finally:
        holder.rollback()
        holder.close()

    assert readiness["live"] is True
    assert readiness["ready"] is False
    assert readiness["reason"] == "database_locked"
    assert db_connections.probe_sqlite_write_readiness(db_path) == {
        "live": True,
        "ready": True,
        "reason": None,
    }


def test_stop_subscription_writes_are_serialized(monkeypatch):
    hooks = importlib.import_module("routes.hooks")
    active = 0
    peak = 0

    async def fake_upsert(_db, **_kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return 7

    class FakeDb:
        async def commit(self):
            await asyncio.sleep(0)

    monkeypatch.setattr(hooks, "_upsert_stop_subscription", fake_upsert)

    async def exercise():
        kwargs = {
            "target_instance_id": "target",
            "target_pane": "%1",
            "subscriber_instance_id": "subscriber",
            "subscriber_pane": "%2",
        }
        return await asyncio.gather(
            hooks._commit_stop_subscription(FakeDb(), **kwargs),
            hooks._commit_stop_subscription(FakeDb(), **kwargs),
        )

    assert asyncio.run(exercise()) == [7, 7]
    assert peak == 1


def test_health_distinguishes_liveness_from_write_readiness(monkeypatch):
    main = importlib.import_module("main")
    monkeypatch.setattr(
        main,
        "probe_sqlite_write_readiness",
        lambda _path: {"live": True, "ready": False, "reason": "database_locked"},
    )

    response = asyncio.run(main.health_check())

    assert response["status"] == "degraded"
    assert response["live"] is True
    assert response["ready"] is False
    assert response["readiness"]["agents_db_write"]["reason"] == "database_locked"
