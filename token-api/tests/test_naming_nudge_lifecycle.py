from __future__ import annotations

import sqlite3
import sys

import pytest


def _insert_wrapper_instance(db_path, *, instance_id="wrap-unnamed", wrapper_id="wrap-1") -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO instances (
                id, name, engine, working_dir, device_id, status, rank,
                wrapper_launch_id, tmux_pane, session_doc_id
            ) VALUES (?, 'needs-name', 'codex', '/tmp', 'Mac-Mini', 'working',
                      'astartes', ?, '%9', NULL)
            """,
            (instance_id, wrapper_id),
        )


@pytest.mark.asyncio
async def test_wrapper_end_schedules_harness_agnostic_naming_nudge(app_env, monkeypatch) -> None:
    """Wrapper-only Codex launches may miss Stop/SessionEnd; terminal WrapperEnd
    must still route through the same unnamed-pane nudge policy.
    """
    hooks = sys.modules["routes.hooks"]
    _insert_wrapper_instance(app_env.db_path)

    scheduled: list[tuple[str | None, str]] = []
    monkeypatch.setattr(
        hooks, "_schedule_naming_nudge", lambda iid, source: scheduled.append((iid, source))
    )
    monkeypatch.setattr(hooks.shared, "clear_pane_tint", lambda *a, **k: None)

    result = await hooks.handle_wrapper_end(
        {
            "wrapper_launch_id": "wrap-1",
            "engine": "codex",
            "launcher": "codex-wrapper",
            "tmux_pane": "%9",
            "env": {"TOKEN_API_WRAPPER_LAUNCH_ID": "wrap-1", "TMUX_PANE": "%9"},
        }
    )

    assert result["action"] == "wrapper_end_stopped_instance"
    assert scheduled == [("wrap-unnamed", "WrapperEnd")]


@pytest.mark.asyncio
async def test_session_end_schedules_harness_agnostic_naming_nudge(app_env, monkeypatch) -> None:
    """SessionEnd is also terminal; the rename interview should not depend on
    Claude's separate naming-nudge shell shim or Codex's Stop hook.
    """
    hooks = sys.modules["routes.hooks"]
    _insert_wrapper_instance(app_env.db_path, instance_id="sess-unnamed", wrapper_id="wrap-2")

    scheduled: list[tuple[str | None, str]] = []
    monkeypatch.setattr(
        hooks, "_schedule_naming_nudge", lambda iid, source: scheduled.append((iid, source))
    )
    monkeypatch.setattr(hooks.shared, "clear_pane_tint", lambda *a, **k: None)
    monkeypatch.setattr(hooks, "_spawn_session_end_assertion", lambda *a, **k: None)

    result = await hooks.handle_session_end(
        {"session_id": "sess-unnamed", "wrapper_launch_id": "wrap-2", "reason": "logout"}
    )

    assert result["action"] == "stopped"
    assert scheduled == [("sess-unnamed", "SessionEnd")]
