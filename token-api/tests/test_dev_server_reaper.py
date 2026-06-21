from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest


def _insert_instance(db_path: Path, instance_id: str, working_dir: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO instances (
                id, name, engine, working_dir, device_id, status, rank,
                is_subagent, created_at, last_activity
            ) VALUES (?, ?, 'codex', ?, 'Mac-Mini', 'working', 'astartes', 1,
                      CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            """,
            (instance_id, instance_id, working_dir),
        )
        conn.commit()


@pytest.mark.asyncio
async def test_session_end_spawns_dev_server_stop_for_dev_worktree(app_env, monkeypatch, tmp_path):
    hooks = sys.modules["routes.hooks"]
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    worktree = home / "worktrees" / "Token-OS" / "wt-feat-x"
    worktree.mkdir(parents=True)
    _insert_instance(app_env.db_path, "dev-close", str(worktree))

    popen_calls: list[tuple[list[str], dict]] = []

    class DummyPopen:
        def __init__(self, args, **kwargs):
            popen_calls.append((list(args), kwargs))

    monkeypatch.setattr(hooks.subprocess, "Popen", DummyPopen)
    monkeypatch.setattr(hooks, "_spawn_session_end_assertion", lambda *a, **k: None)
    monkeypatch.setattr(hooks, "_schedule_naming_nudge", lambda *a, **k: None)
    monkeypatch.setattr(hooks.shared, "clear_pane_tint", lambda *a, **k: None)

    result = await hooks.handle_session_end({"session_id": "dev-close", "reason": "logout"})

    assert result["action"] == "stopped"
    assert len(popen_calls) == 1
    args, kwargs = popen_calls[0]
    assert args == [str(Path.cwd() / "cli-tools" / "bin" / "dev-server-stop"), str(worktree)]
    assert kwargs["start_new_session"] is True
    assert kwargs["close_fds"] is True


@pytest.mark.asyncio
async def test_session_end_does_not_spawn_dev_server_stop_for_non_worktree(app_env, monkeypatch):
    hooks = sys.modules["routes.hooks"]
    _insert_instance(app_env.db_path, "live-close", "/Users/tokenclaw/runtimes/Token-OS/live")

    popen_calls: list = []

    class DummyPopen:
        def __init__(self, args, **kwargs):
            popen_calls.append((args, kwargs))

    monkeypatch.setattr(hooks.subprocess, "Popen", DummyPopen)
    monkeypatch.setattr(hooks, "_spawn_session_end_assertion", lambda *a, **k: None)
    monkeypatch.setattr(hooks, "_schedule_naming_nudge", lambda *a, **k: None)
    monkeypatch.setattr(hooks.shared, "clear_pane_tint", lambda *a, **k: None)

    result = await hooks.handle_session_end({"session_id": "live-close", "reason": "logout"})

    assert result["action"] == "stopped"
    assert popen_calls == []
