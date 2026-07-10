"""Ambiguous semantic pane labels must fail loud, never first-writer-wins.

Cluster A P0 regression pack for the custodes→malcador silent misroute
(Mars/Bugs/custodes-addressed-worker-reports-misdelivered-into-malcador-pane):
a report addressed to ``council:custodes`` was delivered into the live
``council:malcador`` pane. The resolver indexed duplicate/churned ``@PANE_ID``
stamps with ``setdefault`` (first enumeration wins) and silently picked a pane.
Under the #600 loud-fail ruling an ambiguous semantic label is a stop-the-world
resolution error, not a tie to break.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.enums import GridState, PaneKind, WindowArchetype
from tmuxctl.models import PaneSnapshot, WindowSnapshot, WorkspaceSnapshot
from tmuxctl.resolver import resolve_pane_in_snapshot


def pane(pid: str, role: str, window_index: int = 4, window_name: str = "council") -> PaneSnapshot:
    return PaneSnapshot(
        pane_id=pid,
        session_name="main",
        window_index=window_index,
        window_name=window_name,
        pane_index=0,
        width=80,
        height=24,
        current_command="zsh",
        tty="/dev/ttys000",
        pane_role=role,
        grid_state=GridState.UNKNOWN,
        pane_kind=PaneKind.UNKNOWN,
        reserved=False,
        active=False,
    )


def tombstone(pid: str, role: str) -> PaneSnapshot:
    """A council pane carrying the DESIGNED audience-redirect tombstone kind."""
    return PaneSnapshot(
        pane_id=pid,
        session_name="main",
        window_index=4,
        window_name="council",
        pane_index=0,
        width=80,
        height=24,
        current_command="zsh",
        tty="/dev/ttys000",
        pane_role=role,
        grid_state=GridState.UNKNOWN,
        pane_kind=PaneKind.TOMBSTONE,
        reserved=False,
        active=False,
    )


def council_workspace(*panes: PaneSnapshot) -> WorkspaceSnapshot:
    return WorkspaceSnapshot(
        session_name="main",
        windows=(
            WindowSnapshot(
                session_name="main",
                window_index=4,
                window_name="council",
                archetype=WindowArchetype.COUNCIL,
                focused=False,
                grid_expanded="",
                grid_stash="",
                side_expanded="",
                panes=tuple(panes),
            ),
        ),
    )


def test_custodes_resolves_to_custodes_never_malcador() -> None:
    """DoD 4(a): with sane stamps, council:custodes is the Custodes pane only."""
    snapshot = council_workspace(
        pane("%28", "council:custodes"),
        pane("%29", "council:pax"),
        pane("%30", "council:malcador"),
    )
    resolved = resolve_pane_in_snapshot(snapshot, "council:custodes")
    assert resolved.pane_id == "%28"
    assert resolved.pane_role == "council:custodes"
    assert resolve_pane_in_snapshot(snapshot, "council:malcador").pane_id == "%30"


def test_duplicate_singleton_label_fails_loud_not_first_writer() -> None:
    """The misroute red: two live panes stamped council:custodes (stamp churn
    window) must raise, not silently deliver to the first enumerated pane."""
    snapshot = council_workspace(
        pane("%30", "council:custodes"),  # churned/wrong pane enumerates first
        pane("%28", "council:custodes"),  # the real seat
        pane("%31", "council:malcador"),
    )
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_pane_in_snapshot(snapshot, "council:custodes")


def test_duplicate_singleton_positional_alias_fails_loud() -> None:
    """The window-index positional alias (``4:custodes``) is the same address
    space and must fail the same way on duplicates."""
    snapshot = council_workspace(
        pane("%30", "council:custodes"),
        pane("%28", "council:custodes"),
    )
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_pane_in_snapshot(snapshot, "4:custodes")


def test_duplicate_worker_role_fails_loud() -> None:
    """Non-singleton semantic labels are covered too: an ambiguous label is an
    ambiguous label. Callers that want a specific pane use the physical id."""
    snapshot = WorkspaceSnapshot(
        session_name="main",
        windows=(
            WindowSnapshot(
                session_name="main",
                window_index=2,
                window_name="somnium",
                archetype=WindowArchetype.SOMNIUM,
                focused=False,
                grid_expanded="",
                grid_stash="",
                side_expanded="",
                panes=(
                    pane("%31", "somnium:NE", window_index=2, window_name="somnium"),
                    pane("%32", "somnium:NE", window_index=2, window_name="somnium"),
                ),
            ),
        ),
    )
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_pane_in_snapshot(snapshot, "somnium:NE")
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_pane_in_snapshot(snapshot, "2:NE")


def test_two_live_claims_separated_by_tombstone_fail_loud() -> None:
    """A tombstone redirect indexed BETWEEN two distinct live claimants must not
    mask their collision. Old code let the tombstone absorb the second live claim
    (early return, no poison); the live-claimant tracker poisons the key so two
    distinct live @PANE_ID stamps always fail loud, tombstone interposed or not."""
    snapshot = council_workspace(
        pane("%28", "council:custodes"),  # first live claim
        tombstone("%30", "council:custodes"),  # designed redirect, indexed between
        pane("%29", "council:custodes"),  # second distinct live claim
    )
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_pane_in_snapshot(snapshot, "council:custodes")


def test_physical_id_still_resolves_when_labels_are_duplicated() -> None:
    """A raw %NN is unambiguous by construction; duplicate labels elsewhere must
    not block explicit physical addressing (the operator escape hatch)."""
    snapshot = council_workspace(
        pane("%30", "council:custodes"),
        pane("%28", "council:custodes"),
    )
    assert resolve_pane_in_snapshot(snapshot, "%28").pane_id == "%28"
    assert resolve_pane_in_snapshot(snapshot, "%30").pane_id == "%30"
