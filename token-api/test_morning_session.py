"""Unit tests for the morning-session launcher's tmux-injection resilience.

Regression under test (P0, 2026-06-05): ``run_morning_session()`` crashed when
the tmuxctl ``stack enforce`` pre-assertion hit its 5s timeout. The uncaught
``subprocess.TimeoutExpired`` propagated out of ``create_legion_pane()`` ->
``run_morning_session()``, so the Emperor was never placed into morning-session
mode and the break was never paused. The morning launch MUST survive a slow or
hung stack-enforce: the legion stack is persistent and ``resolve-pane`` is the
operation that actually gates the launch.

Run:
    cd token-api && .venv/bin/python -m pytest test_morning_session.py -v
"""

import json
import subprocess
from unittest.mock import patch

import pytest

import morning_session

# ── Helpers ───────────────────────────────────────────────────


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def _cmd_is(cmd, *needles):
    """True if every needle appears in the subprocess argv list."""
    return all(n in cmd for n in needles)


# ── create_legion_pane: the stack-enforce timeout path ────────


def test_create_legion_pane_survives_stack_enforce_timeout():
    """stack-enforce timing out (5s) must NOT crash; resolve-pane still gates."""

    def fake_run(cmd, **kwargs):
        if _cmd_is(cmd, "stack", "enforce"):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 5))
        if _cmd_is(cmd, "resolve-pane"):
            return _completed(cmd, 0, stdout="%42\n")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch("morning_session.subprocess.run", side_effect=fake_run):
        pane = morning_session.create_legion_pane()
    assert pane == "%42"


def test_create_legion_pane_survives_stack_enforce_error():
    """Any stack-enforce failure (not just timeout) is best-effort, non-fatal."""

    def fake_run(cmd, **kwargs):
        if _cmd_is(cmd, "stack", "enforce"):
            raise OSError("tmuxctl exploded")
        if _cmd_is(cmd, "resolve-pane"):
            return _completed(cmd, 0, stdout="%7\n")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch("morning_session.subprocess.run", side_effect=fake_run):
        pane = morning_session.create_legion_pane()
    assert pane == "%7"


def test_create_legion_pane_normal_path():
    """Control: both enforce and resolve-pane succeed → resolved pane."""

    def fake_run(cmd, **kwargs):
        if _cmd_is(cmd, "stack", "enforce"):
            return _completed(cmd, 0)
        if _cmd_is(cmd, "resolve-pane"):
            return _completed(cmd, 0, stdout="%99\n")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch("morning_session.subprocess.run", side_effect=fake_run):
        pane = morning_session.create_legion_pane()
    assert pane == "%99"


def test_create_legion_pane_resolve_timeout_returns_none():
    """If resolve-pane itself times out, fail gracefully (None), never raise."""

    def fake_run(cmd, **kwargs):
        if _cmd_is(cmd, "stack", "enforce"):
            return _completed(cmd, 0)
        if _cmd_is(cmd, "resolve-pane"):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 5))
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch("morning_session.subprocess.run", side_effect=fake_run):
        pane = morning_session.create_legion_pane()
    assert pane is None


def test_create_legion_pane_resolve_nonzero_returns_none():
    """Control: resolve-pane rc!=0 → None (already-correct behavior)."""

    def fake_run(cmd, **kwargs):
        if _cmd_is(cmd, "stack", "enforce"):
            return _completed(cmd, 0)
        if _cmd_is(cmd, "resolve-pane"):
            return _completed(cmd, 1, stderr="no such window")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch("morning_session.subprocess.run", side_effect=fake_run):
        pane = morning_session.create_legion_pane()
    assert pane is None


def test_create_legion_pane_resolve_empty_returns_none():
    """Control: resolve-pane rc=0 but empty stdout → None."""

    def fake_run(cmd, **kwargs):
        if _cmd_is(cmd, "stack", "enforce"):
            return _completed(cmd, 0)
        if _cmd_is(cmd, "resolve-pane"):
            return _completed(cmd, 0, stdout="   \n")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch("morning_session.subprocess.run", side_effect=fake_run):
        pane = morning_session.create_legion_pane()
    assert pane is None


# ── run_morning_session: end-to-end survival of the timeout ───


@pytest.fixture
def isolated_morning_dir(tmp_path, monkeypatch):
    """Isolate the morning state file under tmp so we never touch real /tmp state."""
    monkeypatch.setenv("CUSTODES_MORNING_DIR", str(tmp_path))
    return tmp_path


def test_run_morning_session_survives_stack_enforce_timeout(isolated_morning_dir):
    """End-to-end: a stack-enforce timeout must NOT abort the morning launch.

    With the enforce timing out but resolve-pane + assert-instance + send-text
    all succeeding AND a live Custodes confirming registration, run_morning_session()
    must reach status="active" — i.e. the Emperor is placed into morning-session
    mode despite the timeout.
    """

    def fake_run(cmd, **kwargs):
        if _cmd_is(cmd, "stack", "enforce"):
            raise subprocess.TimeoutExpired(cmd, 5)
        if _cmd_is(cmd, "resolve-pane"):
            return _completed(cmd, 0, stdout="%42\n")
        if _cmd_is(cmd, "assert-instance"):
            return _completed(cmd, 0, stdout=json.dumps({"ok": True, "action": "noop"}))
        if _cmd_is(cmd, "send-text"):
            return _completed(cmd, 0, stdout="")
        raise AssertionError(f"unexpected cmd: {cmd}")

    confirmed = {
        "live": True,
        "instance_id": "cafe1234",
        "tmux_pane": "%42",
        "pane_matched": True,
        "waited_s": 0.0,
    }

    with (
        patch("morning_session.subprocess.run", side_effect=fake_run),
        patch("morning_session.ensure_daily_notes", lambda: None),
        patch("morning_session.get_daily_thread_id", lambda today: "thread123"),
        patch("morning_session.create_daily_thread", lambda today: "thread123"),
        patch("morning_session.send_tts", lambda msg: None),
        patch("morning_session.confirm_custodes_registered", lambda **kw: confirmed),
        patch("nas_mount.ensure_mounted", lambda share, **kw: (True, "ok")),
    ):
        result = morning_session.run_morning_session()

        assert result["status"] == "active"
        assert result["pane_id"] == "%42"
        assert result["instance_id"] == "cafe1234"

        # The state file is durably written as active (validation confirmed a live
        # sync Custodes), so the keepalive gate (morning_session_active) reports the
        # session in-bound — the Emperor is genuinely placed into morning mode, not
        # just "launch returned ok".
        state = morning_session.read_morning_state()
        assert state is not None
        assert state["status"] == "active"
        assert state["confirmed_instance_id"] == "cafe1234"
        active, reason = morning_session.morning_session_active()
        assert active is True
        assert reason == "active"


def test_run_morning_session_marks_failed_when_custodes_never_registers(isolated_morning_dir):
    """Launch sent but no live sync Custodes registers → status="failed".

    Closes the validation gap: send-text succeeding is NOT proof a Custodes is up.
    When confirmation times out, the state file must flip to "failed" so the
    keepalive does NOT re-inject into a phantom, and a warning TTS must fire.
    """

    def fake_run(cmd, **kwargs):
        if _cmd_is(cmd, "stack", "enforce"):
            return _completed(cmd, 0)
        if _cmd_is(cmd, "resolve-pane"):
            return _completed(cmd, 0, stdout="%42\n")
        if _cmd_is(cmd, "assert-instance"):
            return _completed(cmd, 0, stdout=json.dumps({"ok": True, "action": "noop"}))
        if _cmd_is(cmd, "send-text"):
            return _completed(cmd, 0, stdout="")
        raise AssertionError(f"unexpected cmd: {cmd}")

    unconfirmed = {
        "live": False,
        "instance_id": None,
        "tmux_pane": None,
        "pane_matched": False,
        "waited_s": 90.0,
    }
    tts_messages: list[str] = []

    with (
        patch("morning_session.subprocess.run", side_effect=fake_run),
        patch("morning_session.ensure_daily_notes", lambda: None),
        patch("morning_session.get_daily_thread_id", lambda today: "thread123"),
        patch("morning_session.create_daily_thread", lambda today: "thread123"),
        patch("morning_session.send_tts", lambda msg: tts_messages.append(msg)),
        patch("morning_session.confirm_custodes_registered", lambda **kw: unconfirmed),
        patch("nas_mount.ensure_mounted", lambda share, **kw: (True, "ok")),
    ):
        result = morning_session.run_morning_session()

        assert result["status"] == "failed"
        assert result["reason"] == "custodes_not_registered"

        state = morning_session.read_morning_state()
        assert state is not None
        assert state["status"] == "failed"
        assert state["failed_reason"] == "custodes_not_registered"
        # Keepalive must NOT treat an unconfirmed launch as an active session.
        active, _reason = morning_session.morning_session_active()
        assert active is False
        # The Emperor is warned in-pathway (the supervisor is the redundant net).
        assert tts_messages and "could not be confirmed" in tts_messages[0]


def test_run_morning_session_no_pane_fails_gracefully(isolated_morning_dir):
    """If the pane cannot be resolved at all, fail cleanly (no_pane), never raise."""

    def fake_run(cmd, **kwargs):
        if _cmd_is(cmd, "stack", "enforce"):
            raise subprocess.TimeoutExpired(cmd, 5)
        if _cmd_is(cmd, "resolve-pane"):
            # Even resolve-pane is wedged — graceful degradation, not a crash.
            raise subprocess.TimeoutExpired(cmd, 5)
        raise AssertionError(f"unexpected cmd: {cmd}")

    with (
        patch("morning_session.subprocess.run", side_effect=fake_run),
        patch("morning_session.ensure_daily_notes", lambda: None),
        patch("morning_session.get_daily_thread_id", lambda today: "thread123"),
        patch("morning_session.create_daily_thread", lambda today: "thread123"),
        patch("morning_session.send_tts", lambda msg: None),
        patch("nas_mount.ensure_mounted", lambda share, **kw: (True, "ok")),
    ):
        result = morning_session.run_morning_session()

    assert result["status"] == "no_pane"
