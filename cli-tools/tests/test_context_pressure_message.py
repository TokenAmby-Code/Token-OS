"""Plan-aware context-pressure nudge (Emperor ruling 2026-07-02).

An instance already in plan mode must NOT receive the "switch to plan mode OR
run /compact" prompt — it derails a planning turn. It must instead be told to
pose its plan without gathering more context.
"""

from __future__ import annotations

import importlib.util
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "cli-tools" / "lib"
TMUX_CONTEXT = ROOT / "cli-tools" / "bin" / "tmux-context"

sys.path.insert(0, str(LIB))
import context_pressure_message as cpm  # noqa: E402


def test_standard_message_when_not_planning() -> None:
    for state in (None, "", "none", "NONE", "working"):
        msg = cpm.context_full_message(state)
        assert "switch to plan mode OR run /compact" in msg
        assert "update your session document" in msg


def test_plan_mode_message_does_not_tell_the_agent_to_enter_plan_or_compact() -> None:
    for state in ("planning", "preplanning", "approving", "  Planning  "):
        msg = cpm.context_full_message(state)
        # The whole point: no plan-or-compact prompt for an already-planning turn.
        assert "/compact" not in msg
        assert "switch to plan mode" not in msg
        # It tells the agent to pose the plan without gathering context.
        assert "Do NOT gather" in msg
        assert "plan mode" in msg


def test_is_plan_active_classification() -> None:
    assert cpm.is_plan_active("planning") is True
    assert cpm.is_plan_active("preplanning") is True
    assert cpm.is_plan_active("approving") is True
    assert cpm.is_plan_active("none") is False
    assert cpm.is_plan_active(None) is False
    assert cpm.is_plan_active("") is False


def _load_tmux_context():
    loader = SourceFileLoader("tmux_context_under_test", str(TMUX_CONTEXT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_tmux_context_check_pre_compact_threads_planning_state_into_message(
    monkeypatch, tmp_path
) -> None:
    """The status hook must pass planning_state through so the injected nudge is
    plan-aware — an in-plan-mode instance gets the no-gather-context message."""
    module = _load_tmux_context()

    sent: list[str] = []

    class FakePopen:
        def __init__(self, argv, *a, **k):
            # argv = [agent_cmd, "--pane", pane, msg]
            sent.append(argv[-1])

    monkeypatch.setattr(module.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(module, "tmux_pane_has_input", lambda _pane: False)
    # Keep the test hermetic: cooldown marker writes go to a tmp dir, never real
    # /tmp, and the exists() stub keeps both calls past the cooldown gate.
    monkeypatch.setattr(module, "STATE_DIR", str(tmp_path))
    monkeypatch.setattr(module.os.path, "exists", lambda _p: False)
    monkeypatch.setenv("TMUX", "1")

    # Well over the 250k flush threshold (pct * total / 100).
    module.check_pre_compact("%42", 90, 300_000, planning_state="planning")
    assert sent, "a nudge should have been injected"
    assert "/compact" not in sent[-1]
    assert "Do NOT gather" in sent[-1]

    sent.clear()
    module.check_pre_compact("%42", 90, 300_000, planning_state="none")
    assert sent
    assert "switch to plan mode OR run /compact" in sent[-1]
