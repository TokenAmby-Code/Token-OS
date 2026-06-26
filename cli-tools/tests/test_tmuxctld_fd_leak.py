"""Regression test for the SQLite fd leak in the send gate's DB helpers.

The bug: ``with sqlite3.connect(...) as conn:`` commits/rolls back but never
*closes* the connection. Harmless in the old short-lived ``tmuxctl`` CLI (process
exit reaped the fd) but fatal in the long-lived ``tmuxctld`` daemon, which
stranded one connection — one open file descriptor against ``agents.db`` — per
call. The send path leaked two per send-text and eventually hit "too many open
files". The fix wraps every connect in ``contextlib.closing(...)`` (keeping the
trailing ``, conn`` so commit/rollback semantics are preserved).

This drives the gate's read AND write helpers in a loop against a temp DB and
asserts the process's open-fd count against the db file stays bounded — it must
not grow with iterations. Runtime over mocks: it exercises the real connect path.

RED before the fix (fd count climbs ~2/iteration), GREEN after.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

import psutil
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import tmuxctl.send_gate as send_gate


@pytest.fixture
def db(tmp_path, monkeypatch):
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
    monkeypatch.setenv("TOKEN_API_DB", str(path))
    return path


def _open_db_fds(db_path: pathlib.Path) -> int:
    """Count this process's open file descriptors pointing at ``db_path``."""
    target = str(pathlib.Path(db_path).resolve())
    proc = psutil.Process()
    count = 0
    for handle in proc.open_files():
        try:
            if str(pathlib.Path(handle.path).resolve()) == target:
                count += 1
        except OSError:
            continue
    return count


def test_send_gate_helpers_do_not_leak_db_fds(db):
    """Driving the read + write helpers many times must not strand connections.

    Each ``quiet_hours_active`` call opens two connections (``_read_day_state`` +
    ``_session_quiet_latch``); ``record_suppression`` and ``register_automated_send``
    each open one for a write. Pre-fix every one leaked an fd. We warm up to reach
    steady state, snapshot the open-fd count against the db, then loop hard and
    assert the count has not grown.
    """
    args = ("send-keys", "-t", "%1", "hello")

    def drive() -> None:
        send_gate.quiet_hours_active(db_path=db)
        result = send_gate.evaluate(args, db_path=db)
        if result is not None:
            send_gate.record_suppression(result, db_path=db)
        send_gate.register_automated_send(args, db_path=db)

    # Warm up so any one-time fds (module/table caches) settle before the snapshot.
    for _ in range(10):
        drive()
    baseline = _open_db_fds(db)

    for _ in range(150):
        drive()
    after = _open_db_fds(db)

    # With the fix, every connection is closed, so the count is flat (≈0). A leak
    # would add roughly four fds per iteration → hundreds. Allow a tiny tolerance
    # for transient WAL/-shm handles, but nothing that scales with iterations.
    assert after <= baseline + 3, (
        f"db fd count grew from {baseline} to {after} across 150 iterations — "
        "a sqlite connection is being leaked (missing contextlib.closing)"
    )
