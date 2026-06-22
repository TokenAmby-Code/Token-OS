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
        if "list-clients" in cmd and "%active" in cmd and "#{client_activity}" in cmd:
            proc.stdout = f"{now}\n"
            return proc
        if "list-clients" in cmd and "%active" in cmd and "#{client_activity}" in cmd:
            proc.stdout = f"{now}\n"
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


def test_evaluate_does_not_gate_other_pane_while_typing_in_active_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end per-pane proof (the mandate scenario, real predicate).

    The Emperor is typing in the active+attended pane ``%active`` (fresh
    keystroke, an unsent draft on its prompt line); ``%other`` is an unattended
    worker pane at an empty prompt. A dispatch send to ``%other`` MUST sail
    through while a send to ``%active`` is held — typing in one pane never
    blocks an unrelated pane. No monkeypatch of the predicate: evaluate() runs
    the real ``typing_guard_active`` over a faked tmux.
    """
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
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
        if "list-clients" in cmd and "%active" in cmd and "#{client_activity}" in cmd:
            proc.stdout = f"{now}\n"
            return proc
        if "list-clients" in cmd and "%active" in cmd:
            proc.stdout = "x\n"
            return proc
        if "capture-pane" in cmd and "%active" in cmd:
            proc.stdout = "> draft\n"  # Emperor's unsent draft
            return proc
        if "capture-pane" in cmd and "%other" in cmd:
            proc.stdout = "> \n"  # worker pane, empty prompt
            return proc
        proc.returncode = 1
        return proc

    monkeypatch.setattr(send_gate.subprocess, "run", _fake_run)

    held = send_gate.evaluate(("send-keys", "-t", "%active", "x"))
    dispatched = send_gate.evaluate(("send-keys", "-t", "%other", "launch"))

    assert held is not None and held["reason"] == "typing_guard" and held["suppressed"] is True
    assert dispatched is None, "a send to an unrelated pane must not be gated by typing in %active"


def test_pending_input_skips_tui_chrome_below_the_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#284 regression pin: a paused Claude/Codex draft sits ABOVE the TUI chrome.

    The post-#284 detector took the literal last non-empty line — the
    ``⏵⏵ bypass permissions`` hint / context footer — so a held draft read as
    *clear* once the client-activity window expired and enforcement leaked into
    the Emperor's half-typed prompt. The detector must skip the chrome and
    evaluate the real prompt line above it.
    """
    drafting = (
        "> tell me about the thing I am drafting\n"
        "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents\n"
    )
    cleared = (
        "❯ /clear\n"
        "❯ \n"
        "─" * 60 + "\n"
        "  ... 0/200k $0.00\n"
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
    )

    def _capture(text: str):
        def _fake_run(cmd, *args, **kwargs):
            proc = _FakeCompleted()
            if "capture-pane" in cmd:
                proc.stdout = text
                return proc
            proc.returncode = 1
            return proc

        return _fake_run

    monkeypatch.setattr(send_gate.subprocess, "run", _capture(drafting))
    assert send_gate._pane_has_pending_input("%active") is True

    monkeypatch.setattr(send_gate.subprocess, "run", _capture(cleared))
    assert send_gate._pane_has_pending_input("%active") is False


def test_unattended_worker_pane_with_prompt_text_is_deliverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asymmetry fix (mandate 3) + Custodes brief-delivery guardrail.

    A worker pane that no human is attending must NOT be typing-guarded merely
    because it has leftover prompt text — the guard is scoped to the pane the
    human is actually typing in. So a brief/dispatch send to that unattended
    worker still SAILS THROUGH (evaluate → None). This is the over-block the old
    predicate caused: ``_pane_has_pending_input`` fired regardless of attendance,
    holding W's brief-delivery to idle worker panes.
    """
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)

    def _fake_run(cmd, *args, **kwargs):
        proc = _FakeCompleted()
        if "display-message" in cmd and "#{client_activity}" in cmd and "-t" not in cmd:
            proc.stdout = f"{now - 3600}\n"  # last keystroke an hour ago, nowhere near
            return proc
        if "display-message" in cmd and "-t" in cmd and "%worker" in cmd:
            proc.stdout = "00\n"  # not the active pane, not the active window
            return proc
        if "list-clients" in cmd and "%worker" in cmd:
            return proc  # no client attached to the worker pane
        if "capture-pane" in cmd and "%worker" in cmd:
            proc.stdout = "❯ leftover prompt text\n"  # has prompt text, but no human here
            return proc
        proc.returncode = 1
        return proc

    monkeypatch.setattr(send_gate.subprocess, "run", _fake_run)

    assert send_gate.typing_guard_active(target="%worker") is False
    assert send_gate.evaluate(("send-keys", "-t", "%worker", "brief body")) is None


def test_attended_pane_is_held_on_pending_text_and_on_recent_keystroke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Human-protection pin: the attended pane the Emperor is in stays guarded
    both when its prompt shows an unsent draft AND when he just struck a key with
    no visible draft yet (mid-keystroke injection must be held)."""
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)

    state = {"capture": "❯ draft\n", "activity": now - 3600}  # draft, stale keystroke

    def _fake_run(cmd, *args, **kwargs):
        proc = _FakeCompleted()
        if "display-message" in cmd and "#{client_activity}" in cmd and "-t" not in cmd:
            proc.stdout = f"{state['activity']}\n"
            return proc
        if "display-message" in cmd and "-t" in cmd and "%active" in cmd:
            proc.stdout = "11\n"
            return proc
        if "list-clients" in cmd and "%active" in cmd and "#{client_activity}" in cmd:
            proc.stdout = f"{state['activity']}\n"
            return proc
        if "list-clients" in cmd and "%active" in cmd:
            proc.stdout = "x\n"
            return proc
        if "capture-pane" in cmd and "%active" in cmd:
            proc.stdout = state["capture"]
            return proc
        proc.returncode = 1
        return proc

    monkeypatch.setattr(send_gate.subprocess, "run", _fake_run)

    # Draft on the prompt, no recent keystroke → held.
    assert send_gate.typing_guard_active(target="%active") is True

    # No visible draft, but a keystroke just landed → still held (mid-keystroke).
    state["capture"] = "❯ \n"
    state["activity"] = now
    assert send_gate.typing_guard_active(target="%active") is True


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
    monkeypatch.setattr(send_gate, "_pane_has_pending_input", lambda target: False)
    monkeypatch.setattr(send_gate, "_target_client_activity_epochs", lambda target: [998])
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
    monkeypatch.setattr(send_gate, "_pane_has_pending_input", lambda target: False)
    monkeypatch.setattr(
        send_gate,
        "_target_client_activity_epochs",
        lambda target: [998 if fake_clock["now"] < 1_005 else 1_005],
    )
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
    monkeypatch.setattr(send_gate, "_pane_has_pending_input", lambda target: False)
    monkeypatch.setattr(
        send_gate, "_target_client_activity_epochs", lambda target: [fake_clock["now"] - 1]
    )
    monkeypatch.setattr(send_gate, "_client_activity_epoch", lambda: fake_clock["now"] - 1)
    monkeypatch.setenv("TMUX_SEND_GATE_DELAY_TIMEOUT", "5")

    assert send_gate.wait_for_gate_clear(("send-keys", "-t", "%9", "hi")) is False

    assert abs(sum(fake_clock["sleeps"]) - 5.0) < 0.01, "timeout caps the deadline sleep"


def test_target_typing_guard_ignores_unrelated_global_activity(monkeypatch):
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    now = 10_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    monkeypatch.setattr(send_gate, "_client_activity_epoch", lambda: now)
    monkeypatch.setattr(send_gate, "_target_client_activity_epochs", lambda target: [now - 60])
    monkeypatch.setattr(send_gate, "_pane_has_pending_input", lambda target: False)
    monkeypatch.setattr(send_gate, "_pane_attended", lambda target: True)

    assert send_gate.evaluate(("send-keys", "-t", "%9", "hi")) is None


def test_target_typing_guard_uses_attending_client_activity(monkeypatch):
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    now = 10_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    monkeypatch.setattr(send_gate, "_target_client_activity_epochs", lambda target: [now - 2])
    monkeypatch.setattr(send_gate, "_pane_has_pending_input", lambda target: False)
    monkeypatch.setattr(send_gate, "_pane_attended", lambda target: True)

    result = send_gate.evaluate(("send-keys", "-t", "%9", "hi"))

    assert result is not None
    assert result["reason"] == "typing_guard"
    assert result["policy"] == "delay"


def test_target_typing_guard_pending_prompt_on_attended_target(monkeypatch):
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    monkeypatch.setattr(send_gate.time, "time", lambda: 10_000)
    monkeypatch.setattr(send_gate, "_target_client_activity_epochs", lambda target: [9_000])
    monkeypatch.setattr(send_gate, "_pane_has_pending_input", lambda target: True)
    monkeypatch.setattr(send_gate, "_pane_attended", lambda target: True)

    result = send_gate.evaluate(("send-keys", "-t", "%9", "hi"))

    assert result is not None
    assert result["reason"] == "typing_guard"


def test_target_typing_guard_unattended_pending_prompt_is_deliverable(monkeypatch):
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    monkeypatch.setattr(send_gate, "_target_client_activity_epochs", lambda target: [])
    monkeypatch.setattr(send_gate, "_pane_has_pending_input", lambda target: True)
    monkeypatch.setattr(send_gate, "_pane_attended", lambda target: False)

    assert send_gate.evaluate(("send-keys", "-t", "%9", "hi")) is None


def test_send_text_then_submit_gated_attended_pane_writes_no_bytes(
    monkeypatch, captured_subprocess, recorded_suppressions
):
    """Typing guard suppression is atomic for direct pane writes.

    This pins the lower-boundary half of attended-pane queue safety: when a
    target pane has pending human input and the delay cannot clear, tmuxctl
    raises a structured gate result and issues no tmux write. The API layer is
    responsible for queueing that payload for a later drain.
    """
    _force_quiet(monkeypatch, False)
    _force_typing(monkeypatch, True)
    _no_override(monkeypatch)
    monkeypatch.setattr(send_gate, "wait_for_gate_clear", lambda *_args, **_kwargs: False)

    adapter = TmuxAdapter(tmux_binary="tmux")

    with pytest.raises(tmux_adapter.TmuxSendGated) as excinfo:
        adapter.send_text_then_submit("%attended", "do not clobber", clear_prompt=True)

    assert excinfo.value.gate["reason"] == "typing_guard"
    assert excinfo.value.gate["suppressed"] is True
    assert captured_subprocess == [], "gated send_text_then_submit must not touch tmux"
    assert recorded_suppressions and recorded_suppressions[-1]["reason"] == "typing_guard"


# ── send-gate-attended-scoping-clobber: canonical-id resolution miss ──────────
# The clobber's root cause: the gate's attendance/typing checks shell out to
# `tmux display-message -t <target>` / `tmux list-clients -t <target>`. tmux only
# understands physical %pane ids (and native session:window addresses). An
# Imperium canonical id (mechanicus:fabricator-general, legion:custodes, 1:N…)
# silently mis-resolves, so _pane_attended returns False and the guard MISSES an
# actively-typed attended pane — then the send clobbers the human's live draft.


def _attended_physical_only_tmux(physical: str):
    """Fake real-tmux where ONLY ``physical`` is an attended pane with a draft.

    Mirrors live tmux: a canonical id (anything != ``physical``) cannot be
    resolved at the tmux boundary, so every query for it errors (rc=1) exactly
    as `tmux display-message -t mechanicus:fabricator-general` would.
    """

    def _fake_run(cmd, *args, **kwargs):
        proc = _FakeCompleted()
        target = None
        for idx, tok in enumerate(cmd):
            if tok == "-t" and idx + 1 < len(cmd):
                target = cmd[idx + 1]
                break
        verb = cmd[1] if len(cmd) > 1 else ""
        if target is not None and target != physical:
            proc.returncode = 1  # tmux cannot resolve a canonical id
            return proc
        if verb == "display-message":
            proc.stdout = "11\n"  # pane_active & window_active
        elif verb == "list-clients":
            proc.stdout = (
                f"{int(send_gate.time.time())}\n" if "#{client_activity}" in cmd else "x\n"
            )
        elif verb == "capture-pane":
            proc.stdout = "> half-typed draft\n"  # pending human input
        return proc

    return _fake_run


def test_typing_guard_detects_physical_but_misses_canonical_target(monkeypatch) -> None:
    """Debug-step pin: physical id is detected, canonical id is MISSED.

    This is the exact divergence behind the clobber. The fix lives upstream
    (resolve canonical -> physical before the gate, see the adapter test below);
    this test pins the boundary behaviour so a future refactor can't silently
    re-introduce a canonical id reaching the raw tmux attendance probes.
    """
    monkeypatch.setattr(send_gate, "_real_tmux_binary", lambda: "tmux")
    monkeypatch.setattr(send_gate.subprocess, "run", _attended_physical_only_tmux("%44"))

    assert send_gate.typing_guard_active(target="%44") is True
    assert send_gate.typing_guard_active(target="mechanicus:fabricator-general") is False


def test_run_resolves_canonical_target_to_physical_before_gating(
    monkeypatch, captured_subprocess, recorded_suppressions
) -> None:
    """The fix: an attended-pane send addressed canonically is HELD, not clobbered.

    `tmuxctl send-text --pane mechanicus:fabricator-general` (and every
    TmuxAdapter.run caller) must evaluate the gate against the RESOLVED physical
    id. With the human typing in %44, the gate must see target=%44, engage the
    typing guard, and issue zero bytes — the draft survives.
    """
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    monkeypatch.setenv("TMUX_SEND_GATE_POLICY", "cancel")  # avoid the delay/retry loop

    seen_targets: list[str | None] = []

    def _typing(*, target=None, **_kw):
        seen_targets.append(target)
        return target == "%44"  # the human is typing in the physical pane

    monkeypatch.setattr(send_gate, "typing_guard_active", _typing)

    adapter = TmuxAdapter(tmux_binary="tmux")
    # Stand in for live canonical->physical resolution (no live tmux in tests).
    monkeypatch.setattr(
        adapter,
        "_resolve_pane_target_arg",
        lambda t: "%44" if t == "mechanicus:fabricator-general" else t,
    )

    result = adapter.run("send-keys", "-t", "mechanicus:fabricator-general", "-l", "brief body")

    assert "%44" in seen_targets, "gate must evaluate the RESOLVED physical id"
    assert "mechanicus:fabricator-general" not in seen_targets, (
        "the unresolved canonical id must never reach the typing-guard predicate"
    )
    assert captured_subprocess == [], "attended-pane send must write zero bytes, never clobber"
    assert recorded_suppressions and recorded_suppressions[-1]["reason"] == "typing_guard"
    assert result == ""
