"""Send-gate propagation through send_text_then_submit (the incident fix).

The brief delivery failure (2026-05-30): a gated send returned silently and the
caller reported it as ``sent``. These tests pin the corrected contract:

  * When the universal gate suppresses the byte-bearing literal send,
    ``send_text_then_submit`` raises ``TmuxSendGated`` (a distinct exception
    carrying the gate result) and issues NO subsequent submit — zero bytes
    reach the pane, so the caller may safely re-queue.
  * When the gate is open, the full text+submit sequence proceeds unchanged and
    no gate result is left dangling.
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
    calls: list[list[str]] = []

    def _fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return _FakeCompleted()

    monkeypatch.setattr(tmux_adapter.subprocess, "run", _fake_run)
    return calls


@pytest.fixture(autouse=True)
def _silence_suppression_log(monkeypatch):
    monkeypatch.setattr(send_gate, "record_suppression", lambda result, **kw: None)


def _force_quiet(monkeypatch, active: bool):
    monkeypatch.setattr(send_gate, "quiet_hours_active", lambda **kw: (active, {"forced": active}))


def _force_typing(monkeypatch, active: bool):
    monkeypatch.setattr(send_gate, "typing_guard_active", lambda **kw: active)


def _no_override(monkeypatch):
    monkeypatch.setattr(send_gate, "sanctioned_override", lambda: None)


def test_send_text_then_submit_raises_when_gate_suppresses(monkeypatch, captured_subprocess):
    _force_quiet(monkeypatch, False)
    _force_typing(monkeypatch, True)
    _no_override(monkeypatch)
    monkeypatch.setenv("TMUX_SEND_GATE_POLICY", "cancel")
    monkeypatch.setattr(tmux_adapter.time, "sleep", lambda _: None)

    adapter = TmuxAdapter(tmux_binary="tmux")

    with pytest.raises(tmux_adapter.TmuxSendGated) as excinfo:
        adapter.send_text_then_submit("%9", "brief for FG")

    # The gate result is carried on the exception for the caller to inspect.
    assert excinfo.value.gate["reason"] == "typing_guard"
    assert excinfo.value.gate["suppressed"] is True
    # No bytes reached the pane and crucially no bare C-m was fired at an empty
    # prompt — the entire submit aborted atomically.
    assert captured_subprocess == []
    assert adapter.last_send_gate_result is not None
    assert adapter.last_send_gate_result["suppressed"] is True


def test_send_text_then_submit_aborts_before_submit_during_quiet_hours(
    monkeypatch, captured_subprocess
):
    _force_quiet(monkeypatch, True)
    _force_typing(monkeypatch, False)
    _no_override(monkeypatch)
    monkeypatch.setattr(tmux_adapter.time, "sleep", lambda _: None)

    adapter = TmuxAdapter(tmux_binary="tmux")

    with pytest.raises(tmux_adapter.TmuxSendGated) as excinfo:
        adapter.send_text_then_submit("%9", "brief for FG", clear_prompt=True)

    assert excinfo.value.gate["reason"] == "quiet_hours"
    assert captured_subprocess == []


def test_send_text_then_submit_proceeds_when_gate_open(monkeypatch, captured_subprocess):
    _force_quiet(monkeypatch, False)
    _force_typing(monkeypatch, False)
    _no_override(monkeypatch)
    monkeypatch.setattr(tmux_adapter.time, "sleep", lambda _: None)

    adapter = TmuxAdapter(tmux_binary="tmux")
    adapter.send_text_then_submit("%9", "brief for FG")

    sends = [c for c in captured_subprocess if "send-keys" in c]
    # literal payload + two C-m submits all issued.
    assert len(sends) == 3
    assert adapter.last_send_gate_result is None
