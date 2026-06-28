"""SessionEnd ↔ tmuxctl boundary: the instance hook must never reach across.

P0 cull (09:39): a SessionEnd fired during a plan-mode context-clear and
``handle_session_end`` spawned the tmuxctl ``assert-instance`` COLD path (which
bypasses the daemon) against four live panes — culling working agents. Root
cause is a boundary violation, not a wrong heuristic.

DOCTRINE: tmuxctld owns the wrapper/pane; token-api owns the instance. SessionEnd
is an instance-level signal. It updates the instance DB row and STOPS. It must
NOT:
  * spawn ``tmuxctl assert-instance`` (the severed COLD path), or
  * invoke any tmuxctl pane-control (``clear_pane_tint`` / pane prune), or
  * even resolve pane geometry — pane teardown is tmuxctld's job, driven by
    wrapper-level signals only.

These are RED before the boundary fix (SessionEnd resolved the pane, cleared its
tint, and spawned the assert-instance subprocess) and GREEN after.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from datetime import datetime


def _insert(db_path, instance_id, *, status="working", is_subagent=0):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO legacy_instances
           (id, session_id, tab_name, working_dir, origin_type, device_id,
            status, is_subagent, last_activity)
           VALUES (?, ?, ?, '/tmp', 'local', 'Mac-Mini', ?, ?, ?)""",
        (
            instance_id,
            f"{instance_id}-session",
            instance_id,
            status,
            is_subagent,
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def _status(db_path, instance_id):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT status FROM instances WHERE id = ?", (instance_id,)).fetchone()
    conn.close()
    return row[0] if row else None


def _install_boundary_spies(hooks, monkeypatch):
    """Record every cross-boundary seam SessionEnd could touch.

    Returns (tmuxctl_calls, tint_calls, resolve_calls). The stop_hook spawn is a
    non-tmuxctl argv and runs as a no-op; only assert-instance / tmuxctl argv are
    recorded as a violation.
    """
    tmuxctl_calls: list[tuple[str, ...]] = []
    tint_calls: list[tuple] = []
    resolve_calls: list[str] = []

    def _spy_popen(args, *_a, **_k):
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        if any("assert-instance" in str(x) or str(x).endswith("tmuxctl") for x in argv):
            tmuxctl_calls.append(tuple(str(x) for x in argv))
        return None

    def _spy_clear_tint(*a, **k):
        tint_calls.append((a, k))

    async def _spy_resolve(instance_id):
        resolve_calls.append(instance_id)
        return (None, None)

    monkeypatch.setattr(hooks.subprocess, "Popen", _spy_popen)
    monkeypatch.setattr(hooks.shared, "clear_pane_tint", _spy_clear_tint)
    monkeypatch.setattr(hooks.shared, "resolve_instance_pane", _spy_resolve)
    return tmuxctl_calls, tint_calls, resolve_calls


def test_terminal_session_end_updates_row_but_never_touches_tmux(app_env, monkeypatch):
    """A terminal SessionEnd on a LIVE row stops the row (instance-domain) and
    invokes NO tmuxctl / pane-control / pane-resolution at all."""
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "live-1", status="working")
    tmuxctl_calls, tint_calls, resolve_calls = _install_boundary_spies(hooks, monkeypatch)

    result = asyncio.run(hooks.handle_session_end({"session_id": "live-1", "reason": "logout"}))

    # token-api domain: the instance row is updated/stopped.
    assert result["action"] == "stopped"
    assert _status(app_env.db_path, "live-1") == "stopped"

    # tmuxctld domain: untouched.
    assert tmuxctl_calls == [], f"SessionEnd must not invoke tmuxctl: {tmuxctl_calls}"
    assert tint_calls == [], f"SessionEnd must not invoke pane-control (tint): {tint_calls}"
    assert resolve_calls == [], (
        f"SessionEnd must not resolve pane geometry across the boundary: {resolve_calls}"
    )


def test_missing_reason_session_end_updates_row_but_never_touches_tmux(app_env, monkeypatch):
    """A missing/unknown reason defaults to terminal teardown — same boundary."""
    hooks = sys.modules["routes.hooks"]
    _insert(app_env.db_path, "live-2", status="working")
    tmuxctl_calls, tint_calls, resolve_calls = _install_boundary_spies(hooks, monkeypatch)

    result = asyncio.run(hooks.handle_session_end({"session_id": "live-2"}))

    assert result["action"] == "stopped"
    assert _status(app_env.db_path, "live-2") == "stopped"
    assert tmuxctl_calls == []
    assert tint_calls == []
    assert resolve_calls == []


def test_session_end_not_found_does_not_touch_tmux(app_env, monkeypatch):
    """The not-found branch (no matching row) must also not reach across — the
    old code spawned an assert-instance against the payload's fallback pane."""
    hooks = sys.modules["routes.hooks"]
    tmuxctl_calls, tint_calls, _resolve_calls = _install_boundary_spies(hooks, monkeypatch)

    result = asyncio.run(
        hooks.handle_session_end(
            {
                "session_id": "ghost",
                "reason": "logout",
                "tmux_pane": "%999",
                "pane_label": "council:custodes",
            }
        )
    )

    assert result["action"] == "not_found"
    assert tmuxctl_calls == [], f"not-found SessionEnd must not invoke tmuxctl: {tmuxctl_calls}"
    assert tint_calls == []
