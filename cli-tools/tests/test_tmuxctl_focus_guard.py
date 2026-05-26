from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.focus_guard import preserve_focus


class FakeFocusAdapter:
    def __init__(self) -> None:
        self.current_window = "main:1"
        self.current_pane = "%1"
        self.pane_window = {"%1": "main:1", "%2": "main:2"}
        self.commands: list[tuple[str, ...]] = []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.commands.append(args)
        if args[0] == "display-message":
            target = args[args.index("-t") + 1] if "-t" in args else ""
            fmt = args[-1]
            if fmt == "#{session_name}:#{window_index}\t#{pane_id}":
                if target:
                    return f"{self.pane_window.get(target, '')}\t{target}\n" if target in self.pane_window else ""
                return f"{self.current_window}\t{self.current_pane}\n"
            if fmt == "#{pane_id}":
                if target:
                    return f"{target}\n" if target in self.pane_window else ""
                return f"{self.current_pane}\n"
            if fmt == "#{session_name}:#{window_index}":
                return self.pane_window.get(target, self.current_window) + "\n"
        if args[0] == "select-window":
            self.current_window = args[args.index("-t") + 1]
        if args[0] == "select-pane":
            pane = args[args.index("-t") + 1]
            self.current_pane = pane
            self.current_window = self.pane_window.get(pane, self.current_window)
        return ""


def test_preserve_focus_restores_window_and_pane_after_automation_snap():
    adapter = FakeFocusAdapter()

    with preserve_focus(adapter, source="test", attempted_target="%2"):
        adapter.run("select-window", "-t", "main:2")
        adapter.run("select-pane", "-t", "%2")

    assert adapter.current_window == "main:1"
    assert adapter.current_pane == "%1"
    assert ("select-window", "-t", "main:1") in adapter.commands
    assert ("select-pane", "-t", "%1") in adapter.commands
