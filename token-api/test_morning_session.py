"""Unit tests for the morning-session launcher's tmux-injection resilience.

Historical regression (P0, 2026-06-05): ``run_morning_session()`` crashed when
the tmuxctl ``stack enforce`` pre-assertion hit its 5s timeout — the uncaught
``subprocess.TimeoutExpired`` propagated out of the seat resolver into
``run_morning_session()``, so the Emperor was never placed into morning-session
mode and the break was never paused.

That pre-assertion targeted the per-fleet ``legion`` stack, which was retired
into the council page. ``resolve_custodes_pane()`` now resolves the durable fixed
``council:custodes`` seat with NO stack-enforce step, so the regression is
structurally impossible — ``resolve-pane`` is the only operation, and it already
fails closed (returns None) when the seat is absent.

Run:
    cd token-api && .venv/bin/python -m pytest test_morning_session.py -v
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import morning_session

# ── Helpers ───────────────────────────────────────────────────


def _completed(cmd, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def _cmd_is(cmd, *needles):
    """True if every needle appears in the subprocess argv list."""
    return all(n in cmd for n in needles)


# ── resolve_custodes_pane: the durable council seat, no stack-enforce ─


def test_resolve_custodes_pane_never_invokes_stack_enforce():
    """The retired ``legion`` stack pre-assertion is gone: resolving the fixed
    council seat must call ``resolve-pane`` ONLY — any ``stack enforce`` call is a
    regression (and a hard error against a non-stack base)."""
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append(list(cmd))
        if _cmd_is(cmd, "resolve-pane"):
            return _completed(cmd, 0, stdout="%42\n")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch("morning_session.subprocess.run", side_effect=fake_run):
        pane = morning_session.resolve_custodes_pane()
    assert pane == "%42"
    # Resolves the durable council seat, never the retired legion window.
    flat = [tok for cmd in seen for tok in cmd]
    assert any("council:custodes" in tok for tok in flat), seen
    assert not any(tok.endswith(":legion") for tok in flat), seen


def test_resolve_custodes_pane_normal_path():
    """Control: resolve-pane succeeds → resolved pane."""

    def fake_run(cmd, **kwargs):
        if _cmd_is(cmd, "resolve-pane"):
            return _completed(cmd, 0, stdout="%99\n")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch("morning_session.subprocess.run", side_effect=fake_run):
        pane = morning_session.resolve_custodes_pane()
    assert pane == "%99"


def test_resolve_custodes_pane_resolve_timeout_returns_none():
    """If resolve-pane times out, fail gracefully (None), never raise."""

    def fake_run(cmd, **kwargs):
        if _cmd_is(cmd, "resolve-pane"):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 5))
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch("morning_session.subprocess.run", side_effect=fake_run):
        pane = morning_session.resolve_custodes_pane()
    assert pane is None


def test_resolve_custodes_pane_resolve_nonzero_returns_none():
    """Control: resolve-pane rc!=0 → None (seat absent fails closed)."""

    def fake_run(cmd, **kwargs):
        if _cmd_is(cmd, "resolve-pane"):
            return _completed(cmd, 1, stderr="no such pane")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch("morning_session.subprocess.run", side_effect=fake_run):
        pane = morning_session.resolve_custodes_pane()
    assert pane is None


def test_resolve_custodes_pane_resolve_empty_returns_none():
    """Control: resolve-pane rc=0 but empty stdout → None."""

    def fake_run(cmd, **kwargs):
        if _cmd_is(cmd, "resolve-pane"):
            return _completed(cmd, 0, stdout="   \n")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch("morning_session.subprocess.run", side_effect=fake_run):
        pane = morning_session.resolve_custodes_pane()
    assert pane is None


# ── run_morning_session: end-to-end survival of the timeout ───


@pytest.fixture
def isolated_morning_dir(tmp_path, monkeypatch):
    """Isolate the morning state file under tmp so we never touch real /tmp state."""
    monkeypatch.setenv("CUSTODES_MORNING_DIR", str(tmp_path))
    return tmp_path


def test_run_morning_session_reaches_active_via_council_seat(isolated_morning_dir):
    """End-to-end: resolving the council:custodes seat + assert-instance + send-text
    all succeeding AND a live Custodes confirming registration, run_morning_session()
    must reach status="active" — i.e. the Emperor is placed into morning-session
    mode. No stack-enforce pre-assertion is involved (the legion stack is retired).
    """

    def fake_run(cmd, **kwargs):
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
        "reconciled": True,
        "waited_s": 0.0,
    }

    day_state = {"day_started_at": "2026-06-15T06:00:00", "source": "morning"}
    with (
        patch("shared.get_day_state_sync", lambda today=None, db_path=None: day_state),
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

        # The state file is durably written for audit/debug, but first-class
        # timer mode is now the only morning liveness source.
        state = morning_session.read_morning_state()
        assert state is not None
        assert state["status"] == "active"
        assert state["confirmed_instance_id"] == "cafe1234"
        active, reason = morning_session.morning_session_active()
        assert active is False
        assert reason.startswith("timer_mode:")


def test_run_morning_session_marks_failed_when_custodes_never_registers(isolated_morning_dir):
    """Launch sent but no live sync Custodes registers → status="failed".

    Closes the validation gap: send-text succeeding is NOT proof a Custodes is up.
    When confirmation times out, the state file must flip to "failed" so the
    keepalive does NOT re-inject into a phantom, and a warning TTS must fire.
    """

    def fake_run(cmd, **kwargs):
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
        "reconciled": False,
        "waited_s": 90.0,
    }
    tts_messages: list[str] = []

    day_state = {"day_started_at": "2026-06-15T06:00:00", "source": "morning"}
    with (
        patch("shared.get_day_state_sync", lambda today=None, db_path=None: day_state),
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


def test_run_morning_session_falls_back_when_custodes_pane_unregistered(
    isolated_morning_dir: Path,
) -> None:
    """A live but unregistered council:custodes pane must not brick the morning.

    This is the 2026-06-15 failure mode: SessionStart registration was lost, so
    tmuxctl assert-instance returned persona_unregistered_live_runtime. The
    morning launcher must dispatch a fresh Custodes (mechanicus:new) instead of
    failing closed.
    """

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if _cmd_is(cmd, "resolve-pane"):
            return _completed(cmd, 0, stdout="%86\n")
        if _cmd_is(cmd, "assert-instance"):
            return _completed(
                cmd,
                1,
                stdout=json.dumps(
                    {
                        "ok": False,
                        "pane": "%86",
                        "action": "persona_unregistered_noted",
                        "reason": "persona_unregistered_live_runtime",
                    }
                ),
            )
        if "dispatch" in str(cmd[0]):
            assert "mechanicus:new" in cmd
            return _completed(cmd, 0, stdout="dispatched\n")
        raise AssertionError(f"unexpected cmd: {cmd}")

    confirmed = {
        "live": True,
        "instance_id": "fresh-custodes",
        "tmux_pane": "%101",
        "pane_matched": False,
        "reconciled": True,
        "waited_s": 0.0,
    }
    confirm = MagicMock(return_value=confirmed)
    day_state = {"day_started_at": "2026-06-15T07:32:00", "source": "custodes"}
    with (
        patch("shared.get_day_state_sync", lambda today=None, db_path=None: day_state),
        patch("morning_session.subprocess.run", side_effect=fake_run),
        patch("morning_session.ensure_daily_notes", lambda: None),
        patch("morning_session.get_daily_thread_id", lambda today: "thread123"),
        patch("morning_session.create_daily_thread", lambda today: "thread123"),
        patch("morning_session.send_tts", lambda msg: None),
        patch("morning_session.confirm_custodes_registered", confirm),
        patch("nas_mount.ensure_mounted", lambda share, **kw: (True, "ok")),
    ):
        result = morning_session.run_morning_session()

    assert result["status"] == "active"
    assert result["instance_id"] == "fresh-custodes"
    assert result["pane_id"] == "%101"
    assert any("dispatch" in str(cmd[0]) and "mechanicus:new" in cmd for cmd in calls)
    assert not any(_cmd_is(cmd, "send-text") for cmd in calls)
    confirm.assert_called_once()
    assert confirm.call_args.kwargs["pane_id"] is None
    assert confirm.call_args.kwargs["exclude_pane_id"] == "%86"


def test_run_morning_session_no_pane_fails_gracefully(isolated_morning_dir):
    """If the pane cannot be resolved at all, fail cleanly (no_pane), never raise."""

    def fake_run(cmd, **kwargs):
        if _cmd_is(cmd, "resolve-pane"):
            # Even resolve-pane is wedged — graceful degradation, not a crash.
            raise subprocess.TimeoutExpired(cmd, 5)
        raise AssertionError(f"unexpected cmd: {cmd}")

    day_state = {"day_started_at": "2026-06-15T06:00:00", "source": "morning"}
    with (
        patch("shared.get_day_state_sync", lambda today=None, db_path=None: day_state),
        patch("morning_session.subprocess.run", side_effect=fake_run),
        patch("morning_session.ensure_daily_notes", lambda: None),
        patch("morning_session.get_daily_thread_id", lambda today: "thread123"),
        patch("morning_session.create_daily_thread", lambda today: "thread123"),
        patch("morning_session.send_tts", lambda msg: None),
        patch("nas_mount.ensure_mounted", lambda share, **kw: (True, "ok")),
    ):
        result = morning_session.run_morning_session()

    assert result["status"] == "no_pane"


# ── run_morning_session: day-start latch guard (phantom killer, #101) ─


def _no_launch_run(cmd, **kwargs):
    """subprocess.run stub that fails loudly if the launch path is reached."""
    raise AssertionError(f"launch path must not run for a phantom: {cmd}")


def test_run_morning_session_refuses_without_day_start_latch(isolated_morning_dir):
    """Phantom: a bare /api/morning/start with NO day_state latch must NOT launch.

    The legacy phone macro POSTs /api/morning/start directly, bypassing the
    day-start latch. run_morning_session must refuse (status="no_day_start_latch")
    and never reach resolve_custodes_pane / confirm — no ghost Custodes spawned.
    """
    create_pane = MagicMock()
    confirm = MagicMock()
    with (
        patch("shared.get_day_state_sync", lambda today=None, db_path=None: None),
        patch("morning_session.subprocess.run", side_effect=_no_launch_run),
        patch("morning_session.ensure_daily_notes", lambda: None),
        patch("morning_session.get_daily_thread_id", lambda today: "t"),
        patch("morning_session.create_daily_thread", lambda today: "t"),
        patch("morning_session.send_tts", lambda msg: None),
        patch("morning_session.resolve_custodes_pane", create_pane),
        patch("morning_session.confirm_custodes_registered", confirm),
        patch("nas_mount.ensure_mounted", lambda share, **kw: (True, "ok")),
    ):
        result = morning_session.run_morning_session()

    assert result["status"] == "no_day_start_latch"
    create_pane.assert_not_called()
    confirm.assert_not_called()
    # State file records the refusal but is NOT an active morning.
    state = morning_session.read_morning_state()
    assert state is not None
    assert state["status"] == "no_day_start_latch"
    active, _reason = morning_session.morning_session_active()
    assert active is False


def test_run_morning_session_refuses_non_official_day_start_source(isolated_morning_dir):
    """A day_state latched by a non-official source (e.g. schedule_fallback) is
    still not an Emperor ack → refuse with a source-specific reason."""
    create_pane = MagicMock()
    day_state = {"day_started_at": "2026-06-07T08:30:00", "source": "schedule_fallback"}
    with (
        patch("shared.get_day_state_sync", lambda today=None, db_path=None: day_state),
        patch("morning_session.subprocess.run", side_effect=_no_launch_run),
        patch("morning_session.ensure_daily_notes", lambda: None),
        patch("morning_session.get_daily_thread_id", lambda today: "t"),
        patch("morning_session.create_daily_thread", lambda today: "t"),
        patch("morning_session.send_tts", lambda msg: None),
        patch("morning_session.resolve_custodes_pane", create_pane),
        patch("nas_mount.ensure_mounted", lambda share, **kw: (True, "ok")),
    ):
        result = morning_session.run_morning_session()

    assert result["status"] == "no_day_start_latch"
    assert "schedule_fallback" in result["reason"]
    create_pane.assert_not_called()


def test_run_morning_session_proceeds_with_real_alarm_ack(isolated_morning_dir):
    """Control: the real wake (day_state latched source=alarm_silenced) passes the
    guard and reaches a normal active launch — the guard must NOT block the real
    path that this morning's 11:16 wake exercised."""

    def fake_run(cmd, **kwargs):
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
        "reconciled": True,
        "waited_s": 0.0,
    }
    day_state = {"day_started_at": "2026-06-07T11:16:00", "source": "alarm_silenced"}
    with (
        patch("shared.get_day_state_sync", lambda today=None, db_path=None: day_state),
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
    assert result["instance_id"] == "cafe1234"


# ── find_live_custodes + reconcile: persona + rank identity ───


def test_find_live_custodes_matches_by_persona_and_rank():
    """The custodes is found by persona.slug + non-retired rank, NOT by sync.

    The canonical /api/instances surface exposes persona.slug + rank + normalized
    status and carries NO legion/instance_type/synced, so the locator resolves on
    identity. A resting custodes with no sync marker is still THE custodes.
    """
    instances = [
        # Retired (superseded) custodes — must be skipped.
        {
            "id": "dead1",
            "persona": {"slug": "custodes"},
            "rank": "retired",
            "status": "stopped",
        },
        # Live custodes, no sync marker anywhere — found by identity alone.
        {
            "id": "live1",
            "persona": {"slug": "custodes"},
            "rank": "overseer",
            "status": "working",
            "runtime": {"tmux_pane": "%9"},
        },
    ]
    with patch("morning_session._get", lambda path: instances):
        inst = morning_session.find_live_custodes()
    assert inst is not None
    assert inst["id"] == "live1"


def test_find_live_custodes_trusts_tmux_liveness_over_stale_status():
    """A live @INSTANCE_ID stamp beats a stale stopped DB status.

    This is the Morning Supervisor false-relaunch regression: /api/instances can
    contain a stale/dead durable status during SessionStart lock contention or
    registry freeze, but the runtime overlay proves the Custodes pane is alive.
    """
    instances = [
        {
            "id": "custodes-stale-dead",
            "persona": {"slug": "custodes"},
            "rank": "overseer",
            "status": "idle",
            "durable_status": "stopped",
            "stale_status": "stopped",
            "status_source": "tmuxctl-live-overlay",
            "runtime": {
                "live_pane": True,
                "tmux_pane": "%47",
                "pane_label": "council:custodes",
            },
        }
    ]
    with patch("morning_session._get", lambda path: instances):
        inst = morning_session.find_live_custodes()
    assert inst is not None
    assert inst["id"] == "custodes-stale-dead"


def test_find_live_custodes_none_when_no_custodes_alive():
    """No live custodes persona → None (the one genuine launch failure)."""
    instances = [
        # Another persona, even if sync-shaped, is not the custodes.
        {
            "id": "x",
            "persona": {"slug": "fabricator-general"},
            "rank": "primarch",
            "status": "working",
        },
        # A custodes row that is retired/stopped does not count as alive.
        {"id": "y", "persona": {"slug": "custodes"}, "rank": "retired", "status": "stopped"},
        {"id": "z", "persona": {"slug": "custodes"}, "rank": "overseer", "status": "archived"},
    ]
    with patch("morning_session._get", lambda path: instances):
        assert morning_session.find_live_custodes() is None


def test_reconcile_custodes_active_sets_sync_mode():
    """Reconcile sets sync MODE (best-effort) on the resolved custodes row."""
    sent: dict = {}

    def fake_patch(path, data=None):
        sent[path] = data
        return {"ok": True}

    inst = {"id": "abc123def456", "persona": {"slug": "custodes"}, "rank": "overseer"}
    with patch("morning_session._patch", side_effect=fake_patch):
        result = morning_session.reconcile_custodes_active(inst)
    assert result["reconciled"] is True
    assert sent["/api/instances/abc123def456/type"] == {"instance_type": "sync"}
    assert sent["/api/instances/abc123def456/synced"] == {"synced": True}


def test_reconcile_custodes_active_no_instance_id():
    """A row without an id cannot be reconciled — no PATCH calls."""
    calls: list = []
    inst = {"persona": {"slug": "custodes"}, "rank": "overseer"}
    with patch("morning_session._patch", side_effect=lambda *a, **k: calls.append(a)):
        result = morning_session.reconcile_custodes_active(inst)
    assert result["reconciled"] is False
    assert result["reason"] == "no_instance_id"
    assert calls == []


def test_confirm_custodes_registered_finds_by_identity():
    """confirm finds the custodes by identity, sets sync mode, returns live.

    The live pane comes from runtime.tmux_pane (pane identity is never durably
    stored on the canonical row).
    """
    inst = {
        "id": "live9",
        "persona": {"slug": "custodes"},
        "rank": "overseer",
        "status": "working",
        "runtime": {"tmux_pane": "%9"},
    }
    with (
        patch("morning_session._get", lambda path: [inst]),
        patch("morning_session._patch", lambda path, data=None: {"ok": True}),
    ):
        result = morning_session.confirm_custodes_registered(
            pane_id="%9", timeout_s=1, interval_s=0
        )
    assert result["live"] is True
    assert result["instance_id"] == "live9"
    assert result["pane_matched"] is True
    assert result["reconciled"] is True
