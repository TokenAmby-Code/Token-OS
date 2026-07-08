"""`/close-pane` releases the wrapper-ledger occupancy row.

Unlike the WrapperEnd path, a canonical `/close-pane` used to clear the pane runtime
and kill the pane but leave the ledger's OPEN occupancy row behind — so the next
`:new` allocation saw `ledger_occupied=true` and jammed until a `/reconcile` pruned
the stale row. close_pane now reads the pane's wrapper id BEFORE the runtime scrub and
closes the ledger row on a terminal close.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.close import _release_ledger_occupancy, close_pane  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_wrapper_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("TMUXCTLD_WRAPPER_LEDGER_PATH", str(tmp_path / "wrapper-ledger.json"))
    from tmuxctl import wrapper_ledger

    wrapper_ledger.LEDGER._rows = {}
    wrapper_ledger.LEDGER._loaded = False
    wrapper_ledger.LEDGER.load(force=True)
    yield
    wrapper_ledger.LEDGER._rows = {}
    wrapper_ledger.LEDGER._loaded = False


def test_release_ledger_occupancy_closes_open_row_on_terminal_close():
    from tmuxctl import wrapper_ledger

    wrapper_ledger.LEDGER.upsert(
        wrapper_id="wrap-close",
        pane_positional_id="mechanicus:2",
        instance_id="inst-x",
        state="OPEN",
    )
    result: dict = {"status": "closed"}
    _release_ledger_occupancy("wrap-close", result)

    assert result["ledger_released"] is True
    row = wrapper_ledger.LEDGER.resolve(wrapper_id="wrap-close", include_closed=True)
    assert row is not None
    assert row.state == "CLOSED"


def test_release_ledger_occupancy_noop_on_refused_close():
    from tmuxctl import wrapper_ledger

    wrapper_ledger.LEDGER.upsert(
        wrapper_id="wrap-keep",
        pane_positional_id="council:custodes",
        persona="custodes",
        state="OPEN",
    )
    result: dict = {"status": "refused"}
    _release_ledger_occupancy("wrap-keep", result)

    assert "ledger_released" not in result
    row = wrapper_ledger.LEDGER.resolve(wrapper_id="wrap-keep")
    assert row is not None
    assert row.state == "OPEN"


def test_release_ledger_occupancy_noop_without_wrapper_id():
    result: dict = {"status": "closed"}
    _release_ledger_occupancy("", result)
    assert "ledger_released" not in result


class LedgerCloseAdapter:
    """A worker pane that vanishes after the kill, carrying a wrapper-ownership id."""

    def __init__(self, wrapper_id: str) -> None:
        self.wrapper_id = wrapper_id
        self.exists_count = 1
        self.commands: list[tuple[str, ...]] = []

    def show_pane_option(self, pane_id: str, option: str) -> str:
        if option == "@PANE_ID":
            return "mechanicus:worker"
        if option in ("@TOKEN_API_WRAPPER_ID", "@TOKEN_API_WRAPPER_LAUNCH_ID"):
            return self.wrapper_id
        return ""

    def clear_runtime_state(self, target: str) -> None:
        self.commands.append(("clear_runtime_state", target))

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.commands.append(("send-keys", "-t", target, *keys))

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.commands.append(args)
        if args[0] == "display-message" and "-t" in args and args[-1] == "#{pane_id}":
            self.exists_count -= 1
            return "%9\n" if self.exists_count >= 0 else ""
        if (
            args[0] == "display-message"
            and args[-1] == "#{session_name}:#{window_index}\t#{window_name}"
        ):
            return "main:3\tmechanicus\n"
        if args[0] == "list-panes":
            return "%C\tcouncil:custodes\tcouncil\t0\t0\t0\t80\t40\tclaude\tfalse\n"
        return ""


def test_close_pane_releases_ledger_occupancy_integration():
    from tmuxctl import wrapper_ledger

    wrapper_ledger.LEDGER.upsert(
        wrapper_id="wrap-live",
        pane_positional_id="mechanicus:2",
        instance_id="inst-live",
        state="OPEN",
    )
    adapter = LedgerCloseAdapter("wrap-live")

    result = close_pane(adapter, "%9", timeout=0)

    assert result["status"] == "closed"
    assert result.get("ledger_released") is True
    row = wrapper_ledger.LEDGER.resolve(wrapper_id="wrap-live", include_closed=True)
    assert row is not None
    assert row.state == "CLOSED"
