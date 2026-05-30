"""Part 2 of the dispatch pane-registry wedge fix: tmuxctl stack sweep self-heals
instance rows whose tmux_pane is an allocation token (e.g. ``mechanicus:new``)
instead of a concrete pane id. Correlation is by PID — the row's recorded pid
lives in the process subtree of exactly one live stack-worker pane.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import custodes as custmod
from tmuxctl import stack as stackmod
from tmuxctl.stack import _is_token_pane, reconcile_token_valued_panes


def test_is_token_pane_distinguishes_tokens_from_concrete_panes():
    assert _is_token_pane("mechanicus:new")
    assert _is_token_pane("legion:new")
    assert not _is_token_pane("%16")
    assert not _is_token_pane("%0")
    assert not _is_token_pane("")


def test_reconcile_rebinds_token_pane_by_pid_and_skips_the_rest():
    # %16's shell pid 100 owns the wrapper pid 4295 (a descendant).
    worker_pane_pids = {"%16": 100, "%20": 200}
    children_by_ppid = {100: [4295], 4295: [4296], 200: [9999]}
    rows = [
        # drifted + live + pid in %16's subtree -> rebind to %16
        {"id": "drift-1", "tmux_pane": "mechanicus:new", "status": "processing", "pid": 4295},
        # already concrete -> skip
        {"id": "healthy", "tmux_pane": "%20", "status": "processing", "pid": 9999},
        # token but stopped (not live) -> skip
        {"id": "dead", "tmux_pane": "mechanicus:new", "status": "stopped", "pid": 4296},
        # token + live but pid not in any pane subtree -> skip
        {"id": "orphan", "tmux_pane": "legion:new", "status": "processing", "pid": 7777},
    ]
    calls: list[tuple[str, str]] = []
    rebinds = reconcile_token_valued_panes(
        worker_pane_pids, rows, children_by_ppid, rebind=lambda i, p: calls.append((i, p))
    )

    assert rebinds == [("drift-1", "%16")]
    assert calls == [("drift-1", "%16")]


class _FakeAdapter:
    """Minimal adapter exposing one mechanicus stack-worker pane (%16, pid 100)."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, ...]] = []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.commands.append(args)
        if args[0] == "list-windows":
            return "4\tmechanicus\n"
        if args[0] == "list-panes":
            return "\n".join(
                [
                    "%F\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse",
                    "%16\tmechanicus:worker\tstack-worker\t0\t81\t0\t80\t42\tclaude\tfalse",
                ]
            )
        if args[0] == "display-message":
            fmt = args[-1]
            target = args[args.index("-t") + 1] if "-t" in args else ""
            if fmt == "#{pane_pid}":
                return "100\n" if target == "%16" else "0\n"
            if fmt == "#{pane_id}":
                return f"{target}\n"
        return ""

    def send_keys(self, *args: str, allow_failure: bool = False) -> None:
        self.run("send-keys", *args, allow_failure=allow_failure)

    def show_window_option(self, target: str, option: str) -> str:
        return ""


def test_sweep_reconcile_rebinds_live_drifted_stack_worker(monkeypatch):
    monkeypatch.setattr(custmod, "_process_tree", lambda: ({100: [4295]}, {4295: "claude"}))
    monkeypatch.setattr(
        stackmod,
        "fetch_instance_rows_raw",
        lambda: [
            {"id": "drift-1", "tmux_pane": "mechanicus:new", "status": "processing", "pid": 4295},
            {"id": "healthy", "tmux_pane": "%9", "status": "processing", "pid": 555},
        ],
    )
    rebinds: list[tuple[str, str]] = []
    monkeypatch.setattr(stackmod, "rebind_instance_pane", lambda i, p: rebinds.append((i, p)))

    result = stackmod.reconcile_stack_pane_registry(_FakeAdapter(), "main")

    assert ("drift-1", "%16") in result
    assert rebinds == [("drift-1", "%16")]


def test_sweep_reconcile_noops_when_no_stack_workers(monkeypatch):
    class _Empty(_FakeAdapter):
        def run(self, *args, allow_failure=False):
            if args[0] == "list-windows":
                return "4\tmechanicus\n"
            if args[0] == "list-panes":
                return (
                    "%F\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse"
                )
            return ""

    # If the registry were consulted this would raise; assert it is not reached.
    def _boom():
        raise AssertionError("registry must not be fetched when no stack workers exist")

    monkeypatch.setattr(stackmod, "fetch_instance_rows_raw", _boom)
    assert stackmod.reconcile_stack_pane_registry(_Empty(), "main") == []
