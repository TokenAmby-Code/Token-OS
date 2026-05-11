from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.legion import LEGION_COLLAPSED_HEIGHT, focus_selected


class FakeLegionAdapter:
    def __init__(self, *, guard: bool = False) -> None:
        self.guard = guard
        self.commands: list[tuple[str, ...]] = []
        self.window_options: dict[str, str] = {}

    def show_window_option(self, target: str, option: str) -> str:
        if option == "@LEGION_FOCUS_GUARD":
            return "true" if self.guard else self.window_options.get(option, "false")
        return self.window_options.get(option, "")

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.commands.append(args)
        if args[0] == "display-message":
            target = args[args.index("-t") + 1] if "-t" in args else ""
            fmt = args[-1]
            if fmt == "#{session_name}\t#{window_index}\t#{window_name}":
                return "main\t3\tlegion\n"
            if fmt == "#{window_width}":
                return "200\n"
            if fmt == "#{window_height}":
                return "50\n"
            if fmt == "#{session_name}:#{window_index}":
                return "main:3\n"
            if fmt == "#{pane_id}":
                return f"{target}\n"
        if args[0] == "list-panes":
            return "\n".join(
                [
                    "%C\tlegion:custodes\t0\t0\t50",
                    "%1\tlegion:regiment\t0\t0\t3",
                    "%2\tlegion:regiment\t1\t4\t42",
                ]
            )
        if args[0] == "set-option" and "-w" in args:
            self.window_options[args[-2]] = args[-1]
        return ""


def test_selecting_custodes_does_not_resize_legion():
    adapter = FakeLegionAdapter()

    result = focus_selected(adapter, "%C")  # type: ignore[arg-type]

    assert result.endswith(": custodes")
    assert not any(command[0] == "resize-pane" for command in adapter.commands)


def test_selecting_regiment_expands_it_and_collapses_siblings_to_ribbons():
    adapter = FakeLegionAdapter()

    result = focus_selected(adapter, "%2")  # type: ignore[arg-type]

    assert result == "focused legion %2 in main:3"
    assert ("resize-pane", "-t", "%1", "-y", str(LEGION_COLLAPSED_HEIGHT)) in adapter.commands
    assert any(command[:3] == ("resize-pane", "-t", "%2") for command in adapter.commands)
    assert ("select-pane", "-t", "%2") in adapter.commands
    assert adapter.window_options["@LEGION_FOCUSED_PANE"] == "%2"
    assert adapter.window_options["@LEGION_FOCUS_GUARD"] == "false"


def test_legion_focus_guard_makes_hook_reentry_noop():
    adapter = FakeLegionAdapter(guard=True)

    result = focus_selected(adapter, "%2")  # type: ignore[arg-type]

    assert result.endswith(": guarded")
    assert not any(command[0] == "resize-pane" for command in adapter.commands)
