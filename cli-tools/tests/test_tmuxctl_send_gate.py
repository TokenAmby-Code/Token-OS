"""Regression tests for the universal tmux send gate (the pane-write sentinel).

Invariant under test: quiet hours cancel automated pane writes by default;
the typing guard delays automated writes by default; sanctioned direct-input
sends pierce but are audited. Reads are never gated.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import pytest
import tmuxctl.send_gate as send_gate
import tmuxctl.tmux_adapter as tmux_adapter
from tmuxctl.tmux_adapter import TmuxAdapter


class _FakeCompleted:
    def __init__(self) -> None:
        self.returncode: int = 0
        self.stdout: str = ""
        self.stderr: str = ""


@pytest.fixture
def captured_subprocess(monkeypatch):
    """Replace subprocess.run in the adapter so no real tmux is invoked.

    Records every invocation so a gated (suppressed) send can be proven to have
    written nothing to a PTY.
    """
    calls: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return _FakeCompleted()

    monkeypatch.setattr(tmux_adapter.subprocess, "run", _fake_run)
    return calls


def test_run_allow_failure_uses_single_output_pipe(monkeypatch):
    calls = []

    def _fake_run(cmd, *args, **kwargs):
        calls.append((cmd, kwargs))
        proc = _FakeCompleted()
        proc.stdout = "%9\n"
        return proc

    monkeypatch.setattr(tmux_adapter.subprocess, "run", _fake_run)

    adapter = TmuxAdapter(tmux_binary="tmux")
    assert adapter.run("list-panes", "-t", "legion", allow_failure=True) == "%9\n"

    assert calls
    assert calls[0][1]["stdout"] is tmux_adapter.subprocess.PIPE
    assert calls[0][1]["stderr"] is tmux_adapter.subprocess.DEVNULL


def test_run_reports_emfile_as_tmux_error(monkeypatch):
    def _fake_run(cmd, *args, **kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(tmux_adapter.subprocess, "run", _fake_run)

    adapter = TmuxAdapter(tmux_binary="tmux")
    with pytest.raises(tmux_adapter.TmuxError) as excinfo:
        adapter.run("list-panes", "-t", "legion", allow_failure=True)

    assert "too many open files" in str(excinfo.value)
    assert "list-panes" in str(excinfo.value)


@pytest.fixture
def recorded_suppressions(monkeypatch):
    records: list[dict] = []
    monkeypatch.setattr(
        send_gate, "record_suppression", lambda result, **kw: records.append(result)
    )
    return records


def _force_quiet(monkeypatch, active: bool):
    monkeypatch.setattr(send_gate, "quiet_hours_active", lambda **kw: (active, {"forced": active}))


def _force_typing(monkeypatch, active: bool):
    monkeypatch.setattr(send_gate, "typing_guard_active", lambda **kw: active)


def _no_override(monkeypatch):
    monkeypatch.setattr(send_gate, "sanctioned_override", lambda: None)


# (a) send-keys / paste-buffer during quiet hours -> suppressed + logged, no PTY write.
@pytest.mark.parametrize(
    "verb_args",
    [("send-keys", "-t", "%9", "intervention"), ("paste-buffer", "-t", "%9", "-b", "buf")],
)
def test_run_suppresses_mutating_send_during_quiet_hours(
    monkeypatch, captured_subprocess, recorded_suppressions, verb_args
):
    _force_quiet(monkeypatch, True)
    _force_typing(monkeypatch, False)
    _no_override(monkeypatch)

    adapter = TmuxAdapter(tmux_binary="tmux")
    result = adapter.run(*verb_args)

    assert captured_subprocess == [], "no bytes may reach a pane during quiet hours"
    assert recorded_suppressions and recorded_suppressions[-1]["reason"] == "quiet_hours"
    assert recorded_suppressions[-1]["suppressed"] is True
    assert result == ""  # silent no-op, never raises


# (b) same during typing-guard-active.
def test_run_delays_send_keys_during_typing_guard_then_sends(
    monkeypatch, captured_subprocess, recorded_suppressions
):
    _force_quiet(monkeypatch, False)
    calls = {"typing": 0}

    def _typing_once(**_kw):
        calls["typing"] += 1
        return calls["typing"] == 1

    monkeypatch.setattr(send_gate, "typing_guard_active", _typing_once)
    monkeypatch.setattr(send_gate.time, "sleep", lambda _seconds: None)
    _no_override(monkeypatch)

    adapter = TmuxAdapter(tmux_binary="tmux")
    adapter.run("send-keys", "-t", "%9", "C-m")

    assert len(captured_subprocess) == 1, "typing guard should delay, not drop, by default"
    assert recorded_suppressions and recorded_suppressions[-1]["reason"] == "typing_guard"
    assert recorded_suppressions[-1]["policy"] == "delay"


def test_run_can_cancel_send_keys_during_typing_guard_by_policy(
    monkeypatch, captured_subprocess, recorded_suppressions
):
    _force_quiet(monkeypatch, False)
    _force_typing(monkeypatch, True)
    _no_override(monkeypatch)
    monkeypatch.setenv("TMUX_SEND_GATE_POLICY", "cancel")

    adapter = TmuxAdapter(tmux_binary="tmux")
    adapter.run("send-keys", "-t", "%9", "C-m")

    assert captured_subprocess == []
    assert recorded_suppressions and recorded_suppressions[-1]["policy"] == "cancel"


def test_run_does_not_gate_read_commands_during_quiet_hours(
    monkeypatch, captured_subprocess, recorded_suppressions
):
    _force_quiet(monkeypatch, True)
    _force_typing(monkeypatch, True)
    _no_override(monkeypatch)

    adapter = TmuxAdapter(tmux_binary="tmux")
    adapter.run("capture-pane", "-t", "%9", "-p")

    assert len(captured_subprocess) == 1, "reads must pass through even during quiet hours"
    assert recorded_suppressions == []


def test_run_allows_sanctioned_override_but_logs(
    monkeypatch, captured_subprocess, recorded_suppressions
):
    _force_quiet(monkeypatch, True)
    _force_typing(monkeypatch, False)
    monkeypatch.setattr(send_gate, "sanctioned_override", lambda: "tmux-dictate")

    adapter = TmuxAdapter(tmux_binary="tmux")
    adapter.run("send-keys", "-t", "%9", "-l", "dictated text")

    assert len(captured_subprocess) == 1, "a sanctioned human send is allowed through"
    assert recorded_suppressions and recorded_suppressions[-1]["override"] == "tmux-dictate"
    assert recorded_suppressions[-1]["policy"] == "pierce"


def test_run_sends_normally_when_gate_open(monkeypatch, captured_subprocess, recorded_suppressions):
    _force_quiet(monkeypatch, False)
    _force_typing(monkeypatch, False)
    _no_override(monkeypatch)

    adapter = TmuxAdapter(tmux_binary="tmux")
    adapter.run("send-keys", "-t", "%9", "hello")

    assert len(captured_subprocess) == 1
    assert recorded_suppressions == []


def test_evaluate_returns_structured_result(monkeypatch):
    _force_quiet(monkeypatch, True)
    _force_typing(monkeypatch, False)
    _no_override(monkeypatch)

    result = send_gate.evaluate(("send-keys", "-t", "%9", "hi"))
    assert result is not None
    assert result["reason"] == "quiet_hours"
    assert result["verb"] == "send-keys"
    assert result["target"] == "%9"
    assert result["suppressed"] is True
    assert result["policy"] == "cancel"


def test_evaluate_defaults_typing_guard_to_delay(monkeypatch):
    _force_quiet(monkeypatch, False)
    _force_typing(monkeypatch, True)
    _no_override(monkeypatch)

    result = send_gate.evaluate(("send-keys", "-t", "%9", "hi"))
    assert result is not None
    assert result["reason"] == "typing_guard"
    assert result["policy"] == "delay"
    assert result["suppressed"] is True


def test_typing_guard_is_scoped_to_target_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)

    def _fake_run(cmd, *args, **kwargs):
        proc = _FakeCompleted()
        if "display-message" in cmd and "#{client_activity}" in cmd and "-t" not in cmd:
            proc.stdout = f"{now}\n"
            return proc
        if "display-message" in cmd and "-t" in cmd and "%active" in cmd:
            proc.stdout = "11\n"
            return proc
        if "display-message" in cmd and "-t" in cmd and "%other" in cmd:
            proc.stdout = "00\n"
            return proc
        if "list-clients" in cmd and "%active" in cmd:
            proc.stdout = "x\n"
            return proc
        if "capture-pane" in cmd and "-t" in cmd:
            proc.stdout = "> \n"
            return proc
        proc.returncode = 1
        return proc

    monkeypatch.setattr(send_gate.subprocess, "run", _fake_run)

    assert send_gate.typing_guard_active(target="%active") is True
    assert send_gate.typing_guard_active(target="%other") is False


def test_evaluate_only_blocks_target_under_typing_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    monkeypatch.setattr(
        send_gate,
        "typing_guard_active",
        lambda **kw: kw.get("target") == "%guarded",
    )

    blocked = send_gate.evaluate(("send-keys", "-t", "%guarded", "hi"))
    allowed = send_gate.evaluate(("send-keys", "-t", "%clear", "hi"))

    assert blocked is not None and blocked["reason"] == "typing_guard"
    assert allowed is None


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Deterministic time for the delay path: sleep() advances the clock."""
    clock = {"now": 1_000.0, "sleeps": []}

    def _sleep(seconds: float) -> None:
        clock["sleeps"].append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(send_gate.time, "time", lambda: clock["now"])
    monkeypatch.setattr(send_gate.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(send_gate.time, "sleep", _sleep)
    monkeypatch.delenv("TMUX_TYPING_GUARD_WINDOW", raising=False)
    monkeypatch.delenv("TMUX_SEND_GATE_DELAY_TIMEOUT", raising=False)
    return clock


@pytest.fixture
def counted_typing_delay(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Force evaluate() to diagnose a typing-guard delay, counting every call."""
    calls: list[tuple] = []

    def _evaluate(args, **kwargs) -> dict | None:
        calls.append(tuple(args))
        activity = send_gate._client_activity_epoch()
        if activity is None:
            return None
        if 0 <= send_gate.time.time() - activity <= send_gate._typing_guard_window_seconds():
            return {"suppressed": True, "policy": "delay", "reason": "typing_guard"}
        return None

    monkeypatch.setattr(send_gate, "evaluate", _evaluate)
    return calls


# The de-poll regression: a clean clear used to take ~40 evaluate() round-trips
# (0.25s poll, 2 sqlite opens each); now it is one evaluation plus one
# deadline-sleep to the typing window's expiry.
def test_wait_for_gate_clear_sleeps_to_deadline_not_polls(
    monkeypatch: pytest.MonkeyPatch, fake_clock: dict, counted_typing_delay: list[tuple]
) -> None:
    monkeypatch.setattr(send_gate, "_client_activity_epoch", lambda: 998)

    assert send_gate.wait_for_gate_clear(("send-keys", "-t", "%9", "hi")) is True

    assert len(counted_typing_delay) <= 2
    assert len(fake_clock["sleeps"]) <= 2, "one wake per typing burst, not 4/second"
    # last keystroke at 998, window 10s, margin 0.1 → one sleep of ~8.1s
    assert abs(sum(fake_clock["sleeps"]) - 8.1) < 0.01


def test_wait_for_gate_clear_extends_when_typing_resumes(
    monkeypatch: pytest.MonkeyPatch, fake_clock: dict, counted_typing_delay: list[tuple]
) -> None:
    # Keystroke at 998; human types again at 1005 (visible after the first wake).
    monkeypatch.setattr(
        send_gate,
        "_client_activity_epoch",
        lambda: 998 if fake_clock["now"] < 1_005 else 1_005,
    )

    assert send_gate.wait_for_gate_clear(("send-keys", "-t", "%9", "hi")) is True

    sleeps = fake_clock["sleeps"]
    assert 2 <= len(sleeps) <= 3, "a resumed burst earns exactly one more wake"
    assert abs(sleeps[0] - 8.1) < 0.01
    assert abs(sum(sleeps) - 15.1) < 0.01  # ends at 1015.1 = 1005 + 10 + 0.1


def test_wait_for_gate_clear_honors_delay_timeout(
    monkeypatch: pytest.MonkeyPatch, fake_clock: dict, counted_typing_delay: list[tuple]
) -> None:
    monkeypatch.setattr(send_gate, "_client_activity_epoch", lambda: fake_clock["now"] - 1)
    monkeypatch.setenv("TMUX_SEND_GATE_DELAY_TIMEOUT", "5")

    assert send_gate.wait_for_gate_clear(("send-keys", "-t", "%9", "hi")) is False

    assert abs(sum(fake_clock["sleeps"]) - 5.0) < 0.01, "timeout caps the deadline sleep"
