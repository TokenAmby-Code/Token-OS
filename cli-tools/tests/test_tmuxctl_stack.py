from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.stack import STACK_COLLAPSED_HEIGHT, enforce_stack_layout, focus_selected
from tmuxctl.stack import dispatch_stack_command


class FakeLegionAdapter:
    def __init__(
        self, *, guard: bool = False, window_name: str = "legion", rows: list[str] | None = None
    ) -> None:
        self.guard = guard
        self.window_name = window_name
        self.rows = rows
        self.commands: list[tuple[str, ...]] = []
        self.window_options: dict[str, str] = {}

    def show_window_option(self, target: str, option: str) -> str:
        if option == "@STACK_FOCUS_GUARD":
            return "true" if self.guard else self.window_options.get(option, "false")
        return self.window_options.get(option, "")

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        # Mirror TmuxAdapter.send_keys so the universal-gate routing is exercised.
        self.run("send-keys", "-t", target, *keys, allow_failure=allow_failure)

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.commands.append(args)
        if args[0] == "display-message":
            target = args[args.index("-t") + 1] if "-t" in args else ""
            fmt = args[-1]
            if fmt == "#{session_name}\t#{window_index}\t#{window_name}":
                return f"main\t3\t{self.window_name}\n"
            if fmt == "#{window_width}":
                return "200\n"
            if fmt == "#{window_height}":
                return "50\n"
            if fmt == "#{window_name}":
                return f"{self.window_name}\n"
            if fmt == "#{session_name}:#{window_index}":
                return "main:3\n"
            if fmt == "#{session_name}:#{window_name}":
                return f"main:{self.window_name}\n"
            if fmt == "#{pane_id}":
                return f"{target}\n"
        if args[0] == "list-windows":
            return f"{self.window_name}\n"
        if args[0] == "list-panes":
            if self.rows is not None:
                return "\n".join(self.rows)
            return "\n".join(
                [
                    "%C\tlegion:custodes\t0\t0\t50",
                    "%1\tlegion:regiment\t0\t0\t3",
                    "%2\tlegion:regiment\t1\t4\t42",
                ]
            )
        if args[0] == "set-option" and "-w" in args:
            self.window_options[args[-2]] = args[-1]
        if args[0] == "set-option" and "-p" in args and self.rows is not None:
            target = args[args.index("-t") + 1]
            option = args[-2]
            value = args[-1]
            updated = []
            for row in self.rows:
                parts = row.split("\t")
                if parts[0] == target:
                    if option == "@PANE_ID":
                        parts[1] = value
                    elif option == "@PANE_TYPE":
                        parts[2] = value
                    elif option == "@GRID_STATE":
                        pass
                    elif option == "@STACK_PENDING":
                        if len(parts) == 9:
                            parts.append(value)
                        else:
                            parts[9] = value
                    row = "\t".join(parts)
                updated.append(row)
            self.rows = updated
        if args[0] == "split-window" and self.rows is not None:
            pane = "%N"
            self.rows.append(f"{pane}\t\t\t0\t80\t0\t80\t10\tzsh\tfalse")
            return f"{pane}\n"
        if args[0] == "kill-pane" and self.rows is not None:
            pane = args[args.index("-t") + 1]
            self.rows = [row for row in self.rows if not row.startswith(f"{pane}\t")]
        return ""


def test_selecting_custodes_does_not_resize_legion():
    adapter = FakeLegionAdapter()

    result = focus_selected(adapter, "%C")  # type: ignore[arg-type]

    assert result.endswith(": custodes")
    assert not any(command[0] == "resize-pane" for command in adapter.commands)


def test_selecting_regiment_expands_it_and_collapses_siblings_to_ribbons():
    adapter = FakeLegionAdapter()

    result = focus_selected(adapter, "%2")  # type: ignore[arg-type]

    assert result == "focused stack %2 in main:3"
    assert ("resize-pane", "-t", "%1", "-y", str(STACK_COLLAPSED_HEIGHT)) in adapter.commands
    assert any(command[:3] == ("resize-pane", "-t", "%2") for command in adapter.commands)
    assert adapter.window_options["@STACK_FOCUSED_PANE"] == "%2"
    assert adapter.window_options["@STACK_FOCUS_GUARD"] == "false"


def test_selecting_custodes_preserves_last_focused_regiment_slot():
    adapter = FakeLegionAdapter()
    adapter.window_options["@STACK_FOCUSED_PANE"] = "%2"

    result = enforce_stack_layout(adapter, "main:3", focused_pane="%C", focus=True)  # type: ignore[arg-type]

    assert result == "noop stack focus %C: persona pane"
    assert not any(command[0] == "resize-pane" for command in adapter.commands)
    assert adapter.window_options["@STACK_FOCUSED_PANE"] == "%2"
    assert not any(command[0] == "select-pane" for command in adapter.commands)


def test_normalize_uses_stored_focus_without_reselecting_or_clearing_it():
    adapter = FakeLegionAdapter()
    adapter.window_options["@STACK_FOCUSED_PANE"] = "%1"

    result = enforce_stack_layout(adapter, "main:3")  # type: ignore[arg-type]

    assert result == "normalized stack layout main:3"
    assert any(command[:3] == ("resize-pane", "-t", "%1") for command in adapter.commands)
    assert ("resize-pane", "-t", "%2", "-y", str(STACK_COLLAPSED_HEIGHT)) in adapter.commands
    assert adapter.window_options["@STACK_FOCUSED_PANE"] == "%1"
    assert not any(command[0] == "select-pane" for command in adapter.commands)


def test_legion_focus_guard_makes_hook_reentry_noop():
    adapter = FakeLegionAdapter(guard=True)

    result = focus_selected(adapter, "%2")  # type: ignore[arg-type]

    assert result.endswith(": guarded")
    assert not any(command[0] == "resize-pane" for command in adapter.commands)


def test_clear_legion_worker_is_killed_instead_of_becoming_blank_stack_pane():
    adapter = FakeLegionAdapter(
        rows=[
            "%C\tlegion:custodes\tlegion\t0\t0\t0\t80\t50\tclaude",
            "%1\tlegion:worker\tstack-worker\t1\t81\t0\t80\t10\tzsh",
        ]
    )

    result = enforce_stack_layout(adapter, "main:3")  # type: ignore[arg-type]

    assert result == "normalized stack layout main:3: orchestrator only"
    assert ("kill-pane", "-t", "%1") in adapter.commands


def test_mechanicus_stack_uses_fabricator_left_column_and_worker_right_stack():
    adapter = FakeLegionAdapter(
        window_name="mechanicus",
        rows=[
            "%F\tmechanicus:fabricator-general\tmechanicus\t0\t0\t0\t80\t50\tclaude",
            "%1\tmechanicus:worker\tstack-worker\t1\t81\t0\t80\t42\tcodex",
        ],
    )

    result = enforce_stack_layout(adapter, "main:4", focused_pane="%1", focus=True)  # type: ignore[arg-type]

    assert result == "focused stack %1 in main:4"
    assert ("set-window-option", "-t", "main:4", "main-pane-width", "80") in adapter.commands
    assert ("resize-pane", "-t", "%F", "-x", "80") in adapter.commands
    assert adapter.window_options["@STACK_FOCUSED_PANE"] == "%1"


def test_blank_orchestrator_tag_is_moved_to_single_untyped_live_pane():
    adapter = FakeLegionAdapter(
        rows=[
            "%blank\tlegion:custodes\tlegion\t0\t0\t0\t80\t20\tzsh",
            "%live\t\t\t1\t0\t0\t160\t50\tclaude",
        ]
    )

    result = enforce_stack_layout(adapter, "main:3")  # type: ignore[arg-type]

    assert result == "normalized stack layout main:3: orchestrator only"
    assert ("set-option", "-p", "-t", "%live", "@PANE_ID", "legion:custodes") in adapter.commands
    assert ("kill-pane", "-t", "%blank") in adapter.commands


def test_stack_dispatch_creates_managed_worker_and_launches_command():
    adapter = FakeLegionAdapter(
        rows=[
            "%C\tlegion:custodes\tlegion\t1\t0\t0\t80\t50\tclaude\tfalse",
        ]
    )

    pane = dispatch_stack_command(  # type: ignore[arg-type]
        adapter,
        "main",
        "legion",
        "echo hello",
        cwd="/tmp",
        settle_seconds=0,
    )

    assert pane == "%N"
    assert ("set-option", "-p", "-t", "%N", "@PANE_ID", "legion:1") in adapter.commands
    assert ("set-option", "-p", "-t", "%N", "@PANE_TYPE", "stack-worker") in adapter.commands
    assert ("send-keys", "-t", "%N", "echo hello", "Enter") in adapter.commands


def test_stack_enforce_preserves_existing_numeric_worker_ids_with_gap():
    adapter = FakeLegionAdapter(
        rows=[
            "%C\tlegion:custodes\tlegion\t0\t0\t0\t80\t50\tclaude\tfalse",
            "%1\tlegion:1\tstack-worker\t0\t81\t0\t80\t10\tclaude\tfalse",
            "%5\tlegion:5\tstack-worker\t1\t81\t11\t80\t39\tclaude\tfalse",
        ]
    )

    enforce_stack_layout(adapter, "main:3")  # type: ignore[arg-type]

    assert ("set-option", "-p", "-t", "%5", "@PANE_ID", "legion:2") not in adapter.commands
    assert any(row.startswith("%5\tlegion:5\t") for row in adapter.rows or [])


def test_stack_dispatch_reuses_lowest_available_worker_id():
    adapter = FakeLegionAdapter(
        rows=[
            "%C\tlegion:custodes\tlegion\t1\t0\t0\t80\t50\tclaude\tfalse",
            "%2\tlegion:2\tstack-worker\t0\t81\t0\t80\t10\tclaude\tfalse",
        ]
    )

    dispatch_stack_command(  # type: ignore[arg-type]
        adapter,
        "main",
        "legion",
        "echo hello",
        cwd="/tmp",
        settle_seconds=0,
    )

    assert ("set-option", "-p", "-t", "%N", "@PANE_ID", "legion:1") in adapter.commands


def test_mechanicus_admin_is_not_treated_as_worker_and_workers_are_numeric():
    adapter = FakeLegionAdapter(
        window_name="mechanicus",
        rows=[
            "%F\tmechanicus:fabricator-general\tmechanicus\t0\t0\t0\t80\t25\tclaude\tfalse",
            "%A\tmechanicus:admin\tmechanicus\t0\t0\t26\t80\t24\tclaude\tfalse",
            "%W\tmechanicus:worker\tstack-worker\t1\t81\t0\t80\t42\tcodex\tfalse",
        ],
    )

    enforce_stack_layout(adapter, "main:4")  # type: ignore[arg-type]

    assert ("set-option", "-p", "-t", "%A", "@PANE_ID", "mechanicus:1") not in adapter.commands
    assert ("set-option", "-p", "-t", "%W", "@PANE_ID", "mechanicus:1") in adapter.commands


def test_mechanicus_enforce_creates_admin_pane_when_missing():
    adapter = FakeLegionAdapter(
        window_name="mechanicus",
        rows=[
            "%F\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse",
        ],
    )

    enforce_stack_layout(adapter, "main:4")  # type: ignore[arg-type]

    assert ("set-option", "-p", "-t", "%N", "@PANE_ID", "mechanicus:admin") in adapter.commands


def test_selecting_already_focused_worker_does_not_reenforce():
    adapter = FakeLegionAdapter(
        rows=[
            "%C\tlegion:custodes\tlegion\t0\t0\t0\t80\t50\tclaude\tfalse",
            "%1\tlegion:1\tstack-worker\t1\t81\t0\t80\t10\tclaude\tfalse",
            "%2\tlegion:2\tstack-worker\t0\t81\t11\t80\t39\tclaude\tfalse",
        ]
    )
    adapter.window_options["@STACK_FOCUSED_PANE"] = "%1"

    result = enforce_stack_layout(adapter, "main:3", focused_pane="%1", focus=True)  # type: ignore[arg-type]

    assert result == "noop stack focus %1: already focused"
    assert not any(command[0] == "resize-pane" for command in adapter.commands)
