"""Wrapper-ledger cold-state must fail closed, never serve stale file rows.

Cluster A P0 co-hypothesis (confirmed in code): the daemon boot sequence loads
the write-behind ledger JSON (pre-restart rows) and then reconciles from live
tmux; if that reconcile throws, the exception is swallowed and ledger-first
resolution (``/resolve-pane`` → ``ledger_resolve``) serves STALE rows. The
2026-07-07 custodes→malcador misroute followed a daemon cold-start by ~63s.

Contract under test:
  * a ledger is ``warmed`` only after a successful ``reconcile_from_tmux``;
  * a cold ledger refuses semantic resolution service (found:false, flagged);
  * duplicate active ``pane_positional_id`` rows are ambiguous → loud.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.wrapper_ledger import WrapperLedger

_SCAN_SEP = "__TMUXCTLD_WRAPPER_LEDGER_FIELD__"


def _write_ledger_file(path: pathlib.Path, rows: list[dict]) -> None:
    path.write_text(json.dumps({"version": 1, "rows": rows}), encoding="utf-8")


def _stale_custodes_row(pane: str = "council:custodes") -> dict:
    return {
        "wrapper_id": "w-stale-custodes",
        "instance_id": "4cad6036-5ac8-4808-9a48-92af86a0dfa7",
        "persona": "custodes",
        "pane_positional_id": pane,
        "engine": "claude",
        "working_dir": "/Volumes/Imperium/Imperium-ENV",
        "born_epoch": 1000.0,
        "state": "OPEN",
    }


class ScanOkAdapter:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def run(self, *args: str, allow_failure: bool = False) -> str:
        return "\n".join(self.lines)


class ScanFailAdapter:
    def run(self, *args: str, allow_failure: bool = False) -> str:
        raise RuntimeError("tmux unavailable during daemon cold start")


def _scan_line(wrapper_id: str, instance_id: str, persona: str, pane: str) -> str:
    return _SCAN_SEP.join(
        [wrapper_id, "", instance_id, persona, pane, "claude", "/tmp", "1000", "0"]
    )


def test_file_loaded_ledger_is_not_warmed(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "ledger.json"
    _write_ledger_file(path, [_stale_custodes_row()])
    ledger = WrapperLedger(path)
    ledger.load()
    assert ledger.warmed is False


def test_successful_tmux_reconcile_warms_the_ledger(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "ledger.json"
    _write_ledger_file(path, [_stale_custodes_row()])
    ledger = WrapperLedger(path)
    ledger.load()
    ledger.reconcile_from_tmux(
        ScanOkAdapter([_scan_line("w-live", "b39464f0", "custodes", "council:custodes")])
    )
    assert ledger.warmed is True


def test_failed_boot_reconcile_leaves_ledger_cold(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "ledger.json"
    _write_ledger_file(path, [_stale_custodes_row()])
    ledger = WrapperLedger(path)
    ledger.load()
    with pytest.raises(RuntimeError):
        ledger.reconcile_from_tmux(ScanFailAdapter())
    assert ledger.warmed is False


def test_cold_ledger_refuses_to_serve_stale_semantic_rows(tmp_path: pathlib.Path) -> None:
    """The misroute red: pre-restart custodes row must not be resolution truth
    while the ledger has never been reconciled against live tmux this boot."""
    path = tmp_path / "ledger.json"
    _write_ledger_file(path, [_stale_custodes_row()])
    ledger = WrapperLedger(path)
    ledger.load()
    assert ledger.resolve("council:custodes") is None
    assert ledger.resolve(pane_positional_id="council:custodes") is None


def test_warmed_ledger_serves_the_reconciled_row(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "ledger.json"
    _write_ledger_file(path, [_stale_custodes_row()])
    ledger = WrapperLedger(path)
    ledger.load()
    ledger.reconcile_from_tmux(
        ScanOkAdapter([_scan_line("w-live", "b39464f0", "custodes", "council:custodes")])
    )
    row = ledger.resolve(pane_positional_id="council:custodes")
    assert row is not None
    assert row.wrapper_id == "w-live"
    assert row.instance_id == "b39464f0"


def test_duplicate_active_pane_labels_resolve_loud_not_last_writer(
    tmp_path: pathlib.Path,
) -> None:
    """Two active rows claiming council:custodes is the ambiguity class; the
    reverse index silently kept the last writer. Must raise instead."""
    path = tmp_path / "ledger.json"
    ledger = WrapperLedger(path)
    ledger.load()
    ledger.reconcile_from_tmux(
        ScanOkAdapter(
            [
                _scan_line("w-old", "4cad6036", "custodes", "council:custodes"),
                _scan_line("w-new", "b39464f0", "custodes", "council:custodes"),
            ]
        )
    )
    with pytest.raises(ValueError, match="ambiguous"):
        ledger.resolve(pane_positional_id="council:custodes")
    with pytest.raises(ValueError, match="ambiguous"):
        ledger.resolve("council:custodes")
    # Unambiguous keys on the same ledger still resolve.
    row = ledger.resolve(wrapper_id="w-new")
    assert row is not None and row.instance_id == "b39464f0"
