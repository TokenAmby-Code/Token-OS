from __future__ import annotations

import pathlib
import sys
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import time

from tmuxctl import assertions
from tmuxctl.assertions import (
    assert_instance,
)


class FakeAdapter:
    """Records tmux calls and stores pane options, mirroring the persona suite."""

    def __init__(self) -> None:
        self.options: dict[str, str] = {}
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args, allow_failure: bool = False) -> str:
        self.calls.append(args)
        if args and args[0] == "set-option":
            if "-pu" in args:
                self.options.pop(args[-1], None)
            else:
                self.options[args[-2]] = args[-1]
        if args and args[0] == "display-message":
            return ""
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self.options.get(option, "")


def _worker_row(created_at: str, **kw):
    base = dict(
        instance_id="i-worker",
        pane_label="legion:1",
        created_at=created_at,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _run_assert(adapter, *, runtime_ok, rows):
    resolved = SimpleNamespace(pane_id="%W", pane_role="")
    with (
        patch.object(assertions, "resolve_pane", return_value=resolved),
        patch.object(assertions, "_pane_type", return_value="stack-worker"),
        patch.object(assertions, "_runtime_has_instance", return_value=runtime_ok),
        patch.object(assertions, "_registry_entries", return_value=rows),
        patch.object(assertions, "_stop_rows") as stop_rows,
        patch.object(assertions, "log_event"),
    ):
        result = assert_instance(adapter, "%W")
    return result, stop_rows


def test_within_grace_row_not_pruned():
    adapter = FakeAdapter()
    rows = [_worker_row(datetime.now().isoformat())]
    result, stop_rows = _run_assert(adapter, runtime_ok=False, rows=rows)

    assert result["action"] == "boot_grace"
    assert result["reason"] == "stack_worker_boot_grace"
    assert not any(c and c[0] == "kill-pane" for c in adapter.calls)
    stop_rows.assert_not_called()


def test_past_grace_row_is_pruned():
    adapter = FakeAdapter()
    stale = datetime.fromtimestamp(time.time() - 60).isoformat()
    rows = [_worker_row(stale)]
    result, stop_rows = _run_assert(adapter, runtime_ok=False, rows=rows)

    assert result["action"] == "pruned"
    assert any(c and c[0] == "kill-pane" for c in adapter.calls)
    stop_rows.assert_called_once()


def test_no_row_fresh_pane_not_pruned():
    adapter = FakeAdapter()
    adapter.options["@PANE_BORN"] = str(int(time.time()))
    result, stop_rows = _run_assert(adapter, runtime_ok=False, rows=[])

    assert result["action"] == "boot_grace"
    assert not any(c and c[0] == "kill-pane" for c in adapter.calls)
    stop_rows.assert_not_called()


def test_no_row_no_birth_stamp_still_pruned():
    adapter = FakeAdapter()
    result, stop_rows = _run_assert(adapter, runtime_ok=False, rows=[])

    assert result["action"] == "pruned"
    assert any(c and c[0] == "kill-pane" for c in adapter.calls)


def test_healthy_row_is_noop():
    adapter = FakeAdapter()
    rows = [_worker_row(datetime.now().isoformat())]
    result, _ = _run_assert(adapter, runtime_ok=True, rows=rows)

    assert result["ok"] is True
    assert result["reason"] == "live"
    assert not any(c and c[0] == "kill-pane" for c in adapter.calls)


def test_unparseable_row_does_not_extend_grace():
    # A legacy row with no/garbage created_at must not hold the grace open.
    adapter = FakeAdapter()
    rows = [_worker_row("")]
    result, _ = _run_assert(adapter, runtime_ok=False, rows=rows)

    assert result["action"] == "pruned"


def test_build_snapshot_carries_created_at():
    from tmuxctl.registry import build_registry_snapshot

    snap = build_registry_snapshot(
        device_id="Mac-Mini",
        instances=[
            {
                "id": "i-c",
                "device_id": "Mac-Mini",
                "pane_label": "legion:1",
                "status": "working",
                "created_at": "2026-06-15T12:00:00",
            }
        ],
    )
    assert snap.instances[0].created_at == "2026-06-15T12:00:00"


def test_tag_worker_stamps_pane_born():
    from tmuxctl.stack import _tag_worker

    adapter = FakeAdapter()
    _tag_worker(adapter, "%W", "legion")

    assert "@PANE_BORN" in adapter.options
    assert int(adapter.options["@PANE_BORN"]) > 0
