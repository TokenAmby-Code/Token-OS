"""Tests for the automated-activation marker the send gate writes for run() sends.

Every send through TmuxAdapter.run() is automated by construction (humans type
directly into tmux, never through run()), so the gate stamps the target pane with
a marker that token-api's compute_work_state uses to discount the woken agent's
reflex activity from productivity accounting. These tests pin the marker-writing
contract: only mutating send verbs with a resolved -t target write a marker, the
upsert slides the window forward (last writer wins, one row per pane), the send
itself is never broken by a marker failure, and the TTL honors its env override.
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import pytest
import tmuxctl.send_gate as send_gate
import tmuxctl.tmux_adapter as tmux_adapter
from tmuxctl.tmux_adapter import TmuxAdapter

_SCHEMA = """
CREATE TABLE automated_pane_activity (
    tmux_pane   TEXT PRIMARY KEY,
    injected_at TIMESTAMP NOT NULL,
    expires_at  TIMESTAMP NOT NULL,
    source      TEXT,
    verb        TEXT
);
"""


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "agents.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()
    monkeypatch.setenv("TOKEN_API_DB", str(path))
    return path


def _markers(path):
    with sqlite3.connect(path) as conn:
        return conn.execute(
            "SELECT tmux_pane, source, verb, injected_at, expires_at FROM automated_pane_activity"
        ).fetchall()


def test_marker_written_only_for_send_verb_with_target(db_path):
    # Reads write nothing.
    send_gate.register_automated_send(("display-message", "-p", "#{x}"))
    # A send verb without a -t target writes nothing (no pane to attribute to).
    send_gate.register_automated_send(("send-keys", "hello", "Enter"))
    assert _markers(db_path) == []

    # A send verb with a resolved -t writes exactly one marker for that pane.
    send_gate.register_automated_send(("send-keys", "-t", "%42", "hi", "Enter"), source="pytest")
    rows = _markers(db_path)
    assert len(rows) == 1
    assert rows[0][0] == "%42"
    assert rows[0][1] == "pytest"
    assert rows[0][2] == "send-keys"


def test_marker_target_supports_combined_t_form(db_path):
    send_gate.register_automated_send(("send", "-t%99", "x"))
    panes = {r[0] for r in _markers(db_path)}
    assert "%99" in panes


def test_marker_upsert_slides_window_last_writer_wins(db_path):
    send_gate.register_automated_send(("send-keys", "-t", "%42", "hi"), source="first")
    first_expires = _markers(db_path)[0][4]
    # A second send to the same pane slides the window forward, never duplicates.
    send_gate.register_automated_send(("paste-buffer", "-t", "%42"), source="second")
    rows = _markers(db_path)
    assert len(rows) == 1
    assert rows[0][1] == "second"
    assert rows[0][2] == "paste-buffer"
    assert rows[0][4] >= first_expires


def test_marker_window_matches_ttl(db_path):
    send_gate.register_automated_send(("send-keys", "-t", "%42", "hi"))
    _, _, _, injected_at, expires_at = _markers(db_path)[0]
    span = (
        datetime.fromisoformat(expires_at) - datetime.fromisoformat(injected_at)
    ).total_seconds()
    assert span == pytest.approx(send_gate.automated_activity_ttl(), abs=1)


def test_ttl_env_override_and_fallback(monkeypatch):
    monkeypatch.setenv("TMUXCTL_AUTOMATED_ACTIVITY_TTL", "5")
    assert send_gate.automated_activity_ttl() == 5
    monkeypatch.setenv("TMUXCTL_AUTOMATED_ACTIVITY_TTL", "garbage")
    assert send_gate.automated_activity_ttl() == 90
    monkeypatch.delenv("TMUXCTL_AUTOMATED_ACTIVITY_TTL", raising=False)
    assert send_gate.automated_activity_ttl() == 90


def test_marker_write_failure_never_raises(monkeypatch):
    # A missing/unwritable DB must be swallowed — a marker failure can never break a send.
    monkeypatch.setenv("TOKEN_API_DB", "/nonexistent/dir/agents.db")
    send_gate.register_automated_send(("send-keys", "-t", "%42", "hi"))  # must not raise


def test_run_records_marker_before_send_and_skips_suppressed(monkeypatch, db_path):
    """run() stamps the pane on a real send, but a gated send writes no marker."""
    calls: list[list[str]] = []
    monkeypatch.setattr(
        tmux_adapter.subprocess,
        "run",
        lambda cmd, *a, **k: (
            calls.append(cmd) or type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        ),
    )

    adapter = TmuxAdapter(tmux_binary="tmux")

    # Gate open → send lands → marker written.
    monkeypatch.setattr(send_gate, "quiet_hours_active", lambda **kw: (False, {}))
    # evaluate() consults the send-path predicate (send_hold_active); keep the
    # border predicate in lockstep so both surfaces agree in this gate test.
    monkeypatch.setattr(send_gate, "typing_guard_active", lambda **kw: False)
    monkeypatch.setattr(send_gate, "send_hold_active", lambda **kw: False)
    adapter.run("send-keys", "-t", "%7", "hello")
    assert len(calls) == 1
    assert any(r[0] == "%7" for r in _markers(db_path))

    # Gate closed (typing) → send suppressed → no new marker for a fresh pane.
    monkeypatch.setattr(send_gate, "typing_guard_active", lambda **kw: True)
    monkeypatch.setattr(send_gate, "send_hold_active", lambda **kw: True)
    monkeypatch.setattr(send_gate, "sanctioned_override", lambda: None)
    monkeypatch.setenv("TMUX_SEND_GATE_POLICY", "cancel")
    adapter.run("send-keys", "-t", "%8", "blocked")
    assert all(r[0] != "%8" for r in _markers(db_path)), "suppressed send must write no marker"
