from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.enums import GridState, PaneKind, WindowArchetype
from tmuxctl.models import PaneSnapshot, WindowSnapshot, WorkspaceSnapshot
from tmuxctl.resolver import resolve_pane, resolve_to_physical, resolve_to_public


def _pane(pane_id: str, role: str | None, *, session: str = "main") -> PaneSnapshot:
    return PaneSnapshot(
        pane_id=pane_id,
        session_name=session,
        window_index=1,
        window_name="palace",
        pane_index=0,
        width=80,
        height=24,
        current_command="zsh",
        tty="/dev/ttys001",
        pane_role=role,
        grid_state=GridState.UNKNOWN,
        pane_kind=PaneKind.UNKNOWN,
        reserved=False,
        active=False,
    )


def _workspace(session: str, *panes: PaneSnapshot) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        session_name=session,
        windows=(
            WindowSnapshot(
                session_name=session,
                window_index=1,
                window_name="palace",
                archetype=WindowArchetype.PALACE,
                focused=False,
                grid_expanded="none",
                grid_stash="",
                side_expanded="none",
                panes=panes,
            ),
        ),
    )


class PhysicalTargetAdapter:
    pinned_resolution_session: str | None = None

    def __init__(self, physical_session: str = "physical") -> None:
        self.physical_session = physical_session
        self.current_session_called = False

    def current_session_name(self) -> str:
        self.current_session_called = True
        return "ambient"

    def show_pane_option(self, pane_id: str, option: str) -> str:
        if option == "@PANE_ID":
            return "physical:N" if pane_id == "%88" else ""
        return ""

    def run(self, *args: str, allow_failure: bool = False) -> str:
        assert args[:2] == ("display-message", "-t")
        target = args[2]
        fmt = args[-1]
        if fmt == "#{pane_id}":
            return "%88" if target == "%88" else ""
        return "\t".join(
            [
                "%88",
                self.physical_session,
                "1",
                "palace",
                "0",
                "80",
                "24",
                "zsh",
                "/dev/ttys001",
                "1",
            ]
        )


def test_physical_target_uses_its_own_session_before_explicit_or_pinned(monkeypatch):
    seen_sessions: list[str] = []

    def fake_snapshot(adapter: object, session: str) -> WorkspaceSnapshot:
        seen_sessions.append(session)
        if session == "physical":
            return _workspace("physical", _pane("%88", "physical:N", session="physical"))
        return _workspace(session)

    monkeypatch.setattr("tmuxctl.snapshot.build_workspace_snapshot", fake_snapshot)
    adapter = PhysicalTargetAdapter(physical_session="physical")
    adapter.pinned_resolution_session = "pinned"

    resolved = resolve_pane(adapter, "%88", session_name="explicit")

    assert resolved.pane_id == "%88"
    assert seen_sessions == ["physical"]
    assert adapter.current_session_called is False


def test_resolve_to_physical_returns_raw_target_after_public_resolution(monkeypatch):
    monkeypatch.setattr(
        "tmuxctl.snapshot.build_workspace_snapshot",
        lambda adapter, session: _workspace("main", _pane("%41", "council:custodes")),
    )
    adapter = PhysicalTargetAdapter()
    adapter.current_session_name = lambda: "main"  # type: ignore[method-assign]

    assert resolve_to_physical(adapter, "council:custodes") == "%41"
    assert resolve_to_public(adapter, "council:custodes") == "council:custodes"


def test_resolve_to_public_fails_when_physical_target_has_no_public_id(monkeypatch):
    monkeypatch.setattr(
        "tmuxctl.snapshot.build_workspace_snapshot",
        lambda adapter, session: _workspace("physical", _pane("%88", None, session="physical")),
    )
    adapter = PhysicalTargetAdapter(physical_session="physical")
    adapter.show_pane_option = lambda pane_id, option: ""  # type: ignore[method-assign]

    assert resolve_to_physical(adapter, "%88") == "%88"
    with pytest.raises(ValueError, match="pane target has no public @PANE_ID: %88"):
        resolve_to_public(adapter, "%88")
