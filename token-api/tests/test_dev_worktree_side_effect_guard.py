"""Dev-worktree instances are test traffic: their hooks must produce NO
Emperor-facing side-effects (completion TTS, AskUserQuestion phone/Discord buzz).
The DB still registers them (isolated dev DB via TOKEN_API_DB) — only the
notification fanout is suppressed, gated on _is_dev_worktree_dir.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest


def _insert_instance(db_path: Path, instance_id: str, working_dir: str) -> None:
    from instance_mutation import insert_instance_sync

    now = datetime.now().isoformat()
    with sqlite3.connect(db_path) as conn:
        insert_instance_sync(
            conn,
            values={
                "id": instance_id,
                "name": instance_id,
                "engine": "codex",
                "working_dir": working_dir,
                "device_id": "Mac-Mini",
                "status": "working",
                "rank": "astartes",
                "is_subagent": 0,
                "created_at": now,
                "last_activity": now,
            },
            mutation_type="instance_registered",
            write_source="test",
            actor="test",
        )
        conn.commit()


def test_guard_excludes_real_workspaces(monkeypatch, tmp_path):
    """Over-suppression check: the Emperor's real workspaces are never dev traffic."""
    hooks = sys.modules["routes.hooks"]
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))

    assert hooks._is_dev_worktree_dir(None) is False
    assert hooks._is_dev_worktree_dir("/Volumes/Imperium/Imperium-ENV") is False
    assert hooks._is_dev_worktree_dir(str(home / "runtimes" / "Token-OS" / "live")) is False
    # A real dev worktree IS flagged.
    assert hooks._is_dev_worktree_dir(str(home / "worktrees" / "Token-OS" / "wt-x")) is True


@pytest.mark.asyncio
async def test_stop_skips_side_effects_for_dev_worktree(app_env, monkeypatch, tmp_path):
    hooks = sys.modules["routes.hooks"]
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    worktree = home / "worktrees" / "Token-OS" / "wt-feat-stop"
    worktree.mkdir(parents=True)
    _insert_instance(app_env.db_path, "dev-stop", str(worktree))

    notify_calls: list = []
    tts_calls: list = []
    monkeypatch.setattr(hooks, "dispatch_notify", lambda *a, **k: notify_calls.append((a, k)))

    async def _fake_queue_tts(*a, **k):
        tts_calls.append((a, k))

    monkeypatch.setattr(hooks, "queue_tts", _fake_queue_tts)

    result = await hooks.handle_stop({"session_id": "dev-stop"})

    assert result["action"] == "skipped_dev_worktree"
    assert notify_calls == []
    assert tts_calls == []


@pytest.mark.asyncio
async def test_pre_tool_use_skips_askq_for_dev_worktree(app_env, monkeypatch, tmp_path):
    hooks = sys.modules["routes.hooks"]
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    worktree = home / "worktrees" / "Token-OS" / "wt-feat-ask"
    worktree.mkdir(parents=True)
    _insert_instance(app_env.db_path, "dev-ask", str(worktree))

    notify_calls: list = []
    monkeypatch.setattr(hooks, "dispatch_notify", lambda *a, **k: notify_calls.append((a, k)))

    result = await hooks.handle_pre_tool_use(
        {
            "session_id": "dev-ask",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "Pick one?", "options": ["a", "b"]}]},
            "cwd": str(worktree),
        }
    )

    assert result["action"] == "allowed_dev_worktree"
    assert notify_calls == []
