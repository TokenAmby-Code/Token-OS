"""SessionStart no longer churns pane-local @INSTANCE_ID stamps.

Token-API may read the tmuxctld/tmux oracle to preserve registry continuity, but
pane stamp set/unset ownership belongs to tmuxctld/wrapper lifecycle.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_LIVE_PANE = "%churn"


def _seed_instance(db_path: Path, session_id: str, *, wrapper_id: str = "wrap-churn") -> None:
    conn = sqlite3.connect(db_path)
    try:
        persona_id = conn.execute("SELECT id FROM personas WHERE slug='blood-angels'").fetchone()[0]
        conn.execute(
            """INSERT INTO instances
                   (id, device_id, persona_id, rank, status, wrapper_launch_id, last_activity)
               VALUES (?, 'Mac-Mini', ?, 'astartes', 'idle', ?, '2026-07-01T00:00:00')""",
            (session_id, persona_id, wrapper_id),
        )
        conn.commit()
    finally:
        conn.close()


def _spy_stamp_writes(hooks, monkeypatch):
    writes: list[tuple[str, ...]] = []

    async def _no_pane_instance(_pane):
        return None

    async def _resolve_instance(instance_id):
        return (_LIVE_PANE, "palace:N") if instance_id else (None, None)

    async def _fake_tmux_run(args, **_kwargs):
        writes.append(tuple(args))
        return {"stdout": ""}

    def _fake_sync_tmux(args, **_kwargs):
        writes.append(tuple(args))
        return {"stdout": ""}

    async def _no_tint(*_args, **_kwargs):
        return None

    monkeypatch.setattr(hooks.shared, "instance_id_for_pane", _no_pane_instance)
    monkeypatch.setattr(hooks.shared, "resolve_instance_pane", _resolve_instance)
    monkeypatch.setattr(hooks.shared, "tmuxctld_run_tmux", _fake_tmux_run)
    monkeypatch.setattr(hooks.shared, "_tmuxctld_run_tmux", _fake_sync_tmux)
    monkeypatch.setattr(hooks.shared, "apply_instance_pane_tint", _no_tint)
    return writes


def _stamp_mutations(writes: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    return [
        w
        for w in writes
        if "set-option" in w and ("@INSTANCE_ID" in w or "@PANE_LABEL" in w)
    ]


def test_blank_pane_refire_does_not_mutate_pane_stamps(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = sys.modules["routes.hooks"]
    _seed_instance(app_env.db_path, "churn-1", wrapper_id="wrap-churn-1")
    writes = _spy_stamp_writes(hooks, monkeypatch)

    result = asyncio.run(
        hooks.handle_session_start(
            {
                "session_id": "churn-1",
                "cwd": "/tmp/churn",
                "pid": 999,
                "env": {"TOKEN_API_WRAPPER_ID": "wrap-churn-1"},
            }
        )
    )

    assert result["success"] is True
    assert _stamp_mutations(writes) == []


def test_genuine_pane_move_still_does_not_mutate_pane_stamps(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks = sys.modules["routes.hooks"]
    _seed_instance(app_env.db_path, "move-1", wrapper_id="wrap-move-1")
    writes = _spy_stamp_writes(hooks, monkeypatch)

    result = asyncio.run(
        hooks.handle_session_start(
            {
                "session_id": "move-1",
                "cwd": "/tmp/churn",
                "pid": 1000,
                "env": {"TMUX_PANE": "%new", "TOKEN_API_WRAPPER_ID": "wrap-move-1"},
            }
        )
    )

    assert result["success"] is True
    assert _stamp_mutations(writes) == []
