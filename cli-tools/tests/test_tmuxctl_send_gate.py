"""Regression tests for the universal tmux send gate (the pane-write sentinel).

Invariant under test: quiet hours cancel automated pane writes by default;
the typing guard delays automated writes by default; sanctioned direct-input
sends pierce but are audited. Reads are never gated.

The typing guard is the daemon-owned per-pane JSON state. The gate reads
@TYPING_GUARD_JSON through the same status helper as the daemon endpoint, so
these tests fake that one canonical option instead of timer compatibility state.
"""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import pytest
import tmuxctl.send_gate as send_gate
import tmuxctl.tmux_adapter as tmux_adapter
import tmuxctl.typing_guard_state as typing_guard_state
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


def _lock_tmux(
    monkeypatch,
    locks: dict[str, int | None],
    *,
    pending: dict[str, int | None] | None = None,
    agent: dict[str, int | None] | None = None,
    owners: dict[str, str | None] | None = None,
    panes: list[str] | None = None,
):
    """Fake real tmux so the JSON guard reader sees pane-scoped records."""
    monkeypatch.setattr(send_gate, "_real_tmux_binary", lambda: "tmux")

    def _record_for(target: str | None) -> str:
        until = locks.get(target)
        kind = "human" if until is not None else "off"
        owner = None
        pending_until = (pending or {}).get(target)
        if pending_until is not None:
            kind, until = "pending", pending_until
        agent_until = (agent or {}).get(target)
        if agent_until is not None:
            kind, until = "agent", agent_until
            owner = (owners or {}).get(target)
        return json.dumps({"kind": kind, "until": until, "owner": owner, "source": "tmuxctld"})

    def _fake_run(cmd, *args, **kwargs):
        proc = _FakeCompleted()
        if "show-options" in cmd and typing_guard_state.GUARD_JSON_OPTION in cmd:
            target = None
            for idx, tok in enumerate(cmd):
                if tok == "-t" and idx + 1 < len(cmd):
                    target = cmd[idx + 1]
                    break
            proc.stdout = _record_for(target)
            return proc
        if "list-panes" in cmd:
            ids = panes if panes is not None else list(locks.keys())
            proc.stdout = "".join(f"{p}\n" for p in ids)
            return proc
        proc.returncode = 1
        return proc

    monkeypatch.setattr(send_gate.subprocess, "run", _fake_run)


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
    """The JSON guard is per-pane and never leaks onto another pane."""
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    _lock_tmux(monkeypatch, {"%active": now + 200, "%other": now - 5})

    assert send_gate.typing_guard_active(target="%active") is True
    assert send_gate.typing_guard_active(target="%other") is False


def test_any_typing_guard_active_scans_live_panes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The targetless aggregate query (global policies that hang on ANY guard)
    is true iff some live pane carries an unexpired lock."""
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)

    _lock_tmux(monkeypatch, {"%a": now - 5, "%b": now + 120})
    assert send_gate.any_typing_guard_active() is True

    _lock_tmux(monkeypatch, {"%a": now - 5, "%b": now - 1})
    assert send_gate.any_typing_guard_active() is False


def test_evaluate_does_not_gate_other_pane_while_typing_in_active_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end per-pane proof (the mandate scenario, real predicate).

    The Emperor typed into ``%active`` ~1 min ago, so its JSON human guard is
    still live. ``%other`` has no guard. A dispatch send to ``%other`` MUST sail
    through while a send to ``%active`` is held.
    """
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    _lock_tmux(monkeypatch, {"%active": now + 240, "%other": None})

    held = send_gate.evaluate(("send-keys", "-t", "%active", "x"))
    dispatched = send_gate.evaluate(("send-keys", "-t", "%other", "launch"))

    assert held is not None and held["reason"] == "typing_guard" and held["suppressed"] is True
    assert dispatched is None, "a send to an unrelated pane must not be gated by typing in %active"


def test_unattended_pane_without_lock_is_deliverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No screen-scraping: daemon JSON state is the sole signal."""
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    _lock_tmux(monkeypatch, {"%worker": None})

    assert send_gate.typing_guard_active(target="%worker") is False
    assert send_gate.evaluate(("send-keys", "-t", "%worker", "brief body")) is None


def test_locked_pane_is_held_until_expiry_regardless_of_focus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A guarded pane stays guarded purely on the absolute JSON expiry."""
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    locks: dict[str, int | None] = {"%active": now + 1}
    _lock_tmux(monkeypatch, locks)

    # Lock still future → held (no focus signal is consulted at all).
    assert send_gate.typing_guard_active(target="%active") is True

    # Same pane, expiry now in the past → released. Nothing else changed.
    locks["%active"] = now - 1
    assert send_gate.typing_guard_active(target="%active") is False


def test_pending_pane_remains_send_blocking_after_enter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enter moves ON -> PENDING; the lock is gone, but sends still hold."""
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    _lock_tmux(monkeypatch, {"%active": None}, pending={"%active": now + 5})

    assert send_gate.typing_guard_active(target="%active") is True
    held = send_gate.evaluate(("send-keys", "-t", "%active", "queued"))
    assert held is not None and held["reason"] == "typing_guard"


def test_agent_hold_alone_reports_typing_guard_active_and_delays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pane carrying only an agent JSON guard delays a concurrent send."""
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    _lock_tmux(monkeypatch, {"%held": None}, agent={"%held": now + 8})

    assert send_gate.typing_guard_active(target="%held") is True
    held = send_gate.evaluate(("send-keys", "-t", "%held", "concurrent"))
    assert held is not None and held["reason"] == "typing_guard"
    assert held["policy"] == "delay"


def test_expired_agent_hold_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    _lock_tmux(monkeypatch, {"%held": None}, agent={"%held": now - 1})

    assert send_gate.typing_guard_active(target="%held") is False


def test_guard_reader_uses_single_json_option(monkeypatch: pytest.MonkeyPatch) -> None:
    now = 1_700_000_000
    calls: list[list[str]] = []
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    monkeypatch.setattr(send_gate, "_real_tmux_binary", lambda: "tmux")

    def _fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        proc = _FakeCompleted()
        if "show-options" in cmd and typing_guard_state.GUARD_JSON_OPTION in cmd:
            proc.stdout = json.dumps(
                {"kind": "human", "until": now + 200, "owner": None, "source": "tmuxctld"}
            )
            return proc
        raise AssertionError(f"unexpected tmux call: {cmd}")

    monkeypatch.setattr(send_gate.subprocess, "run", _fake_run)

    assert send_gate.typing_guard_active(target="%active") is True
    assert len(calls) == 1


def test_expired_pending_pane_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    _lock_tmux(monkeypatch, {"%active": None}, pending={"%active": now - 1})

    assert send_gate.typing_guard_active(target="%active") is False


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
    monkeypatch.delenv("TMUX_SEND_GATE_DELAY_TIMEOUT", raising=False)
    return clock


# The de-poll property: a delayed send sleeps TOWARD the lock's absolute expiry
# (no 4/second busy-spin), but at a recheck cap so an early Enter-clear releases
# promptly rather than after the whole window.
def test_wait_for_gate_clear_sleeps_toward_lock_expiry(
    monkeypatch: pytest.MonkeyPatch, fake_clock: dict
) -> None:
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    until = fake_clock["now"] + 3
    _lock_tmux(monkeypatch, {"%9": until})

    assert send_gate.wait_for_gate_clear(("send-keys", "-t", "%9", "hi")) is True

    sleeps = fake_clock["sleeps"]
    assert sleeps, "a held send must sleep at least once"
    assert all(s <= send_gate._TYPING_LOCK_RECHECK_SECONDS + 1e-9 for s in sleeps), (
        "each wake is capped at the recheck interval"
    )
    assert fake_clock["now"] >= until, "wakes carry the clock past the lock expiry"
    # ~3s of lock at a 1s recheck cap → a small handful of wakes, not a busy-spin.
    assert len(sleeps) <= 5


def test_wait_for_gate_clear_releases_promptly_when_lock_cleared_early(
    monkeypatch: pytest.MonkeyPatch, fake_clock: dict
) -> None:
    """An Enter clears the JSON guard mid-wait. Even though
    the original guard ran 5 min out, the recheck cap releases the held send
    within ~1 wake of the clear — the gate must not wait out the full window."""
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    cleared_at = fake_clock["now"] + 2  # Emperor presses Enter 2s in

    def _fake_run(cmd, *args, **kwargs):
        proc = _FakeCompleted()
        if "show-options" in cmd and typing_guard_state.GUARD_JSON_OPTION in cmd:
            until = None if fake_clock["now"] >= cleared_at else int(fake_clock["now"] + 300)
            kind = "off" if until is None else "human"
            proc.stdout = json.dumps(
                {"kind": kind, "until": until, "owner": None, "source": "tmuxctld"}
            )
            return proc
        proc.returncode = 1
        return proc

    monkeypatch.setattr(send_gate, "_real_tmux_binary", lambda: "tmux")
    monkeypatch.setattr(send_gate.subprocess, "run", _fake_run)

    assert send_gate.wait_for_gate_clear(("send-keys", "-t", "%9", "hi")) is True

    total = sum(fake_clock["sleeps"])
    assert total <= cleared_at - 1_000 + send_gate._TYPING_LOCK_RECHECK_SECONDS + 1e-9, (
        "released within one recheck of the Enter-clear, not after the 5-min window"
    )


def test_backspace_pending_followup_keystroke_rearms_and_flushes_held_send_once(
    monkeypatch: pytest.MonkeyPatch, fake_clock: dict
) -> None:
    """Exact one-shot wedge: key -> BACKSPACE pending -> key must leave pending.

    A held automated send starts while the pane is in the 15s Backspace PENDING
    hold.  On the first wait wake, a follow-up human keystroke arrives.  That
    keystroke must convert PENDING back to a real ON lock; then the held send
    releases exactly once when that ON lock expires.  The regression was that
    ``arm()`` preserved PENDING, so the send stayed behind the stale Backspace
    pending hold instead of the fresh keystroke lock.
    """
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    pane = "%9"
    state: dict[str, str] = {}

    class FakeTmux:
        def run(self, *args: str, timeout: float = 0.5):  # noqa: ARG002
            proc = _FakeCompleted()
            if args and args[0] == "show-options":
                proc.stdout = state.get(args[-1], "")
                return proc
            if args[:2] == ("set-option", "-p"):
                state[args[-2]] = args[-1]
                return proc
            if args[:2] == ("set-option", "-pu"):
                state.pop(args[-1], None)
                return proc
            return proc

    fake_tmux = FakeTmux()
    typing_guard_state.arm(fake_tmux, pane, seconds=300, now=int(fake_clock["now"]))
    typing_guard_state.pending(fake_tmux, pane, seconds=15, now=int(fake_clock["now"]))
    assert json.loads(state[typing_guard_state.GUARD_JSON_OPTION])["kind"] == "pending"

    monkeypatch.setattr(send_gate, "_real_tmux_binary", lambda: "tmux")

    actual_sends: list[list[str]] = []

    def _fake_subprocess_run(cmd, *args, **kwargs):
        proc = _FakeCompleted()
        if "show-options" in cmd and typing_guard_state.GUARD_JSON_OPTION in cmd:
            proc.stdout = state.get(typing_guard_state.GUARD_JSON_OPTION, "")
            return proc
        if len(cmd) >= 2 and cmd[1:] == ["send-keys", "-t", pane, "-l", "HELD_ONCE"]:
            actual_sends.append(cmd)
            return proc
        proc.returncode = 1
        return proc

    monkeypatch.setattr(send_gate.subprocess, "run", _fake_subprocess_run)
    sleeps: list[float] = []
    keystroke_rearmed = False

    def _sleep(seconds: float) -> None:
        nonlocal keystroke_rearmed
        sleeps.append(seconds)
        fake_clock["now"] += seconds
        if not keystroke_rearmed:
            keystroke_rearmed = True
            typing_guard_state.arm(
                fake_tmux,
                pane,
                seconds=2,
                now=int(fake_clock["now"]),
            )

    monkeypatch.setattr(send_gate.time, "sleep", _sleep)

    adapter = TmuxAdapter(tmux_binary="tmux")
    adapter.run("send-keys", "-t", pane, "-l", "HELD_ONCE")

    final_record = json.loads(state[typing_guard_state.GUARD_JSON_OPTION])
    assert final_record["kind"] == "human"
    assert int(final_record["until"]) <= int(fake_clock["now"])
    assert sum(sleeps) <= 4, "follow-up keystroke re-arm must avoid waiting out stale pending"
    assert len(actual_sends) == 1


def test_wait_for_gate_clear_honors_delay_timeout(
    monkeypatch: pytest.MonkeyPatch, fake_clock: dict
) -> None:
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    # A full 5-min lock, but an explicit 5s delay timeout caps the wait.
    _lock_tmux(monkeypatch, {"%9": fake_clock["now"] + 300})
    monkeypatch.setenv("TMUX_SEND_GATE_DELAY_TIMEOUT", "5")

    assert send_gate.wait_for_gate_clear(("send-keys", "-t", "%9", "hi")) is False

    assert abs(sum(fake_clock["sleeps"]) - 5.0) < 0.5, "timeout caps the total delayed wait"


# ── send-gate-attended-scoping-clobber: canonical-id resolution miss ──────────
# The clobber's root cause: the gate's JSON guard read must target a physical
# %pane id. Canonical ids must be resolved before the tmux boundary.


def test_typing_guard_detects_physical_but_misses_canonical_target(monkeypatch) -> None:
    """Debug-step pin: a physical id is detected, a canonical id is MISSED.

    This is the exact divergence behind the clobber. The fix lives upstream
    (resolve canonical -> physical before the gate); this pins the boundary so a
    future refactor can't silently re-introduce a canonical id reaching the raw
    tmux lock probe (which would always read clear and never guard).
    """
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    monkeypatch.setattr(send_gate, "_real_tmux_binary", lambda: "tmux")

    def _fake_run(cmd, *args, **kwargs):
        proc = _FakeCompleted()
        if "show-options" in cmd and typing_guard_state.GUARD_JSON_OPTION in cmd:
            target = None
            for idx, tok in enumerate(cmd):
                if tok == "-t" and idx + 1 < len(cmd):
                    target = cmd[idx + 1]
                    break
            if target == "%44":
                proc.stdout = json.dumps(
                    {"kind": "human", "until": now + 200, "owner": None, "source": "tmuxctld"}
                )
                return proc
            proc.returncode = 1
            return proc
        proc.returncode = 1
        return proc

    monkeypatch.setattr(send_gate.subprocess, "run", _fake_run)

    assert send_gate.typing_guard_active(target="%44") is True
    assert send_gate.typing_guard_active(target="mechanicus:fabricator-general") is False


def test_send_text_then_submit_gated_attended_pane_writes_no_bytes(
    monkeypatch, captured_subprocess, recorded_suppressions
):
    """Typing guard suppression is atomic for direct pane writes.

    This pins the lower-boundary half of locked-pane queue safety: when a target
    pane carries a live keystroke lock and the delay cannot clear, tmuxctl raises
    a structured gate result and issues no tmux write. The API layer is
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


def test_run_resolves_canonical_target_to_physical_before_gating(
    monkeypatch, captured_subprocess, recorded_suppressions
) -> None:
    """The fix: a locked-pane send addressed canonically is HELD, not clobbered.

    `tmuxctl send-text --pane mechanicus:fabricator-general` (and every
    TmuxAdapter.run caller) must evaluate the gate against the RESOLVED physical
    id. With the human's lock on %44, the gate must see target=%44, engage the
    typing guard, and issue zero bytes — the draft survives.
    """
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    monkeypatch.setenv("TMUX_SEND_GATE_POLICY", "cancel")  # avoid the delay/retry loop

    seen_targets: list[str | None] = []

    def _typing(*, target=None, **_kw):
        seen_targets.append(target)
        return target == "%44"  # the human's lock is on the physical pane

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
    assert captured_subprocess == [], "locked-pane send must write zero bytes, never clobber"
    assert recorded_suppressions and recorded_suppressions[-1]["reason"] == "typing_guard"
    assert result == ""


def test_tmuxctld_holder_override_cannot_pierce_human_lock(monkeypatch) -> None:
    """The daemon may pierce only its own AGENT hold, never a human lock.

    Incident regression: tmuxctld held a pane green, then a human keystroke
    arrived.  The request thread still carried the local
    ``tmuxctld-send-holder`` override, so later submit keys pierced and clobbered
    active typing.  A live ON/PENDING hold must nullify that override.
    """
    _force_quiet(monkeypatch, False)
    monkeypatch.setattr(send_gate, "typing_guard_active", lambda *, target=None: True)
    monkeypatch.setattr(
        send_gate,
        "_pane_guard_status",
        lambda target: {
            "kind": "human",
            "until": 1300,
            "owner": None,
            "active": True,
            "marker": "",
        },
    )
    monkeypatch.setattr(send_gate, "_pane_human_locked", lambda target: target == "%44")

    with send_gate.thread_local_override("tmuxctld-send-holder", owner="req-1"):
        result = send_gate.evaluate(("send-keys", "-t", "%44", "C-m"))

    assert result is not None
    assert result["reason"] == "typing_guard"
    assert result["policy"] == "delay"
    assert result["suppressed"] is True
    assert result["override"] is None
    assert result["ignored_override"] == "tmuxctld-send-holder"


def test_submit_transaction_override_cannot_pierce_human_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The adapter's text+submit override also yields to ON/PENDING.

    The transaction override exists to keep submit keys behind the daemon's own
    AGENT hold in the same text+submit unit.  It must not become a blanket
    pierce after a human keystroke/backspace/Ctrl+C creates a real lock/pending
    hold on the pane.
    """
    _force_quiet(monkeypatch, False)
    monkeypatch.setattr(send_gate, "typing_guard_active", lambda *, target=None: True)
    monkeypatch.setattr(
        send_gate,
        "_pane_guard_status",
        lambda target: {
            "kind": "pending",
            "until": 1300,
            "owner": None,
            "active": True,
            "marker": "",
        },
    )
    monkeypatch.setattr(send_gate, "_pane_human_locked", lambda target: target == "%44")

    with send_gate.thread_local_override("tmuxctl-submit-transaction", owner="req-1"):
        result = send_gate.evaluate(("send-keys", "-t", "%44", "C-m"))

    assert result is not None
    assert result["reason"] == "typing_guard"
    assert result["policy"] == "delay"
    assert result["suppressed"] is True
    assert result["override"] is None
    assert result["ignored_override"] == "tmuxctl-submit-transaction"


def test_tmuxctld_holder_override_can_pierce_agent_only_hold(monkeypatch) -> None:
    _force_quiet(monkeypatch, False)
    monkeypatch.setattr(send_gate, "typing_guard_active", lambda *, target=None: True)
    monkeypatch.setattr(
        send_gate,
        "_pane_guard_status",
        lambda target: {
            "kind": "agent",
            "until": 1300,
            "owner": "req-1",
            "active": True,
            "marker": "",
        },
    )
    monkeypatch.setattr(send_gate, "_pane_human_locked", lambda target: False)

    with send_gate.thread_local_override("tmuxctld-send-holder", owner="req-1"):
        result = send_gate.evaluate(("send-keys", "-t", "%44", "-l", "payload"))

    assert result is not None
    assert result["reason"] == "typing_guard"
    assert result["policy"] == "pierce"
    assert result["suppressed"] is False
    assert result["override"] == "tmuxctld-send-holder"
    assert result["ignored_override"] is None


def test_submit_transaction_override_can_pierce_agent_only_hold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_quiet(monkeypatch, False)
    monkeypatch.setattr(send_gate, "typing_guard_active", lambda *, target=None: True)
    monkeypatch.setattr(
        send_gate,
        "_pane_guard_status",
        lambda target: {
            "kind": "agent",
            "until": 1300,
            "owner": "req-1",
            "active": True,
            "marker": "",
        },
    )
    monkeypatch.setattr(send_gate, "_pane_human_locked", lambda target: False)

    with send_gate.thread_local_override("tmuxctl-submit-transaction", owner="req-1"):
        result = send_gate.evaluate(("send-keys", "-t", "%44", "C-m"))

    assert result is not None
    assert result["reason"] == "typing_guard"
    assert result["policy"] == "pierce"
    assert result["suppressed"] is False
    assert result["override"] == "tmuxctl-submit-transaction"
    assert result["ignored_override"] is None
