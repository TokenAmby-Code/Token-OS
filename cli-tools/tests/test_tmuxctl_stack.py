from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import pytest
from tmuxctl.stack import (
    STACK_COLLAPSED_HEIGHT,
    add_stack_pane,
    dispatch_stack_command,
    enforce_stack_layout,
    focus_selected,
    sweep_stack_assertions,
)


class FakeLegionAdapter:
    def __init__(
        self,
        *,
        guard: bool = False,
        window_name: str = "legion",
        rows: list[str] | None = None,
        zoomed: bool = False,
        window_present: bool = True,
    ) -> None:
        self.guard = guard
        self.window_name = window_name
        self.rows = rows
        self.zoomed = zoomed
        self.window_present = window_present
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
            if fmt == "#{window_zoomed_flag}":
                return "1\n" if self.zoomed else "0\n"
            if fmt == "#{session_name}:#{window_index}":
                return "main:3\n"
            if fmt == "#{session_name}:#{window_name}":
                return f"main:{self.window_name}\n"
            if fmt == "#{pane_id}":
                return f"{target}\n"
        if args[0] == "list-windows":
            if args[-1] == "#{window_index}\t#{window_name}":
                return f"3\t{self.window_name}\n"
            if not self.window_present:
                return ""
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
        if args[0] == "join-pane" and self.rows is not None:
            # Adoption preserves the source pane id + its running process; the
            # joined pane simply appears in this window's pane list.
            src = args[args.index("-s") + 1]
            if not any(row.startswith(f"{src}\t") for row in self.rows):
                self.rows.append(f"{src}\t\t\t0\t81\t0\t80\t10\tclaude\tfalse")
            return ""
        if args[0] == "kill-pane" and self.rows is not None:
            pane = args[args.index("-t") + 1]
            self.rows = [row for row in self.rows if not row.startswith(f"{pane}\t")]
        return ""


def test_add_stack_pane_kills_new_worker_when_layout_fails(monkeypatch):
    import tmuxctl._stack_core as stack_core

    rows = [
        "%C\tlegion:custodes\tlegion\t1\t0\t0\t80\t50\tclaude\tfalse",
    ]
    adapter = FakeLegionAdapter(rows=rows)

    def _boom(*_args, **_kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(stack_core, "enforce_stack_layout", _boom)

    with pytest.raises(OSError):
        stack_core.add_orchestrator_stack_pane(adapter, "main", "legion", cwd="/tmp", focus=False)

    assert any(command[0] == "kill-pane" and "%N" in command for command in adapter.commands)
    assert not any(row.startswith("%N\t") for row in adapter.rows or [])


def test_selecting_custodes_does_not_resize_legion():
    adapter = FakeLegionAdapter()

    result = focus_selected(adapter, "%C")  # type: ignore[arg-type]

    assert result.endswith(": custodes")
    assert not any(command[0] == "resize-pane" for command in adapter.commands)


def test_selecting_custodes_is_noop_even_when_window_zoomed():
    adapter = FakeLegionAdapter(zoomed=True)

    result = focus_selected(adapter, "%C")  # type: ignore[arg-type]

    assert result.endswith(": custodes")
    assert not any(command[0] == "resize-pane" for command in adapter.commands)
    assert adapter.zoomed is True


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
    assert ("select-layout", "-t", "main:3", "main-vertical") in adapter.commands
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


def test_stack_add_no_focus_allocates_worker_without_selecting_it():
    adapter = FakeLegionAdapter(
        rows=[
            "%C\tlegion:custodes\tlegion\t1\t0\t0\t80\t50\tclaude\tfalse",
        ]
    )

    pane = add_stack_pane(  # type: ignore[arg-type]
        adapter,
        "main",
        "legion",
        cwd="/tmp",
        focus=False,
    )

    assert pane == "%N"
    assert ("set-option", "-p", "-t", "%N", "@PANE_ID", "legion:1") in adapter.commands
    assert ("set-option", "-p", "-t", "%N", "@PANE_TYPE", "stack-worker") in adapter.commands
    assert not any(
        command[0] == "select-pane" and "-T" not in command for command in adapter.commands
    )
    assert "@STACK_FOCUSED_PANE" not in adapter.window_options


def test_mechanicus_stack_dispatch_no_focus_does_not_select_worker():
    adapter = FakeLegionAdapter(
        window_name="mechanicus",
        rows=[
            "%F\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse",
            "%1\tmechanicus:1\tstack-worker\t0\t81\t0\t80\t10\tcodex\tfalse",
        ],
    )

    pane = dispatch_stack_command(  # type: ignore[arg-type]
        adapter,
        "main",
        "mechanicus",
        "echo hello",
        cwd="/tmp",
        focus=False,
        settle_seconds=0,
    )

    assert pane == "%N"
    assert not any(command[0] == "select-window" for command in adapter.commands)
    assert not any(
        command[0] == "select-pane" and "-T" not in command for command in adapter.commands
    )


def test_stale_stored_stack_focus_falls_back_to_live_worker():
    adapter = FakeLegionAdapter(
        window_name="mechanicus",
        rows=[
            "%F\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse",
            "%1\tmechanicus:1\tstack-worker\t0\t81\t0\t80\t10\tcodex\tfalse",
        ],
    )
    adapter.window_options["@STACK_FOCUSED_PANE"] = "%77"

    dispatch_stack_command(  # type: ignore[arg-type]
        adapter,
        "main",
        "mechanicus",
        "echo hello",
        cwd="/tmp",
        focus=False,
        settle_seconds=0,
    )

    assert (
        "split-window",
        "-v",
        "-t",
        "%1",
        "-d",
        "-P",
        "-F",
        "#{pane_id}",
        "-l",
        "3",
        "-c",
        "/tmp",
    ) in adapter.commands


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


def test_zoomed_stack_enforce_defers_structural_layout_mutations():
    adapter = FakeLegionAdapter(
        window_name="mechanicus",
        zoomed=True,
        rows=[
            "%F\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse",
        ],
    )

    result = enforce_stack_layout(adapter, "main:4")  # type: ignore[arg-type]

    assert result == "noop stack layout main:4: window zoomed"
    assert not any(
        command[0] in {"select-layout", "resize-pane", "join-pane", "split-window"}
        for command in adapter.commands
    )


def test_zoomed_stack_sweep_noops_and_does_not_issue_focus_or_layout_commands():
    adapter = FakeLegionAdapter(zoomed=True)

    result = sweep_stack_assertions(adapter, "main")  # type: ignore[arg-type]

    assert result == "noop stack layout main:3: window zoomed"
    assert not any(
        command[0]
        in {
            "select-window",
            "select-pane",
            "select-layout",
            "resize-pane",
            "join-pane",
            "split-window",
        }
        for command in adapter.commands
    )


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


def test_adopt_joins_existing_pane_without_splitting_a_fresh_shell():
    adapter = FakeLegionAdapter(
        rows=[
            "%C\tlegion:custodes\tlegion\t1\t0\t0\t80\t50\tclaude\tfalse",
        ]
    )

    pane = add_stack_pane(  # type: ignore[arg-type]
        adapter,
        "main",
        "legion",
        cwd="/tmp",
        focus=False,
        adopt_pane="%live",
    )

    # The live pane keeps its id — no fresh %N shell is split.
    assert pane == "%live"
    assert not any(command[0] == "split-window" for command in adapter.commands)
    # Joined in (no-workers geometry: horizontal against the orchestrator).
    assert any(
        command[:7] == ("join-pane", "-h", "-d", "-s", "%live", "-t", "%C")
        for command in adapter.commands
    )
    # The allocator tags it as the lowest free worker ordinal.
    assert ("set-option", "-p", "-t", "%live", "@PANE_ID", "legion:1") in adapter.commands
    assert ("set-option", "-p", "-t", "%live", "@PANE_TYPE", "stack-worker") in adapter.commands
    # A live worker is never flagged pending (that would mark it for the reap).
    assert not any(
        command[:5] == ("set-option", "-p", "-t", "%live", "@STACK_PENDING")
        and command[-1] == "true"
        for command in adapter.commands
    )


def test_adopt_with_existing_workers_joins_vertically_onto_the_stack():
    adapter = FakeLegionAdapter(
        rows=[
            "%C\tlegion:custodes\tlegion\t0\t0\t0\t80\t50\tclaude\tfalse",
            "%1\tlegion:1\tstack-worker\t1\t81\t0\t80\t10\tclaude\tfalse",
        ]
    )

    pane = add_stack_pane(  # type: ignore[arg-type]
        adapter,
        "main",
        "legion",
        cwd="/tmp",
        focus=False,
        adopt_pane="%live",
    )

    assert pane == "%live"
    assert not any(command[0] == "split-window" for command in adapter.commands)
    assert (
        "join-pane",
        "-v",
        "-d",
        "-s",
        "%live",
        "-t",
        "%1",
        "-l",
        str(STACK_COLLAPSED_HEIGHT),
    ) in adapter.commands
    # Existing worker keeps legion:1; the adopted pane takes the next ordinal.
    assert ("set-option", "-p", "-t", "%live", "@PANE_ID", "legion:2") in adapter.commands


def test_adopt_creates_legion_window_and_custodes_before_joining():
    adapter = FakeLegionAdapter(rows=[], window_present=False)

    add_stack_pane(  # type: ignore[arg-type]
        adapter,
        "main",
        "legion",
        cwd="/tmp",
        focus=False,
        adopt_pane="%live",
    )

    new_window_idx = next(
        i for i, command in enumerate(adapter.commands) if command[0] == "new-window"
    )
    custodes_idx = next(
        i
        for i, command in enumerate(adapter.commands)
        if command[:2] == ("set-option", "-p") and command[-2:] == ("@PANE_ID", "legion:custodes")
    )
    join_idx = next(i for i, command in enumerate(adapter.commands) if command[0] == "join-pane")

    assert new_window_idx < join_idx
    assert custodes_idx < join_idx


def test_adopt_does_not_kill_the_live_pane_when_enforce_fails(monkeypatch):
    import tmuxctl._stack_core as stack_core

    adapter = FakeLegionAdapter(
        rows=[
            "%C\tlegion:custodes\tlegion\t1\t0\t0\t80\t50\tclaude\tfalse",
        ]
    )

    def _boom(*_args, **_kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(stack_core, "enforce_stack_layout", _boom)

    with pytest.raises(OSError):
        stack_core.add_orchestrator_stack_pane(
            adapter, "main", "legion", cwd="/tmp", focus=False, adopt_pane="%live"
        )

    # The user's live agent must survive a post-join enforce failure.
    assert not any(command[0] == "kill-pane" and "%live" in command for command in adapter.commands)
    assert any(row.startswith("%live\t") for row in adapter.rows or [])


def test_adopt_no_focus_does_not_select_or_record_focused_pane():
    adapter = FakeLegionAdapter(
        rows=[
            "%C\tlegion:custodes\tlegion\t1\t0\t0\t80\t50\tclaude\tfalse",
        ]
    )

    add_stack_pane(  # type: ignore[arg-type]
        adapter,
        "main",
        "legion",
        cwd="/tmp",
        focus=False,
        adopt_pane="%live",
    )

    assert not any(command[0] == "select-window" for command in adapter.commands)
    # The -T "regiment" title select-pane is allowed; a focusing select-pane is not.
    assert not any(
        command[0] == "select-pane" and "-T" not in command for command in adapter.commands
    )
    assert "@STACK_FOCUSED_PANE" not in adapter.window_options


def test_adopt_rejected_for_non_orchestrator_stack():
    adapter = FakeLegionAdapter(window_name="mars", rows=[])

    with pytest.raises(ValueError):
        add_stack_pane(  # type: ignore[arg-type]
            adapter,
            "main",
            "mars",
            cwd="/tmp",
            focus=False,
            adopt_pane="%live",
        )


def test_stack_enforce_defers_while_pane_select_zoom_restore_pending():
    adapter = FakeLegionAdapter(
        window_name="mechanicus",
        rows=[
            "%F\tmechanicus:fabricator-general\tmechanicus\t0\t0\t0\t80\t50\tclaude\tfalse",
            "%1\tmechanicus:1\tstack-worker\t1\t81\t0\t80\t10\tcodex\tfalse",
        ],
    )
    adapter.window_options["@PANE_SELECT_ZOOM_RESTORE_PENDING"] = "true"

    result = enforce_stack_layout(adapter, "main:4", focused_pane="%1", focus=True)  # type: ignore[arg-type]

    assert result == "noop stack layout main:4: pane-select zoom restore pending"
    assert not any(
        command[0] in {"select-layout", "resize-pane", "join-pane", "split-window"}
        for command in adapter.commands
    )
