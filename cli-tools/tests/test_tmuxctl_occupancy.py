from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.occupancy import assert_dispatch_target_available


class OccupancyAdapter:
    def __init__(self, row: str):
        self.row = row

    def _resolve_pane_target_arg(self, pane: str) -> str:
        return pane

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[0] == "display-message":
            return self.row
        raise AssertionError(args)


def test_dispatch_target_guard_refuses_singleton_with_empty_instance_stamp():
    # row fields: pane, clean, instance_id, pane_label, window, pane_pid
    adapter = OccupancyAdapter("%3\t1\t\tlegion:custodes\tlegion\t999")

    with pytest.raises(ValueError, match="protected singleton"):
        assert_dispatch_target_available(adapter, "%3")


def test_dispatch_target_guard_allows_genuinely_empty_worker():
    adapter = OccupancyAdapter("%9\t1\t\tmechanicus:1\tmechanicus\t1000")

    result = assert_dispatch_target_available(adapter, "%9")

    assert result.pane_id == "%9"
    assert result.pane_role == "mechanicus:1"
