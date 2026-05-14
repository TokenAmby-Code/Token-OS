from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import audience as audience_module
from tmuxctl.audience import _window_base, audience_toggle
from tmuxctl.enums import GridState, PaneKind, WindowArchetype
from tmuxctl.models import PaneSnapshot, WindowSnapshot, WorkspaceSnapshot
from tmuxctl.resolver import PaneResolution, resolve_pane_in_snapshot
from tmuxctl.snapshot import _window_warnings


def _pane(
    pane_id: str,
    role: str,
    *,
    kind: PaneKind = PaneKind.UNKNOWN,
    target: str = "",
    window: str = "palace",
    window_index: int = 1,
) -> PaneSnapshot:
    return PaneSnapshot(
        pane_id=pane_id,
        session_name="main",
        window_index=window_index,
        window_name=window,
        pane_index=1,
        width=100,
        height=40,
        current_command="zsh",
        tty="/dev/ttys001",
        pane_role=role,
        grid_state=GridState.SMALL,
        pane_kind=kind,
        reserved=False,
        active=False,
        tombstone_target=target,
        tombstone_source=role if kind is PaneKind.TOMBSTONE else "",
    )


def _workspace(*panes: PaneSnapshot) -> WorkspaceSnapshot:
    first = panes[0]
    return WorkspaceSnapshot(
        session_name="main",
        windows=(
            WindowSnapshot(
                session_name="main",
                window_index=first.window_index,
                window_name=first.window_name,
                archetype=WindowArchetype.PALACE,
                focused=False,
                grid_expanded="none",
                grid_stash="",
                side_expanded="none",
                panes=panes,
            ),
        ),
    )


def test_direct_physical_pane_resolves_to_itself():
    workspace = _workspace(_pane("%1", "palace:SW"))

    resolved = resolve_pane_in_snapshot(workspace, "%1")

    assert resolved.pane_id == "%1"
    assert resolved.chain == ("palace:SW",)


def test_logical_slot_resolves_to_physical_pane():
    workspace = _workspace(_pane("%1", "palace:SW"))

    resolved = resolve_pane_in_snapshot(workspace, "palace:SW")

    assert resolved.pane_id == "%1"


def test_legacy_logical_slot_alias_resolves_to_canonical_pane():
    workspace = _workspace(_pane("%1", "palace:NW"))

    resolved = resolve_pane_in_snapshot(workspace, "palace:TL")

    assert resolved.pane_id == "%1"


def test_canonical_logical_slot_resolves_to_legacy_pane_before_mutation():
    workspace = _workspace(_pane("%1", "palace:TL"))

    resolved = resolve_pane_in_snapshot(workspace, "palace:NW")

    assert resolved.pane_id == "%1"




def test_positional_window_index_slot_resolves_live_pane():
    workspace = _workspace(_pane("%1", "palace:N"))

    resolved = resolve_pane_in_snapshot(workspace, "1:N")

    assert resolved.pane_id == "%1"


def test_positional_window_index_legacy_slot_resolves_canonical_pane():
    workspace = _workspace(_pane("%1", "palace:N"))

    resolved = resolve_pane_in_snapshot(workspace, "1:NW")

    assert resolved.pane_id == "%1"


def test_positional_window_name_slot_resolves_live_pane():
    workspace = _workspace(_pane("%2", "somnium:SE", window="somnium"))

    resolved = resolve_pane_in_snapshot(workspace, "somnium:BR")

    assert resolved.pane_id == "%2"

def test_single_tombstone_resolves_to_target():
    workspace = _workspace(
        _pane("%1", "palace:SW", kind=PaneKind.TOMBSTONE, target="%9"),
        _pane("%9", "audience:palace:SW", window="_palace_audience"),
    )

    resolved = resolve_pane_in_snapshot(workspace, "palace:SW")

    assert resolved.pane_id == "%9"
    assert resolved.chain == ("palace:SW", "audience:palace:SW")


def test_double_tombstone_resolves_to_final_target():
    workspace = _workspace(
        _pane("%1", "legion:custodes", kind=PaneKind.TOMBSTONE, target="palace:NE"),
        _pane("%2", "palace:NE", kind=PaneKind.TOMBSTONE, target="%9"),
        _pane("%9", "audience:palace:NE", window="_palace_audience"),
    )

    resolved = resolve_pane_in_snapshot(workspace, "legion:custodes")

    assert resolved.pane_id == "%9"
    assert resolved.chain == ("legion:custodes", "palace:NE", "audience:palace:NE")


def test_missing_tombstone_target_errors_clearly():
    workspace = _workspace(_pane("%1", "palace:SW", kind=PaneKind.TOMBSTONE, target="%404"))

    with pytest.raises(ValueError, match="target not found: %404"):
        resolve_pane_in_snapshot(workspace, "palace:SW")


def test_tombstone_cycle_errors_clearly():
    workspace = _workspace(
        _pane("%1", "palace:SW", kind=PaneKind.TOMBSTONE, target="palace:NE"),
        _pane("%2", "palace:NE", kind=PaneKind.TOMBSTONE, target="palace:SW"),
    )

    with pytest.raises(ValueError, match="tombstone cycle detected"):
        resolve_pane_in_snapshot(workspace, "palace:SW")


def test_audience_window_base_strips_stack_spill_suffixes():
    assert _window_base("legion-2") == "legion"
    assert _window_base("mechanicus-12") == "mechanicus"
    assert _window_base("legion-2(3)") == "legion"


def test_audience_window_base_does_not_strip_non_audience_names():
    assert _window_base("mars-2") == "mars-2"
    assert _window_base("palace-west") == "palace-west"


def test_audience_window_warns_when_slot_is_split():
    warnings = _window_warnings(
        "_palace_audience",
        ["audience:palace:SW", "audience:palace:NE"],
        ["%1", "%2"],
        focused=False,
        grid_expanded="none",
        grid_stash="",
        side_expanded="none",
    )

    assert "audience window should contain exactly one pane" in warnings
    assert "audience window should contain exactly one audience pane" in warnings


def test_audience_window_warns_when_page_type_does_not_match():
    warnings = _window_warnings(
        "_palace_audience",
        ["audience:somnium:SW"],
        ["%1"],
        focused=False,
        grid_expanded="none",
        grid_stash="",
        side_expanded="none",
    )

    assert "audience pane role does not match window page: audience:somnium:W" in warnings


class FakeAudienceAdapter:
    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.commands.append(args)
        if args[:3] == ("display-message", "-t", "%5"):
            return "\t".join(
                [
                    "%5",
                    "main",
                    "somnium",
                    "somnium:EE",
                    "tui",
                    "",
                    "side",
                    "false",
                    "/Volumes/Imperium/Token-OS",
                ]
            )
        return ""


def test_tui_pane_toggle_selects_dedicated_tui_window():
    adapter = FakeAudienceAdapter()

    result = audience_toggle(adapter, "%5")

    assert result == "selected main:tui"
    assert ("select-window", "-t", "main:tui") in adapter.commands
    assert ("select-pane", "-t", "main:tui.1") in adapter.commands
    assert not any(command[0] == "split-window" for command in adapter.commands)


def test_audience_jump_reports_coordinate_id_not_percent_id(monkeypatch):
    class FakeAdapter:
        def show_pane_option(self, pane_id: str, option: str) -> str:
            if pane_id == "%9" and option == "@PANE_ID":
                return "audience:palace:NE"
            return ""

    selected: list[str] = []

    monkeypatch.setattr(
        audience_module,
        "resolve_pane",
        lambda _adapter, _target: PaneResolution(
            requested="%1",
            pane_id="%9",
            pane_role="audience:palace:NE",
            pane_kind=PaneKind.AUDIENCE,
            chain=("palace:NE", "audience:palace:NE"),
        ),
    )
    monkeypatch.setattr(
        audience_module,
        "_select_pane_for_client",
        lambda _adapter, pane_id, client="": selected.append(pane_id),
    )

    result = audience_module.audience_jump(FakeAdapter(), "%1")

    assert result == "selected palace:N via palace:NE -> palace:NE"
    assert "%9" not in result
    assert selected == ["%9"]
