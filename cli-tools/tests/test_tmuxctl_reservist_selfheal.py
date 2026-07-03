"""F3 — reservist mid-session self-heal ("keep the pulse").

Unit coverage for the daemon-native reservist heartbeat launcher that replaced the
retired executor ``dispatch --direct`` writer:

  * ``persona_seat_command(initial_prompt=…)`` carries the standby prompt env var —
    and personas (no initial_prompt) never do (regression-safe funnel).
  * ``reservist_spec`` values are lifted VERBATIM from the retired executor tuples.
  * ``launch_reservist_seat`` respawns the thin shim with the standby prompt +
    ``persona=""`` fast path; a gate-suppressed respawn stamps no phantom @PANE_BORN.
  * ``assert_reservist_seat`` seats a vacant pane exactly once, no-ops a live one,
    holds off within boot-grace, and treats a missing pane as NOT-a-launch (F2's
    layout owns pane recreation — the clean F2/F3 seam).

Pure-unit: fake pane ids, fake adapter, mocked registry/runtime. No live tmux.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import assertions
from tmuxctl.assertions import (
    RESERVIST_LABELS,
    PersonaSpec,
    ReservistSpec,
    _assert_reservist_seat_impl,
    launch_reservist_seat,
    persona_seat_command,
    persona_spec,
    reservist_spec,
    sweep_reservist_panes,
)

CIVIC_PROMPT = (
    "Stand by as the civic reservist runtime. Do not start new work. "
    "Wait for civic-thread fallthrough or operator instructions."
)
TOKEN_OS_PROMPT = (
    "Stand by as the Token-OS reservist runtime. Do not start new work. "
    "Wait for operator or orchestration instructions."
)


class FakeAdapter:
    """Records tmux commands + serves pane options; models the send gate."""

    def __init__(self, *, gate_on_respawn: bool = False) -> None:
        self.options: dict[str, str] = {}
        self.calls: list[tuple[str, ...]] = []
        self.last_send_gate_result = None
        self._gate_on_respawn = gate_on_respawn

    def run(self, *args, allow_failure: bool = False) -> str:
        self.calls.append(args)
        if args[:1] == ("respawn-pane",) and self._gate_on_respawn:
            self.last_send_gate_result = ("suppressed", "quiet-hours")
        if args and args[0] == "set-option":
            if "-pu" in args:
                self.options.pop(args[-1], None)
            else:
                self.options[args[-2]] = args[-1]
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self.options.get(option, "")

    @property
    def respawns(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[:1] == ("respawn-pane",)]

    @property
    def pane_born_set(self) -> bool:
        return any(c[:1] == ("set-option",) and "@PANE_BORN" in c for c in self.calls)


# ── spec table: single source of truth, lifted verbatim ──────────────────────


def test_reservist_labels_are_the_two_heartbeat_seats() -> None:
    assert RESERVIST_LABELS == ("reservists:civic", "reservists:token-os")


def test_reservist_spec_matches_retired_executor_constants(monkeypatch) -> None:
    monkeypatch.setenv("CIVIC_THREAD_PATH", "/Volumes/Civic")
    civic = reservist_spec("reservists:civic")
    assert civic.standby_prompt == CIVIC_PROMPT
    assert civic.working_dir == "/Volumes/Civic"
    # Engine/model/instance-type match the old dispatch --direct flags verbatim.
    assert civic.engine == "claude"
    assert civic.model == "sonnet"
    assert civic.instance_type == "hook_driven"

    token_os = reservist_spec("reservists:token-os")
    assert token_os.standby_prompt == TOKEN_OS_PROMPT


def test_reservist_spec_civic_dir_defaults_when_env_unset(monkeypatch) -> None:
    monkeypatch.delenv("CIVIC_THREAD_PATH", raising=False)
    assert reservist_spec("reservists:civic").working_dir == "/Volumes/Civic"


def test_reservist_spec_token_os_dir_uses_imperium(monkeypatch) -> None:
    monkeypatch.setenv("IMPERIUM", "/Volumes/Imperium")
    assert (
        reservist_spec("reservists:token-os").working_dir
        == "/Volumes/Imperium/runtimes/token-os/live"
    )


def test_reservist_spec_rejects_unknown_label() -> None:
    with pytest.raises(ValueError):
        reservist_spec("reservists:nope")


# ── persona_seat_command: initial_prompt carried for reservists, absent for personas


def test_persona_seat_command_carries_initial_prompt() -> None:
    spec = PersonaSpec("reservists:civic", "", "hook_driven", "", model="sonnet")
    cmd = persona_seat_command(
        spec,
        wrapper_launch_id="wl-r",
        shim_path="/x/persona-seat.sh",
        initial_prompt="keep the pulse",
    )
    assert "TOKEN_API_SEAT_INITIAL_PROMPT=" in cmd
    assert "keep the pulse" in cmd


def test_persona_seat_command_omits_initial_prompt_for_personas() -> None:
    # Every real persona seat is built WITHOUT initial_prompt → the env var never
    # appears, so the persona funnel is byte-identical to pre-F3.
    for label in sorted(assertions.PERSONA_LABELS):
        cmd = persona_seat_command(persona_spec(label), wrapper_launch_id="w")
        assert "TOKEN_API_SEAT_INITIAL_PROMPT" not in cmd


# ── launch_reservist_seat: respawn shim + standby prompt + gate handling ──────


def test_launch_reservist_seat_respawns_shim_with_standby_prompt(tmp_path) -> None:
    adapter = FakeAdapter()
    spec = ReservistSpec("reservists:token-os", str(tmp_path), "pulse-prompt")
    ok, reason = launch_reservist_seat(adapter, "%7", spec)

    assert ok is True
    assert reason == "launched"
    assert len(adapter.respawns) == 1
    command = adapter.respawns[0][-1]
    assert "persona-seat.sh" in command
    assert "TOKEN_API_SEAT_INITIAL_PROMPT=" in command
    assert "pulse-prompt" in command
    # persona="" fast path — no persona identity leaks into the reservist seat.
    assert "TOKEN_API_PERSONA= " in command or "TOKEN_API_PERSONA=''" in command
    assert adapter.pane_born_set is True


def test_launch_reservist_seat_gate_suppressed_stamps_no_pane_born(tmp_path) -> None:
    adapter = FakeAdapter(gate_on_respawn=True)
    spec = ReservistSpec("reservists:civic", str(tmp_path), "pulse")
    ok, reason = launch_reservist_seat(adapter, "%8", spec)

    assert ok is False
    assert reason == "respawn_suppressed_by_gate"
    # No phantom @PANE_BORN when the gate ate the respawn — the reconcile retries.
    assert adapter.pane_born_set is False


def test_launch_reservist_seat_falls_back_to_home_when_dir_missing(monkeypatch) -> None:
    adapter = FakeAdapter()
    monkeypatch.setenv("HOME", "/home/agent")
    spec = ReservistSpec("reservists:civic", "/nonexistent/civic/mount", "pulse")
    launch_reservist_seat(adapter, "%9", spec)
    command = adapter.respawns[0][-1]
    assert command.startswith("cd /home/agent && ")


# ── assert_reservist_seat: seat / no-op / boot-grace / pane_missing ───────────


def _fake_resolved(pane_id="%3", role="reservists:civic"):
    from types import SimpleNamespace

    return SimpleNamespace(pane_id=pane_id, pane_role=role)


def test_assert_reservist_seat_pane_missing_is_not_a_launch(monkeypatch) -> None:
    launched: list[str] = []

    def boom(adapter, target, session_name=None):
        raise ValueError("no such pane")

    monkeypatch.setattr(assertions, "resolve_pane", boom)
    monkeypatch.setattr(
        assertions, "launch_reservist_seat", lambda *a, **k: launched.append("x") or (True, "l")
    )
    out = _assert_reservist_seat_impl(FakeAdapter(), "reservists:civic")

    assert out["ok"] is False
    assert out["action"] == "pane_missing"
    assert "pane_missing" in out["reason"]
    assert launched == []  # recreating the pinned pane is F2's job, never a launch


def test_assert_reservist_seat_live_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(
        assertions, "resolve_pane", lambda a, t, session_name=None: _fake_resolved()
    )
    monkeypatch.setattr(assertions, "_runtime_has_instance", lambda a, p: True)
    adapter = FakeAdapter()
    out = _assert_reservist_seat_impl(adapter, "reservists:civic")

    assert out["ok"] is True
    assert out["action"] == "none"
    assert out["reason"] == "live"
    assert adapter.respawns == []  # a live seat is never re-seated


def test_assert_reservist_seat_vacant_launches_exactly_once(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        assertions, "resolve_pane", lambda a, t, session_name=None: _fake_resolved()
    )
    monkeypatch.setattr(assertions, "_runtime_has_instance", lambda a, p: False)
    monkeypatch.setattr(assertions, "_registry_entries", lambda *a, **k: [])
    monkeypatch.setattr(assertions, "_persona_within_boot_grace", lambda a, p, rows: False)
    monkeypatch.setattr(
        assertions, "reservist_spec", lambda label: ReservistSpec(label, str(tmp_path), "pulse")
    )

    adapter = FakeAdapter()
    out = _assert_reservist_seat_impl(adapter, "reservists:civic")

    assert out["ok"] is True
    assert out["action"] == "launched"
    assert len(adapter.respawns) == 1  # one respawn-pane, not a storm


def test_assert_reservist_seat_within_boot_grace_holds_off(monkeypatch) -> None:
    monkeypatch.setattr(
        assertions, "resolve_pane", lambda a, t, session_name=None: _fake_resolved()
    )
    monkeypatch.setattr(assertions, "_runtime_has_instance", lambda a, p: False)
    monkeypatch.setattr(assertions, "_registry_entries", lambda *a, **k: [])
    monkeypatch.setattr(assertions, "_persona_within_boot_grace", lambda a, p, rows: True)

    adapter = FakeAdapter()
    out = _assert_reservist_seat_impl(adapter, "reservists:civic")

    assert out["ok"] is False
    assert out["action"] == "boot_grace"
    assert out["reason"] == "reservist_boot_grace"
    assert adapter.respawns == []  # double-seat guard: a booting seat is not re-seated


# ── sweep_reservist_panes: fill-on-absence over both seats ────────────────────


def test_sweep_reservist_panes_covers_both_seats(monkeypatch) -> None:
    seen: list[str] = []

    def fake_assert(adapter, target, *, session=None):
        seen.append(target)
        return {"ok": True, "pane_label": target, "action": "none"}

    monkeypatch.setattr(assertions, "assert_reservist_seat", fake_assert)
    results = sweep_reservist_panes(FakeAdapter(), session="main")

    assert seen == list(RESERVIST_LABELS)
    assert len(results) == 2


def test_sweep_reservist_panes_one_bad_pane_does_not_abort(monkeypatch) -> None:
    def fake_assert(adapter, target, *, session=None):
        if target == "reservists:civic":
            raise RuntimeError("boom")
        return {"ok": True, "pane_label": target, "action": "none"}

    monkeypatch.setattr(assertions, "assert_reservist_seat", fake_assert)
    results = sweep_reservist_panes(FakeAdapter())

    assert results[0]["action"] == "error"
    assert results[1]["ok"] is True  # the other seat still swept


# ── executor convergence: the daemon seats; the restart executor only verifies ─


def test_executor_reservist_writer_is_deleted() -> None:
    # The divergent `dispatch --direct` reservist writer (and its helpers) are gone —
    # the daemon reconcile is the sole reservist launcher, so no double-seat race.
    from tmuxctl.executor import RestartExecutor

    assert not hasattr(RestartExecutor, "_ensure_reservist_runtime")
    assert not hasattr(RestartExecutor, "_dispatch_binary")
    assert not hasattr(RestartExecutor, "_token_os_dir")


def test_executor_r2_verifies_reservists_without_spawning_dispatch(monkeypatch) -> None:
    from tmuxctl import executor as executor_mod
    from tmuxctl.executor import RestartExecutor

    ex = RestartExecutor(adapter=FakeAdapter())
    verified: list[str] = []

    monkeypatch.setattr(ex, "_daemon_reconcile_personas", lambda s: [])
    monkeypatch.setattr(
        ex,
        "_resolve_optional_pane",
        lambda label, session_name=None: verified.append(label) or "%1",
    )
    monkeypatch.setattr(ex, "_pane_has_agent_runtime", lambda pane_id: True)

    def no_subprocess(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("restart executor must not spawn a subprocess to seat reservists")

    monkeypatch.setattr(executor_mod.subprocess, "run", no_subprocess)

    violations = ex._assert_persistent_runtime_panes("main")

    assert violations == []
    # Both reservist seats were R2-verified (read-only), never dispatch-seated.
    assert "reservists:civic" in verified
    assert "reservists:token-os" in verified


def test_executor_r2_flags_reservist_with_no_live_agent(monkeypatch) -> None:
    from tmuxctl.executor import RestartExecutor

    ex = RestartExecutor(adapter=FakeAdapter())
    monkeypatch.setattr(ex, "_daemon_reconcile_personas", lambda s: [])
    monkeypatch.setattr(ex, "_resolve_optional_pane", lambda label, session_name=None: "%1")
    # Personas live; reservists have no agent → each reservist is a violation.
    monkeypatch.setattr(
        ex,
        "_pane_has_agent_runtime",
        lambda pane_id: True,
    )

    def resolve(label, session_name=None):
        return "" if label.startswith("reservists:") else "%1"

    monkeypatch.setattr(ex, "_resolve_optional_pane", resolve)
    violations = ex._assert_persistent_runtime_panes("main")

    assert any("reservist pane missing after assertion: reservists:civic" in v for v in violations)
