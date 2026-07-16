"""Regression coverage for SessionStart's registry/stamp transaction boundary."""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

TOKEN_API_DIR = Path(__file__).resolve().parents[1]
if str(TOKEN_API_DIR) not in sys.path:
    sys.path.insert(0, str(TOKEN_API_DIR))


def test_registration_commit_precedes_fake_pane_stamp(monkeypatch):
    """The fake pane stamp must never run while the SQLite write is uncommitted."""
    hooks = importlib.import_module("routes.hooks")
    events: list[object] = []

    class FakeDb:
        async def commit(self):
            events.append("commit")

    async def fake_stamp(**kwargs):
        events.append(("stamp", kwargs))

    monkeypatch.setattr(hooks, "_bind_instance_stamp", fake_stamp)

    asyncio.run(
        hooks._commit_registration_before_stamp(
            FakeDb(),
            tmux_pane="%99",
            session_id="worker-1",
            wrapper_launch_id="wrapper-1",
            engine="codex",
            working_dir="/tmp/worktree",
            persona="astartes",
        )
    )

    assert events == [
        "commit",
        (
            "stamp",
            {
                "tmux_pane": "%99",
                "session_id": "worker-1",
                "wrapper_launch_id": "wrapper-1",
                "engine": "codex",
                "working_dir": "/tmp/worktree",
                "persona": "astartes",
            },
        ),
    ]
