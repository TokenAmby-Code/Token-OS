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


def test_send_text_then_submit_keeps_enter_in_same_transaction(
    monkeypatch, captured_subprocess
) -> None:
    """Once prompt text starts landing, submit keys cannot be gated separately.

    Regression class: a guard transition after the literal payload could hold
    or cancel the terminating Enter while leaving the text in the composer.  The
    prompt transaction now preflights once, then pierces per-command rechecks
    until text + Enter + recovery Enter have all been issued.
    """
    _force_quiet(monkeypatch, False)
    monkeypatch.setattr(
        send_gate,
        "send_gate_policy",
        lambda *, override=None, reason=None: "pierce" if override else "cancel",
    )
    monkeypatch.setattr(tmux_adapter.time, "sleep", lambda _: None)

    gate_active = {"value": False}

    def _typing(*, target=None, **_kw):
        return gate_active["value"]

    monkeypatch.setattr(send_gate, "typing_guard_active", _typing)

    def _fake_run(cmd, *args, **kwargs):
        captured_subprocess.append(cmd)
        if cmd[1:5] == ["send-keys", "-t", "%9", "-l"]:
            gate_active["value"] = True
        return _FakeCompleted()

    monkeypatch.setattr(tmux_adapter.subprocess, "run", _fake_run)

    adapter = TmuxAdapter(tmux_binary="tmux")
    adapter.send_text_then_submit("%9", "brief for FG")

    sends = [c for c in captured_subprocess if c[1] == "send-keys"]
    assert sends == [
        ["tmux", "send-keys", "-t", "%9", "-l", "brief for FG"],
        ["tmux", "send-keys", "-t", "%9", "C-m"],
        ["tmux", "send-keys", "-t", "%9", "C-m"],
    ]


def test_send_text_then_submit_delays_submit_once_if_human_lock_appears_after_literal(
    monkeypatch: pytest.MonkeyPatch, captured_subprocess: list[list[str]]
) -> None:
    """A human ON/PENDING lock nullifies submit-transaction pierce.

    The text+submit unit may pierce the daemon's own AGENT hold, but if a real
    human lock appears after the literal text lands, the next submit key must
    wait for that lock to clear and then continue once, in order, without
    dropping or duplicating the held key.
    """
    _force_quiet(monkeypatch, False)
    monkeypatch.delenv("TMUX_SEND_GATE_ALLOW", raising=False)
    monkeypatch.setattr(tmux_adapter.time, "sleep", lambda _: None)

    gate_active = {"value": False}
    waits: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        send_gate, "typing_guard_active", lambda *, target=None: gate_active["value"]
    )
    monkeypatch.setattr(send_gate, "_pane_human_locked", lambda target: gate_active["value"])

    def _wait(args, **_kw):
        waits.append(tuple(args))
        gate_active["value"] = False
        return True

    monkeypatch.setattr(send_gate, "wait_for_gate_clear", _wait)

    def _fake_run(cmd, *args, **kwargs):
        captured_subprocess.append(cmd)
        if cmd[1:5] == ["send-keys", "-t", "%9", "-l"]:
            gate_active["value"] = True
        return _FakeCompleted()

    monkeypatch.setattr(tmux_adapter.subprocess, "run", _fake_run)

    adapter = TmuxAdapter(tmux_binary="tmux")
    adapter.send_text_then_submit("%9", "brief for FG")

    assert waits == [("send-keys", "-t", "%9", "C-m")]
    sends = [c for c in captured_subprocess if c[1] == "send-keys"]
    assert sends == [
        ["tmux", "send-keys", "-t", "%9", "-l", "brief for FG"],
        ["tmux", "send-keys", "-t", "%9", "C-m"],
        ["tmux", "send-keys", "-t", "%9", "C-m"],
    ]
