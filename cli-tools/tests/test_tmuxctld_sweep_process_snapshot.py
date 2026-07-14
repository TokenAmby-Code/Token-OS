#!/usr/bin/env python3
"""Regression: persona/reservist sweeps and stamped-pane liveness walks must
take ONE process snapshot per pass instead of forking `ps -A` per pane."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tmuxctld" / "lib"))

from tmuxctl import assertions, liveness  # noqa: E402

SENTINEL_TREE = ({100: [101]}, {101: "claude"})


def test_sweep_persona_panes_single_snapshot() -> None:
    snapshots = 0
    received: list[object] = []

    def fake_snapshot() -> tuple[dict[int, list[int]], dict[int, str]]:
        nonlocal snapshots
        snapshots += 1
        return SENTINEL_TREE

    def fake_assert_instance(adapter, target, **kwargs):  # noqa: ANN001, ANN003, ARG001
        received.append(kwargs.get("process_tree"))
        return {"ok": True, "pane_label": target, "action": "none", "reason": "live"}

    original_snapshot = assertions.process_tree_snapshot
    original_assert = assertions.assert_instance
    original_panes = assertions._sweep_pane_snapshot
    try:
        assertions.process_tree_snapshot = fake_snapshot
        assertions._sweep_pane_snapshot = lambda adapter, session=None: {}
        assertions.assert_instance = fake_assert_instance
        results = assertions.sweep_persona_panes(object(), session="main")
    finally:
        assertions.process_tree_snapshot = original_snapshot
        assertions._sweep_pane_snapshot = original_panes
        assertions.assert_instance = original_assert

    assert snapshots == 1, f"expected one process snapshot, got {snapshots}"
    assert len(results) == len(assertions.PERSONA_LABELS)
    assert all(tree is SENTINEL_TREE for tree in received), received


def test_sweep_reservist_panes_single_snapshot() -> None:
    snapshots = 0
    received: list[object] = []

    def fake_snapshot() -> tuple[dict[int, list[int]], dict[int, str]]:
        nonlocal snapshots
        snapshots += 1
        return SENTINEL_TREE

    def fake_assert_reservist(adapter, target, **kwargs):  # noqa: ANN001, ANN003, ARG001
        received.append(kwargs.get("process_tree"))
        return {"ok": True, "pane_label": target, "action": "none", "reason": "live"}

    original_snapshot = assertions.process_tree_snapshot
    original_assert = assertions.assert_reservist_seat
    original_panes = assertions._sweep_pane_snapshot
    try:
        assertions.process_tree_snapshot = fake_snapshot
        assertions._sweep_pane_snapshot = lambda adapter, session=None: {}
        assertions.assert_reservist_seat = fake_assert_reservist
        results = assertions.sweep_reservist_panes(object(), session="main")
    finally:
        assertions.process_tree_snapshot = original_snapshot
        assertions._sweep_pane_snapshot = original_panes
        assertions.assert_reservist_seat = original_assert

    assert snapshots == 1, f"expected one process snapshot, got {snapshots}"
    assert len(results) == len(assertions.RESERVIST_LABELS)
    assert all(tree is SENTINEL_TREE for tree in received), received


class StampedPanesAdapter:
    """Resolved pane %1 has no agent; stamped panes %2/%3 walk the same snapshot."""

    def run(self, *args: str, allow_failure: bool = False) -> str:  # noqa: ARG002
        if args[0] == "display-message":
            pane = args[2]
            return {"%1": "100", "%2": "200", "%3": "300"}.get(pane, "")
        if args[0] == "list-panes":
            return "\n".join(
                [
                    "%2\tinstance-x\t200",
                    "%3\tinstance-x\t300",
                ]
            )
        raise AssertionError(f"unexpected tmux call: {args!r}")


def test_instance_live_tui_single_snapshot() -> None:
    snapshots = 0

    def fake_snapshot() -> tuple[dict[int, list[int]], dict[int, str]]:
        nonlocal snapshots
        snapshots += 1
        return ({300: [301]}, {301: "codex"})

    original_snapshot = liveness.process_tree_snapshot
    try:
        liveness.process_tree_snapshot = fake_snapshot
        tui = liveness.instance_live_tui(StampedPanesAdapter(), "instance-x", "%1")
    finally:
        liveness.process_tree_snapshot = original_snapshot

    assert snapshots == 1, f"expected one process snapshot, got {snapshots}"
    assert tui is not None and tui.pane_id == "%3" and tui.agent_pid == 301


def test_runtime_has_instance_uses_injected_tree() -> None:
    class Adapter:
        def run(self, *args: str, allow_failure: bool = False) -> str:  # noqa: ARG002
            return "100"

    live = assertions._runtime_has_instance(Adapter(), "%9", process_tree=SENTINEL_TREE)
    assert live is True


def test_live_sweeps_fast_path_avoid_per_label_asserts() -> None:
    class Registry:
        instances: list[object] = []

    panes = {
        label: {
            "session": "main",
            "pane": f"%{idx}",
            "dead": "0",
            "pid": str(1000 + idx),
            "pane_label": label,
            "pane_type": label.split(":", 1)[0],
            "instance_id": "",
            "persona": "Bound",
        }
        for idx, label in enumerate(sorted(assertions.PERSONA_LABELS), 1)
    }
    for idx, label in enumerate(assertions.RESERVIST_LABELS, 100):
        panes[label] = {
            "session": "main",
            "pane": f"%{idx}",
            "dead": "0",
            "pid": str(1000 + idx),
            "pane_label": label,
            "pane_type": "reservists",
            "instance_id": "",
            "persona": "",
        }
    tree = (
        {int(p["pid"]): [int(p["pid"]) + 10000] for p in panes.values()},
        {int(p["pid"]) + 10000: "claude" for p in panes.values()},
    )

    def fail_assert(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("fast path should not call per-label assertion")

    original_snapshot = assertions.process_tree_snapshot
    original_panes = assertions._sweep_pane_snapshot
    original_registry = assertions.fetch_instance_registry
    original_assert_instance = assertions.assert_instance
    original_assert_reservist = assertions.assert_reservist_seat
    try:
        assertions.process_tree_snapshot = lambda: tree
        assertions._sweep_pane_snapshot = lambda adapter, session=None: panes
        assertions.fetch_instance_registry = lambda: Registry()
        assertions.assert_instance = fail_assert
        assertions.assert_reservist_seat = fail_assert
        persona_results = assertions.sweep_persona_panes(object(), session="main")
        reservist_results = assertions.sweep_reservist_panes(object(), session="main")
    finally:
        assertions.process_tree_snapshot = original_snapshot
        assertions._sweep_pane_snapshot = original_panes
        assertions.fetch_instance_registry = original_registry
        assertions.assert_instance = original_assert_instance
        assertions.assert_reservist_seat = original_assert_reservist

    assert len(persona_results) == len(assertions.PERSONA_LABELS)
    assert all(r["ok"] and r["reason"] == "live_registry_skipped" for r in persona_results)
    assert len(reservist_results) == len(assertions.RESERVIST_LABELS)
    assert all(r["ok"] and r["reason"] == "live" for r in reservist_results)


def main() -> None:
    test_sweep_persona_panes_single_snapshot()
    test_sweep_reservist_panes_single_snapshot()
    test_instance_live_tui_single_snapshot()
    test_runtime_has_instance_uses_injected_tree()
    test_live_sweeps_fast_path_avoid_per_label_asserts()


if __name__ == "__main__":
    main()
