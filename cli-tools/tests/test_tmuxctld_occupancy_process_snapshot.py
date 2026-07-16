#!/usr/bin/env python3
"""Regression: multi-pane occupancy scans must not run ps once per pane."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tmuxctld" / "lib"))

from tmuxctl import occupancy  # noqa: E402


class FakeAdapter:
    def run(self, *args: str, allow_failure: bool = False) -> str:  # noqa: ARG002
        assert args[:3] == ("list-panes", "-a", "-F")
        return "\n".join(
            [
                "%1\tworker:1\tmechanicus\t100\t1000.0",
                "%2\tworker:2\tmechanicus\t200\t1000.0",
                "%3\tworker:3\tmechanicus\t300\t1000.0",
            ]
        )


class PaneOccupancyAdapter:
    def run(self, *args: str, allow_failure: bool = False) -> str:  # noqa: ARG002
        assert args[:3] == ("list-panes", "-a", "-F")
        return "%1\tinstance-1\tworker:1\tmechanicus\t100\t1000.0"


def test_scan_ledger_dispatch_availability_single_snapshot() -> None:
    calls = 0

    def fake_process_tree() -> tuple[dict[int, list[int]], dict[int, str]]:
        nonlocal calls
        calls += 1
        return ({100: [101], 200: [201], 300: []}, {101: "claude", 201: "codex"})

    original_snapshot = occupancy._process_tree_snapshot
    original_wrapper = occupancy._active_wrapper_row_for_role
    try:
        occupancy._process_tree_snapshot = fake_process_tree
        occupancy._active_wrapper_row_for_role = lambda _role: None
        rows = occupancy.scan_ledger_dispatch_availability(FakeAdapter())
    finally:
        occupancy._process_tree_snapshot = original_snapshot
        occupancy._active_wrapper_row_for_role = original_wrapper

    assert calls == 1, f"expected one process snapshot, got {calls}"
    assert [row.live_agent for row in rows] == [True, True, False]


class FaultIsolationAdapter:
    """Three panes; ``council:pax`` is the swapped/faulting seat."""

    def run(self, *args: str, allow_failure: bool = False) -> str:  # noqa: ARG002
        assert args[:3] == ("list-panes", "-a", "-F")
        return "\n".join(
            [
                "%1\tworker:1\tmechanicus\t100\t1000.0",
                "%2\tcouncil:pax\tcouncil\t200\t1000.0",
                "%3\tworker:3\tmechanicus\t300\t1000.0",
            ]
        )


def test_scan_ledger_dispatch_availability_isolates_faulting_seat() -> None:
    """One seat whose wrapper-ledger lookup raises must NOT fail the whole scan.

    Regression pin for the council:pax freelist blackout: the Emperor swapped the
    seat to codex, its occupancy lookup raised, and the single bad row took the
    entire pool scan (and all dispatch slot resolution) down. Per-pane fault
    isolation must mark THAT seat faulted+excluded and keep the rest visible.
    """

    def fake_process_tree() -> tuple[dict[int, list[int]], dict[int, str]]:
        return ({100: [], 200: [], 300: []}, {})

    def raising_wrapper(role: str):
        if role == "council:pax":
            raise ValueError("wrapper ledger occupancy lookup failed for council:pax")
        return None

    original_snapshot = occupancy._process_tree_snapshot
    original_wrapper = occupancy._active_wrapper_row_for_role
    try:
        occupancy._process_tree_snapshot = fake_process_tree
        occupancy._active_wrapper_row_for_role = raising_wrapper
        rows = occupancy.scan_ledger_dispatch_availability(FaultIsolationAdapter())
    finally:
        occupancy._process_tree_snapshot = original_snapshot
        occupancy._active_wrapper_row_for_role = original_wrapper

    by_role = {row.pane_role: row for row in rows}
    # The whole pool is still visible — the scan did not raise.
    assert set(by_role) == {"worker:1", "council:pax", "worker:3"}
    # The faulting seat is flagged, excluded from dispatch, and names its fault.
    pax = by_role["council:pax"]
    assert pax.faulted is True
    assert pax.dispatch_available is False
    assert "council:pax" in pax.fault_reason
    # Its neighbours remain dispatch-available.
    assert by_role["worker:1"].faulted is False
    assert by_role["worker:1"].dispatch_available is True
    assert by_role["worker:3"].dispatch_available is True


def test_scan_pane_occupancy_single_snapshot() -> None:
    calls = 0

    def fake_process_tree() -> tuple[dict[int, list[int]], dict[int, str]]:
        nonlocal calls
        calls += 1
        return ({100: [101]}, {101: "claude"})

    original_snapshot = occupancy._process_tree_snapshot
    try:
        occupancy._process_tree_snapshot = fake_process_tree
        rows = occupancy.scan_pane_occupancy(PaneOccupancyAdapter())
    finally:
        occupancy._process_tree_snapshot = original_snapshot

    assert calls == 1, f"expected one process snapshot, got {calls}"
    assert [row.live_agent for row in rows] == [True]


def main() -> None:
    test_scan_ledger_dispatch_availability_single_snapshot()
    test_scan_ledger_dispatch_availability_isolates_faulting_seat()
    test_scan_pane_occupancy_single_snapshot()


if __name__ == "__main__":
    main()
