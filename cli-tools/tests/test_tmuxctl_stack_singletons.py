from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl._stack_core import STACK_PAGE_SPECS, StackPane, _orchestrator_and_workers


def _pane(pane_id: str, role: str, command: str = "claude") -> StackPane:
    return StackPane(pane_id, role, "", False, 0, 0, 80, 25, command)


def test_stack_worker_selection_excludes_displaced_singleton_labels():
    spec = STACK_PAGE_SPECS["mechanicus"]
    orchestrator, workers = _orchestrator_and_workers(
        [
            _pane("%fg", "mechanicus:fabricator-general"),
            _pane("%custodes", "legion:custodes"),
            _pane("%admin", "mechanicus:admin"),
            _pane("%worker", "mechanicus:1"),
        ],
        spec,
    )

    assert orchestrator and orchestrator.pane_id == "%fg"
    assert [p.pane_id for p in workers] == ["%worker"]
