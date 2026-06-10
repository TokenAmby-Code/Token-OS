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
