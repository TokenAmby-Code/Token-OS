"""WS1 — core restart correctness: session-pinned resolution + unconditional,
verified persona/perpetual boot.

Two verified defects are guarded here:

1. ``resolve_pane`` chose the snapshot session via the target-less
   ``current_session_name()``. The restart executor runs detached after parking
   clients into ``_stash`` and killing old ``main``, so that ambient read does
   NOT return the rebuilt session and every persona/resume label resolves
   against the wrong session ("pane target not found" for panes that exist).
   The restart path must pin resolution to the explicit rebuilt session.

2. The post-rebuild persona/reservist assertion treated "no exception" as
   success. A seat counts only when the pane actually hosts a live agent.
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.assertions import PERSONA_LABELS
from tmuxctl.enums import GridState, PaneKind, WindowArchetype
from tmuxctl.executor import RestartExecutor
from tmuxctl.models import PaneSnapshot, WindowSnapshot, WorkspaceSnapshot
from tmuxctl.resolver import resolve_pane


def _pane(pane_id: str, role: str, window: str = "council") -> PaneSnapshot:
    return PaneSnapshot(
        pane_id=pane_id,
        session_name="main",
        window_index=4,
        window_name=window,
        pane_index=0,
        width=120,
        height=40,
        current_command="claude",
        tty="/dev/ttys004",
        pane_role=role,
        grid_state=GridState.SMALL,
        pane_kind=PaneKind.COUNCIL,
        reserved=False,
        active=True,
    )


def _main_workspace() -> WorkspaceSnapshot:
    window = WindowSnapshot(
        session_name="main",
        window_index=4,
        window_name="council",
        archetype=WindowArchetype.COUNCIL,
        focused=False,
        grid_expanded="none",
        grid_stash="",
        side_expanded="none",
        panes=(_pane("%41", "council:custodes"),),
    )
    return WorkspaceSnapshot(session_name="main", windows=(window,))


def _empty_workspace(session: str) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(session_name=session, windows=())


# ── Defect 1: session-pinned resolution after teardown/park ───────────────────


def test_resolve_pane_pins_session_after_teardown(monkeypatch):
    # Simulate the detached executor: the ambient session is the parked _stash,
    # NOT the freshly rebuilt main. A blind resolve snapshots _stash and misses
    # the persona pane that lives in main; a session-pinned resolve finds it.
    snapshots = {"main": _main_workspace(), "_stash": _empty_workspace("_stash")}
    monkeypatch.setattr(
        "tmuxctl.snapshot.build_workspace_snapshot",
        lambda adapter, session: snapshots.get(session, _empty_workspace(session)),
    )

    adapter = SimpleNamespace(
        current_session_name=lambda: "_stash",
        pinned_resolution_session=None,
    )

    # The bug: ambient resolution runs against _stash and fails for a live pane.
    with pytest.raises(ValueError, match="pane target not found"):
        resolve_pane(adapter, "council:custodes")

    # The fix: explicit session pin snapshots main and resolves the pane.
    resolved = resolve_pane(adapter, "council:custodes", session_name="main")
    assert resolved.pane_id == "%41"
    assert resolved.pane_role == "council:custodes"


def test_resolve_pane_honors_adapter_pin(monkeypatch):
    # The resume loop dispatches through the generic run() interception path,
    # which cannot pass an explicit session arg. The executor pins the adapter so
    # that path still resolves against the rebuilt session.
    snapshots = {"main": _main_workspace(), "_stash": _empty_workspace("_stash")}
    monkeypatch.setattr(
        "tmuxctl.snapshot.build_workspace_snapshot",
        lambda adapter, session: snapshots.get(session, _empty_workspace(session)),
    )

    adapter = SimpleNamespace(
        current_session_name=lambda: "_stash",
        pinned_resolution_session="main",
    )

    # No explicit session arg, but the adapter pin routes resolution at main.
    assert resolve_pane(adapter, "council:custodes").pane_id == "%41"

    # Explicit arg still wins over the pin, and an unset pin falls back to ambient.
    adapter.pinned_resolution_session = None
    with pytest.raises(ValueError, match="pane target not found"):
        resolve_pane(adapter, "council:custodes")


def test_pin_resolution_session_context_restores_previous():
    from tmuxctl.tmux_adapter import TmuxAdapter

    adapter = TmuxAdapter.__new__(TmuxAdapter)
    adapter.pinned_resolution_session = None
    with adapter.pin_resolution_session("main"):
        assert adapter.pinned_resolution_session == "main"
    assert adapter.pinned_resolution_session is None


# ── Defect 2: unconditional, live-agent-verified persona/reservist boot ────────


def _executor() -> RestartExecutor:
    return RestartExecutor(adapter=SimpleNamespace(pinned_resolution_session="main"))


def test_persistent_panes_boot_all_personas_and_reservists_session_pinned():
    # Every persona seat and both perpetual reservist seats are asserted on every
    # restart, independent of any DB row, and all resolution is pinned to the
    # rebuilt session (`main`).
    executor = _executor()
    asserted: list[tuple[str, str | None]] = []
    reservists: list[tuple[str, str | None]] = []

    def fake_assert(adapter, label, *, session=None):
        asserted.append((label, session))
        return {"ok": True, "action": "none"}

    def fake_ensure(label, target, cwd, prompt, session_name=None):
        reservists.append((target, session_name))
        return ""

    with (
        patch("tmuxctl.assertions.assert_instance", fake_assert),
        patch.object(executor, "_resolve_optional_pane", return_value="%99"),
        patch.object(executor, "_pane_has_agent_runtime", return_value=True),
        patch.object(executor, "_ensure_reservist_runtime", side_effect=fake_ensure),
    ):
        violations = executor._assert_persistent_runtime_panes("main")

    assert violations == []
    assert sorted(label for label, _ in asserted) == sorted(PERSONA_LABELS)
    assert all(session == "main" for _, session in asserted)
    assert {target for target, _ in reservists} == {"reservists:civic", "reservists:slot"}
    assert all(session == "main" for _, session in reservists)


def test_persistent_panes_flag_persona_without_live_agent():
    # R2: a persona whose assertion "succeeds" but whose pane hosts no live agent
    # is a hard verification failure — not silently accepted.
    executor = _executor()
    with (
        patch("tmuxctl.assertions.assert_instance", return_value={"ok": True, "action": "none"}),
        patch.object(executor, "_resolve_optional_pane", return_value="%99"),
        patch.object(executor, "_pane_has_agent_runtime", return_value=False),
        patch.object(executor, "_ensure_reservist_runtime", return_value=""),
    ):
        violations = executor._assert_persistent_runtime_panes("main")

    assert len(violations) == len(PERSONA_LABELS)
    assert all("no live agent" in v for v in violations)


def test_persistent_panes_flag_missing_persona_pane():
    # A persona pane absent from the rebuilt session is a verification failure,
    # never a silent pass.
    executor = _executor()
    with (
        patch("tmuxctl.assertions.assert_instance", return_value={"ok": True, "action": "none"}),
        patch.object(executor, "_resolve_optional_pane", return_value=""),
        patch.object(executor, "_pane_has_agent_runtime", return_value=True),
        patch.object(executor, "_ensure_reservist_runtime", return_value=""),
    ):
        violations = executor._assert_persistent_runtime_panes("main")

    assert len(violations) == len(PERSONA_LABELS)
    assert all("missing after assertion" in v for v in violations)
