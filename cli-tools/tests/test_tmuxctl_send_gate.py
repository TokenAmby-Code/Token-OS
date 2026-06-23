"""Regression tests for the universal tmux send gate (the pane-write sentinel).

Invariant under test: quiet hours cancel automated pane writes by default;
the typing guard delays automated writes by default; sanctioned direct-input
sends pierce but are audited. Reads are never gated.

The typing guard is the keystroke-anchored per-pane lock: the tmux any-key
binding stamps ``@TYPING_LOCK_UNTIL`` (an absolute expiry epoch) the moment the
Emperor first types into a pane, and an Enter clears it. The gate reads that one
option — focus-decoupled, no screen-scraping — so these tests fake
``show-options -pqv -t <pane> @TYPING_LOCK_UNTIL`` (a future epoch = locked, an
empty/past value = clear) rather than the retired ``client_activity`` /
attendance / pending-prompt model.
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
    # The border predicate and the send-path predicate are split; evaluate() reads
    # the send-path one, so force both to the same value for these gate tests.
    monkeypatch.setattr(send_gate, "typing_guard_active", lambda **kw: active)
    monkeypatch.setattr(send_gate, "send_hold_active", lambda **kw: active)


def _no_override(monkeypatch):
    monkeypatch.setattr(send_gate, "sanctioned_override", lambda: None)


def _lock_tmux(monkeypatch, locks: dict[str, int | None], *, panes: list[str] | None = None):
    """Fake real tmux so the keystroke-lock reader sees ``locks`` (pane -> epoch).

    Answers exactly the two commands the predicate issues:
      * ``show-options -pqv -t <pane> @TYPING_LOCK_UNTIL`` → the pane's lock epoch
        (empty line + exit 0 when unset, matching real ``-pqv``).
      * ``list-panes -a -F #{pane_id}``                    → the live pane ids.
    Any other command errors (rc=1), matching the gate's fail-open contract.
    A pane mapped to ``None`` (or absent) reads as unlocked.
    """
    monkeypatch.setattr(send_gate, "_real_tmux_binary", lambda: "tmux")

    def _fake_run(cmd, *args, **kwargs):
        proc = _FakeCompleted()
        if "show-options" in cmd and send_gate._TYPING_LOCK_OPTION in cmd:
            target = None
            for idx, tok in enumerate(cmd):
                if tok == "-t" and idx + 1 < len(cmd):
                    target = cmd[idx + 1]
                    break
            value = locks.get(target)
            proc.stdout = "" if value is None else f"{int(value)}\n"
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

    # evaluate() consults the send-path predicate; first call holds, then clears.
    monkeypatch.setattr(send_gate, "send_hold_active", _typing_once)
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
    """The lock is per-pane: a live ``@TYPING_LOCK_UNTIL`` on one pane never
    leaks onto another. ``%active`` was typed into (lock 200s out); ``%other``
    carries an expired lock and reads clear."""
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

    The Emperor typed into ``%active`` ~1 min ago, so its lock is still ~4 min
    out; ``%other`` is an unrelated worker pane he never typed into (no lock). A
    dispatch send to ``%other`` MUST sail through while a send to ``%active`` is
    held — typing in one pane never blocks an unrelated pane. No monkeypatch of
    the predicate: evaluate() runs the real ``typing_guard_active`` over a faked
    tmux reading only ``@TYPING_LOCK_UNTIL``.
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
    """No screen-scraping: the keystroke lock is the SOLE signal.

    A worker pane the Emperor never typed into carries no lock, so a
    brief/dispatch send sails through (evaluate → None) even if its screen still
    shows leftover prompt text. This is the over-block the retired
    ``_pane_has_pending_input`` detector caused — holding W's brief-delivery to
    idle worker panes merely because they had prompt text on screen. Stale draft
    text with no live lock is, by the Emperor's accepted tradeoff, deliverable.
    """
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
    """Focus-decoupled persistence: a locked pane stays guarded purely on the
    absolute expiry, with no focus/attendance/keystroke re-check. The same pane
    releases the instant its expiry falls into the past."""
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    locks: dict[str, int | None] = {"%active": now + 1}
    _lock_tmux(monkeypatch, locks)

    # Lock still future → held (no focus signal is consulted at all).
    assert send_gate.typing_guard_active(target="%active") is True

    # Same pane, expiry now in the past → released. Nothing else changed.
    locks["%active"] = now - 1
    assert send_gate.typing_guard_active(target="%active") is False


def test_evaluate_only_blocks_target_under_typing_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    monkeypatch.setattr(
        send_gate,
        "send_hold_active",
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
    # These pin the pure lock-expiry sleep cadence; the post-drop send grace is
    # exercised separately, so disable it here to keep the deadline at the lock.
    monkeypatch.setenv("TMUX_SEND_GRACE_SECONDS", "0")
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
    """An Enter into the pane clears ``@TYPING_LOCK_UNTIL`` mid-wait. Even though
    the original lock ran 5 min out, the recheck cap releases the held send
    within ~1 wake of the clear — the gate must not wait out the full window."""
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    cleared_at = fake_clock["now"] + 2  # Emperor presses Enter 2s in

    def _fake_run(cmd, *args, **kwargs):
        proc = _FakeCompleted()
        if "show-options" in cmd and send_gate._TYPING_LOCK_OPTION in cmd:
            # 5-min lock until an Enter clears it at cleared_at.
            proc.stdout = (
                "" if fake_clock["now"] >= cleared_at else f"{int(fake_clock['now'] + 300)}\n"
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
# The clobber's root cause: the gate's typing-lock read shells out to
# `tmux show-options -pqv -t <target> @TYPING_LOCK_UNTIL`. tmux only understands
# physical %pane ids (and native session:window addresses). An Imperium
# canonical id (mechanicus:fabricator-general, legion:custodes, 1:N…) cannot be
# resolved at the tmux boundary, so the read errors and the guard MISSES an
# actively-typed pane — then a send would clobber the human's live draft. The
# fix is to resolve canonical→physical BEFORE the gate (see the adapter test).


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
        if "show-options" in cmd and send_gate._TYPING_LOCK_OPTION in cmd:
            target = None
            for idx, tok in enumerate(cmd):
                if tok == "-t" and idx + 1 < len(cmd):
                    target = cmd[idx + 1]
                    break
            if target == "%44":
                proc.stdout = f"{now + 200}\n"  # physical pane carries a live lock
                return proc
            proc.returncode = 1  # tmux can't resolve a canonical id → fail-open clear
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

    monkeypatch.setattr(send_gate, "send_hold_active", _typing)

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


# ── Edge-fuzz refinements: the lock-lifecycle boundaries (R1/R2/R3) ───────────
# The BORDER predicate (typing_guard_active) and the SEND predicate
# (send_hold_active) deliberately diverge: the border darkens the instant the lock
# drops / the draft is abandoned, while a send is held for the grace so it never
# clobbers a 2nd queued message. These fake the four per-pane options the
# combined state reads (lock / grace / bs_run / bs_last).


def _guard_tmux(monkeypatch, opts: dict[str, int | None]) -> None:
    """Fake real tmux so ``_pane_guard_state`` reads ``opts`` (option name -> int).

    Answers each ``show-options -pqv -t <pane> <OPTION>`` with ``opts[OPTION]``
    (empty line + rc0 when None/absent, matching real ``-pqv`` for an unset
    option). Single-pane; any other tmux command errors (fail-open).
    """
    monkeypatch.setattr(send_gate, "_real_tmux_binary", lambda: "tmux")
    known = (
        send_gate._TYPING_LOCK_OPTION,
        send_gate._TYPING_GRACE_OPTION,
        send_gate._BS_RUN_OPTION,
        send_gate._BS_LAST_OPTION,
    )

    def _fake_run(cmd, *args, **kwargs):
        proc = _FakeCompleted()
        if "show-options" in cmd:
            for name in known:
                if name in cmd:
                    val = opts.get(name)
                    proc.stdout = "" if val is None else f"{int(val)}\n"
                    return proc
            proc.stdout = ""
            return proc
        proc.returncode = 1
        return proc

    monkeypatch.setattr(send_gate.subprocess, "run", _fake_run)


def _clear_edge_env(monkeypatch) -> None:
    for env in (
        "TMUX_SEND_GRACE_SECONDS",
        "TMUX_BS_ABANDON_RUN",
        "TMUX_BS_STOP_IDLE_SECONDS",
    ):
        monkeypatch.delenv(env, raising=False)


def test_enter_grace_darkens_border_but_holds_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """R2: after Enter the lock is unset (border dark) but @TYPING_GRACE_UNTIL holds
    the send for the grace, so an automated write can't fire into the gap while the
    Emperor queues a follow-up message."""
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    _clear_edge_env(monkeypatch)
    _guard_tmux(monkeypatch, {send_gate._TYPING_GRACE_OPTION: now + 5})

    assert send_gate.typing_guard_active(target="%9") is False, "border dark once lock is unset"
    assert send_gate.send_hold_active(target="%9") is True, "send still held for the grace"
    assert send_gate._pane_guard_state("%9").hold_kind == "grace"


def test_rearm_during_grace_reextends_the_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    """R2: a keystroke during the grace re-arms @TYPING_LOCK_UNTIL to now+300, so the
    border re-lights and the send-hold deadline jumps to lock+grace — the lingering
    grace floor is dominated (re-queue), never an early release."""
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    _clear_edge_env(monkeypatch)
    # Lingering Enter grace (now+5) PLUS a fresh re-arm to now+300, run reset to 0.
    _guard_tmux(
        monkeypatch,
        {
            send_gate._TYPING_LOCK_OPTION: now + 300,
            send_gate._TYPING_GRACE_OPTION: now + 5,
            send_gate._BS_RUN_OPTION: 0,
        },
    )

    assert send_gate.typing_guard_active(target="%9") is True, "border re-lit by the re-arm"
    state = send_gate._pane_guard_state("%9")
    assert state.hold_kind == "lock"
    # lock(now+300) + grace(5) dominates the now+5 floor.
    assert state.send_hold_until == pytest.approx(now + 305)


def test_backspace_abandon_darkens_border_and_holds_only_for_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R3: >= 4 backspaces then >= 1.5s idle reads as abandon — the border darkens
    DESPITE a live future lock, and the send is held only to the deletion-idle point
    plus the grace (NOT the stale remaining lock), so a real abandon injects cleanly
    once the grace lapses."""
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    _clear_edge_env(monkeypatch)
    _guard_tmux(
        monkeypatch,
        {
            send_gate._TYPING_LOCK_OPTION: now + 200,  # lock still live/future…
            send_gate._BS_RUN_OPTION: 4,
            send_gate._BS_LAST_OPTION: now - 2,  # …but deleted-and-idle 2s ago
        },
    )

    assert send_gate.typing_guard_active(target="%9") is False, "border dark on abandon"
    assert send_gate.send_hold_active(target="%9") is True
    state = send_gate._pane_guard_state("%9")
    assert state.hold_kind == "abandon_grace"
    # held to bs_last + STOP_IDLE(1.5) + grace(5) = now+4.5, NOT the now+200 lock.
    assert state.send_hold_until == pytest.approx(now - 2 + 1.5 + 5)


def test_backspace_run_without_idle_stays_lit(monkeypatch: pytest.MonkeyPatch) -> None:
    """R3 boundary: a backspace run that has NOT yet gone idle (still mid-deletion)
    is not an abandon — the border stays lit and the hold is the normal live lock."""
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    _clear_edge_env(monkeypatch)
    _guard_tmux(
        monkeypatch,
        {
            send_gate._TYPING_LOCK_OPTION: now + 200,
            send_gate._BS_RUN_OPTION: 4,
            send_gate._BS_LAST_OPTION: now,  # just backspaced — 0s < 1.5s idle
        },
    )

    assert send_gate.typing_guard_active(target="%9") is True, "still lit mid-deletion"
    assert send_gate.send_hold_active(target="%9") is True
    assert send_gate._pane_guard_state("%9").hold_kind == "lock"


def test_untouched_pane_all_options_unset_adds_zero_latency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2/R3 must not tax untouched panes: all four options unset → no border, no
    hold, no send_hold deadline, and evaluate() allows the send immediately."""
    now = 1_700_000_000
    monkeypatch.setattr(send_gate.time, "time", lambda: now)
    _clear_edge_env(monkeypatch)
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    _guard_tmux(monkeypatch, {})

    assert send_gate.typing_guard_active(target="%9") is False
    assert send_gate.send_hold_active(target="%9") is False
    assert send_gate._pane_guard_state("%9").send_hold_until is None
    assert send_gate.evaluate(("send-keys", "-t", "%9", "hi")) is None


def test_wait_for_gate_clear_requeues_when_lock_rearms_during_grace(
    monkeypatch: pytest.MonkeyPatch, fake_clock: dict
) -> None:
    """R2 end-to-end: a keystroke re-arms the lock mid-grace, so the held send keeps
    waiting past the original grace floor (it re-queues) rather than releasing."""
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    monkeypatch.setenv("TMUX_SEND_GRACE_SECONDS", "5")
    start = fake_clock["now"]
    rearm_at = start + 2  # a keystroke 2s into the grace re-arms the lock
    monkeypatch.setenv("TMUX_SEND_GATE_DELAY_TIMEOUT", "30")  # terminate if logic regresses

    def _fake_run(cmd, *args, **kwargs):
        proc = _FakeCompleted()
        if "show-options" in cmd:
            t = fake_clock["now"]
            if send_gate._TYPING_LOCK_OPTION in cmd:
                proc.stdout = f"{int(rearm_at + 300)}\n" if t >= rearm_at else ""
            elif send_gate._TYPING_GRACE_OPTION in cmd:
                proc.stdout = f"{int(start + 5)}\n"  # the Enter grace floor
            else:
                proc.stdout = ""
            return proc
        proc.returncode = 1
        return proc

    monkeypatch.setattr(send_gate, "_real_tmux_binary", lambda: "tmux")
    monkeypatch.setattr(send_gate.subprocess, "run", _fake_run)

    # The re-armed lock (rearm_at+300)+grace outlasts the 30s timeout → held, not
    # released at the original now+5 floor: the send re-queued behind the new draft.
    assert send_gate.wait_for_gate_clear(("send-keys", "-t", "%9", "hi")) is False
    assert fake_clock["now"] >= start + 5, "kept holding past the original grace floor"


def test_typing_delay_sleep_grace_only_hold_does_not_busyspin(
    monkeypatch: pytest.MonkeyPatch, fake_clock: dict
) -> None:
    """R2 regression guard: a grace-only hold (the lock already PAST, only the grace
    floor future) must sleep toward send_hold_until — not hit the 0.05s expiry floor
    and busy-spin for the whole grace. fake_clock sets grace env 0, so the future
    @TYPING_GRACE_UNTIL is the sole hold term."""
    _force_quiet(monkeypatch, False)
    _no_override(monkeypatch)
    start = fake_clock["now"]

    def _fake_run(cmd, *args, **kwargs):
        proc = _FakeCompleted()
        if "show-options" in cmd:
            if send_gate._TYPING_LOCK_OPTION in cmd:
                proc.stdout = f"{int(start - 10)}\n"  # lock long expired (stale stamp)
            elif send_gate._TYPING_GRACE_OPTION in cmd:
                proc.stdout = f"{int(start + 3)}\n"  # 3s of grace remaining
            else:
                proc.stdout = ""
            return proc
        proc.returncode = 1
        return proc

    monkeypatch.setattr(send_gate, "_real_tmux_binary", lambda: "tmux")
    monkeypatch.setattr(send_gate.subprocess, "run", _fake_run)

    assert send_gate.wait_for_gate_clear(("send-keys", "-t", "%9", "hi")) is True

    sleeps = fake_clock["sleeps"]
    assert sleeps, "a grace-only hold must sleep at least once"
    assert all(s <= send_gate._TYPING_LOCK_RECHECK_SECONDS + 1e-9 for s in sleeps), (
        "each wake is capped at the recheck interval"
    )
    assert fake_clock["now"] >= start + 3, "wakes carry the clock past the grace end"
    assert len(sleeps) <= 5, "~3s grace at a 1s recheck → a handful of wakes, not a busy-spin"
