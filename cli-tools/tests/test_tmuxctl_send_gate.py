"""Regression tests for the universal tmux send gate (the pane-write sentinel).

Invariant under test: no bytes reach any pane via TmuxAdapter.run() while
quiet hours OR the typing guard is active, regardless of which subsystem
originates the send. Reads are never gated; sanctioned human sends are allowed
but logged.
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
def test_run_suppresses_send_keys_during_typing_guard(
    monkeypatch, captured_subprocess, recorded_suppressions
):
    _force_quiet(monkeypatch, False)
    _force_typing(monkeypatch, True)
    _no_override(monkeypatch)

    adapter = TmuxAdapter(tmux_binary="tmux")
    adapter.run("send-keys", "-t", "%9", "C-m")

    assert captured_subprocess == [], "no bytes may reach a pane while the human is typing"
    assert recorded_suppressions and recorded_suppressions[-1]["reason"] == "typing_guard"


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
