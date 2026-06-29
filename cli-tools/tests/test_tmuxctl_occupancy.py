from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.occupancy import (
    assert_dispatch_target_available,
    looks_like_dispatch_launcher_payload,
    occupancy_for_pane,
)


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


class ResolvingOccupancyAdapter:
    def __init__(self, rows: dict[str, str], resolved: dict[str, str] | None = None):
        self.rows = rows
        self.resolved = resolved or {}
        self.targets: list[str] = []

    def _resolve_pane_target_arg(self, pane: str) -> str:
        if pane == "boom":
            raise ValueError("bad target")
        return self.resolved.get(pane, pane)

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[0] == "display-message":
            target = args[2]
            self.targets.append(target)
            return self.rows.get(target, "")
        raise AssertionError(args)


def test_occupancy_for_pane_resolves_logical_target_before_display(monkeypatch):
    monkeypatch.setattr("tmuxctl.occupancy._active_agent", lambda pane_pid: False)
    adapter = ResolvingOccupancyAdapter(
        {"%9": "%9\t1\t\tmechanicus:1\tmechanicus\t1000"},
        {"mechanicus:1": "%9"},
    )

    result = occupancy_for_pane(adapter, "mechanicus:1")

    assert result is not None
    assert result.pane_id == "%9"
    assert result.dispatch_available is True
    assert adapter.targets == ["%9"]


@pytest.mark.parametrize("row", ["", "%9\t1\ttoo-few"])
def test_occupancy_for_pane_returns_none_for_missing_or_malformed_display(row: str, monkeypatch):
    monkeypatch.setattr("tmuxctl.occupancy._active_agent", lambda pane_pid: False)
    adapter = ResolvingOccupancyAdapter({"%9": row})

    assert occupancy_for_pane(adapter, "%9") is None
    with pytest.raises(ValueError, match="pane target not found: %9"):
        assert_dispatch_target_available(adapter, "%9")


def test_occupancy_for_pane_falls_back_to_original_target_when_resolution_fails(
    monkeypatch,
):
    monkeypatch.setattr("tmuxctl.occupancy._active_agent", lambda pane_pid: False)
    adapter = ResolvingOccupancyAdapter({"boom": "%7\t1\t\tmechanicus:7\tmechanicus\t1007"})

    result = occupancy_for_pane(adapter, "boom")

    assert result is not None
    assert result.pane_id == "%7"
    assert adapter.targets == ["boom"]


def test_dispatch_target_guard_refuses_instance_stamp(monkeypatch):
    monkeypatch.setattr("tmuxctl.occupancy._active_agent", lambda pane_pid: False)
    adapter = ResolvingOccupancyAdapter(
        {"%9": "%9\t1\tlive-instance\tmechanicus:1\tmechanicus\t1000"}
    )

    with pytest.raises(ValueError, match="dispatch target is occupied: @INSTANCE_ID=live-instance"):
        assert_dispatch_target_available(adapter, "%9")


def test_dispatch_target_guard_refuses_live_agent(monkeypatch):
    monkeypatch.setattr("tmuxctl.occupancy._active_agent", lambda pane_pid: pane_pid == 1000)
    adapter = ResolvingOccupancyAdapter({"%9": "%9\t1\t\tmechanicus:1\tmechanicus\t1000"})

    with pytest.raises(
        ValueError, match="dispatch target has live Claude/Codex agent: pane_pid=1000"
    ):
        assert_dispatch_target_available(adapter, "%9")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("clear", True),
        ("  clear  ", True),
        ("python /tmp/dispatch-agent.abc.py", True),
        ("TOKEN_API_INTERNAL_DISPATCH=1 dispatch --pane %9", True),
        ("echo dispatch-agent", False),
        ("TOKEN_API_INTERNAL_DISPATCH=0 dispatch", False),
        ("", False),
    ],
)
def test_dispatch_launcher_payload_detection(payload: str, expected: bool):
    assert looks_like_dispatch_launcher_payload(payload) is expected
