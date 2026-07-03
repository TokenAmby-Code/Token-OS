"""Regression test for the SQLite fd leak in the send gate's DB helpers.

The bug: ``with sqlite3.connect(...) as conn:`` commits/rolls back but never
*closes* the connection. Harmless in the old short-lived ``tmuxctl`` CLI (process
exit reaped the fd) but fatal in the long-lived ``tmuxctld`` daemon, which
stranded one connection — one open file descriptor against ``agents.db`` — per
call. The send path leaked two per send-text and eventually hit "too many open
files". The fix wraps every connect in ``contextlib.closing(...)`` (keeping the
trailing ``, conn`` so commit/rollback semantics are preserved).

Rather than count process file descriptors (platform-specific, needs psutil),
this wraps ``sqlite3.connect`` and asserts every connection the gate helpers open
is also closed — a direct, deterministic check of the fix. RED before it (the
unclosed connections never reach ``close``), GREEN after.
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import tmuxctl.send_gate as send_gate  # noqa: E402


@pytest.fixture
def db(tmp_path):
    """A temp agents.db with the tables the gate helpers touch."""
    path = tmp_path / "agents.db"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE day_state (
            date TEXT PRIMARY KEY, day_started_at TEXT, source TEXT,
            details_json TEXT, created_at TEXT, updated_at TEXT)"""
    )
    conn.execute(
        "CREATE TABLE timer_state (id INTEGER PRIMARY KEY, state_json TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_type TEXT, "
        "instance_id TEXT, device_id TEXT, details TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.execute(
        """CREATE TABLE automated_pane_activity (
            tmux_pane TEXT PRIMARY KEY, injected_at TEXT, expires_at TEXT,
            source TEXT, verb TEXT)"""
    )
    conn.commit()
    conn.close()
    return path


def test_send_gate_helpers_close_every_connection(db, monkeypatch):
    """Driving the read + write helpers must close every connection they open.

    ``quiet_hours_active`` opens two connections (``_read_day_state`` +
    ``_session_quiet_latch``); ``record_suppression`` and
    ``register_automated_send`` each open one for a write. Pre-fix none of them
    reached ``close``. We wrap ``sqlite3.connect`` (via a Connection subclass so
    ``close`` can be tracked — the C type forbids instance attributes) and assert
    every opened connection is also closed.
    """
    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []  # keep refs so ids stay stable
    closed_ids: set[int] = set()

    class _Tracked(sqlite3.Connection):
        def close(self) -> None:
            closed_ids.add(id(self))
            super().close()

    def tracking_connect(*args, **kwargs):
        kwargs.setdefault("factory", _Tracked)
        conn = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)

    args = ("send-keys", "-t", "%1", "hello")
    for _ in range(5):
        send_gate.quiet_hours_active(db_path=db)
        result = send_gate.evaluate(args, db_path=db)
        if result is not None:
            send_gate.record_suppression(result, db_path=db)
        send_gate.register_automated_send(args, db_path=db)

    assert opened, "no connections were opened — test wired wrong"
    leaked = [id(c) for c in opened if id(c) not in closed_ids]
    assert not leaked, (
        f"{len(opened)} connections opened, {len(closed_ids)} closed — "
        f"{len(leaked)} leaked (missing contextlib.closing)"
    )


def test_daemon_sqlite_connects_are_wrapped_in_closing() -> None:
    """Long-lived daemon modules must not rely on ``with sqlite3.connect`` alone."""
    repo_root = ROOT.parent
    checked = [
        repo_root / "tmuxctld" / "lib" / "tmuxctl" / "send_gate.py",
        repo_root / "tmuxctld" / "lib" / "tmuxctl" / "service.py",
    ]
    for path in checked:
        tree = ast.parse(path.read_text())
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not _is_sqlite_connect_call(node):
                continue
            parent = parents.get(node)
            assert isinstance(parent, ast.Call) and _is_closing_call(parent), (
                f"{path}:{node.lineno} must wrap sqlite3.connect in contextlib.closing"
            )


def _is_sqlite_connect_call(node: ast.Call) -> bool:
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "connect"
        and isinstance(func.value, ast.Name)
        and func.value.id == "sqlite3"
    )


def _is_closing_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "closing"
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "closing"
        and isinstance(func.value, ast.Name)
        and func.value.id == "contextlib"
    )
