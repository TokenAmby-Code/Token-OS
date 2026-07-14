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


def main() -> None:
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


if __name__ == "__main__":
    main()
