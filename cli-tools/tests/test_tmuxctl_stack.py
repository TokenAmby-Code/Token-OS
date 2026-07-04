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
        window_name: str = "mechanicus",
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
                    "%C\tmechanicus:fabricator-general\t0\t0\t50",
                    "%1\tmechanicus:regiment\t0\t0\t3",
                    "%2\tmechanicus:regiment\t1\t4\t42",
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
        "%C\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse",
    ]
    adapter = FakeLegionAdapter(rows=rows)

    def _boom(*_args, **_kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(stack_core, "enforce_stack_layout", _boom)

    with pytest.raises(OSError):
        stack_core.add_orchestrator_stack_pane(
            adapter, "main", "mechanicus", cwd="/tmp", focus=False
        )

    assert any(command[0] == "kill-pane" and "%N" in command for command in adapter.commands)
    assert not any(row.startswith("%N\t") for row in adapter.rows or [])


def test_selecting_orchestrator_anchor_does_not_resize_stack():
    adapter = FakeLegionAdapter()

    result = focus_selected(adapter, "%C")  # type: ignore[arg-type]

    assert result.endswith(": orchestrator")
    assert not any(command[0] == "resize-pane" for command in adapter.commands)


def test_selecting_orchestrator_anchor_is_noop_even_when_window_zoomed():
    adapter = FakeLegionAdapter(zoomed=True)

    result = focus_selected(adapter, "%C")  # type: ignore[arg-type]

    assert result.endswith(": orchestrator")
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
            "%C\tmechanicus:fabricator-general\tmechanicus\t0\t0\t0\t80\t50\tclaude",
            "%1\tmechanicus:worker\tstack-worker\t1\t81\t0\t80\t10\tzsh",
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
            "%blank\tmechanicus:fabricator-general\tmechanicus\t0\t0\t0\t80\t20\tzsh",
            "%live\t\t\t1\t0\t0\t160\t50\tclaude",
        ]
    )

    result = enforce_stack_layout(adapter, "main:3")  # type: ignore[arg-type]

    assert result == "normalized stack layout main:3: orchestrator only"
    assert (
        "set-option",
        "-p",
        "-t",
        "%live",
        "@PANE_ID",
        "mechanicus:fabricator-general",
    ) in adapter.commands
    assert ("kill-pane", "-t", "%blank") in adapter.commands


def test_stack_dispatch_creates_managed_worker_and_launches_command():
    adapter = FakeLegionAdapter(
        rows=[
            "%C\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse",
        ]
    )

    pane = dispatch_stack_command(  # type: ignore[arg-type]
        adapter,
        "main",
        "mechanicus",
        "echo hello",
        cwd="/tmp",
        settle_seconds=0,
    )

    assert pane == "%N"
    assert ("set-option", "-p", "-t", "%N", "@PANE_ID", "mechanicus:1") in adapter.commands
    assert ("set-option", "-p", "-t", "%N", "@PANE_TYPE", "stack-worker") in adapter.commands
    assert ("send-keys", "-t", "%N", "echo hello", "Enter") in adapter.commands


def test_stack_dispatch_newborn_send_uses_scoped_override(monkeypatch):
    import tmuxctl.stack as stackmod

    adapter = FakeLegionAdapter(
        rows=[
            "%C\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse",
        ]
    )
    reasons: list[str] = []

    class Override:
        def __init__(self, reason: str) -> None:
            self.reason = reason

        def __enter__(self):
            reasons.append(self.reason)

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        stackmod.send_gate,
        "thread_local_override",
        lambda reason: Override(reason),
    )

    dispatch_stack_command(  # type: ignore[arg-type]
        adapter,
        "main",
        "mechanicus",
        "clear",
        cwd="/tmp",
        focus=False,
        settle_seconds=0,
    )

    assert reasons == ["tmuxctl-stack-dispatch-newborn"]
    assert ("send-keys", "-t", "%N", "clear", "Enter") in adapter.commands


def test_stack_dispatch_kills_newborn_pane_when_bootstrap_send_fails():
    class FailingSendAdapter(FakeLegionAdapter):
        def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
            self.commands.append(("send-keys", "-t", target, *keys))
            raise RuntimeError("send gate failed")

    adapter = FailingSendAdapter(
        rows=[
            "%C\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse",
        ]
    )

    with pytest.raises(RuntimeError, match="send gate failed"):
        dispatch_stack_command(  # type: ignore[arg-type]
            adapter,
            "main",
            "mechanicus",
            "clear",
            cwd="/tmp",
            focus=False,
            settle_seconds=0,
        )

    assert ("kill-pane", "-t", "%N") in adapter.commands
    assert not any(row.startswith("%N\t") for row in adapter.rows or [])


def test_stack_enforce_preserves_existing_numeric_worker_ids_with_gap():
    adapter = FakeLegionAdapter(
        rows=[
            "%C\tmechanicus:fabricator-general\tmechanicus\t0\t0\t0\t80\t50\tclaude\tfalse",
            "%1\tmechanicus:1\tstack-worker\t0\t81\t0\t80\t10\tclaude\tfalse",
            "%5\tmechanicus:5\tstack-worker\t1\t81\t11\t80\t39\tclaude\tfalse",
        ]
    )

    enforce_stack_layout(adapter, "main:3")  # type: ignore[arg-type]

    assert ("set-option", "-p", "-t", "%5", "@PANE_ID", "mechanicus:2") not in adapter.commands
    assert any(row.startswith("%5\tmechanicus:5\t") for row in adapter.rows or [])


def test_stack_dispatch_reuses_lowest_available_worker_id():
    adapter = FakeLegionAdapter(
        rows=[
            "%C\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse",
            "%2\tmechanicus:2\tstack-worker\t0\t81\t0\t80\t10\tclaude\tfalse",
        ]
    )

    dispatch_stack_command(  # type: ignore[arg-type]
        adapter,
        "main",
        "mechanicus",
        "echo hello",
        cwd="/tmp",
        settle_seconds=0,
    )

    assert ("set-option", "-p", "-t", "%N", "@PANE_ID", "mechanicus:1") in adapter.commands


def test_stack_add_no_focus_allocates_worker_without_selecting_it():
    adapter = FakeLegionAdapter(
        rows=[
            "%C\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse",
        ]
    )

    pane = add_stack_pane(  # type: ignore[arg-type]
        adapter,
        "main",
        "mechanicus",
        cwd="/tmp",
        focus=False,
    )

    assert pane == "%N"
    assert ("set-option", "-p", "-t", "%N", "@PANE_ID", "mechanicus:1") in adapter.commands
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


def test_mechanicus_orchestrator_is_not_treated_as_worker_and_workers_are_numeric():
    adapter = FakeLegionAdapter(
        window_name="mechanicus",
        rows=[
            "%F\tmechanicus:fabricator-general\tmechanicus\t0\t0\t0\t80\t25\tclaude\tfalse",
            "%A\tmechanicus:orchestrator\tmechanicus\t0\t0\t26\t80\t24\tclaude\tfalse",
            "%W\tmechanicus:worker\tstack-worker\t1\t81\t0\t80\t42\tcodex\tfalse",
        ],
    )

    enforce_stack_layout(adapter, "main:4")  # type: ignore[arg-type]

    assert ("set-option", "-p", "-t", "%A", "@PANE_ID", "mechanicus:1") not in adapter.commands
    assert ("set-option", "-p", "-t", "%W", "@PANE_ID", "mechanicus:1") in adapter.commands


def test_mechanicus_enforce_creates_orchestrator_pane_when_missing():
    adapter = FakeLegionAdapter(
        window_name="mechanicus",
        rows=[
            "%F\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse",
        ],
    )

    enforce_stack_layout(adapter, "main:4")  # type: ignore[arg-type]

    assert (
        "set-option",
        "-p",
        "-t",
        "%N",
        "@PANE_ID",
        "mechanicus:orchestrator",
    ) in adapter.commands


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
            "%C\tmechanicus:fabricator-general\tmechanicus\t0\t0\t0\t80\t50\tclaude\tfalse",
            "%1\tmechanicus:1\tstack-worker\t1\t81\t0\t80\t10\tclaude\tfalse",
            "%2\tmechanicus:2\tstack-worker\t0\t81\t11\t80\t39\tclaude\tfalse",
        ]
    )
    adapter.window_options["@STACK_FOCUSED_PANE"] = "%1"

    result = enforce_stack_layout(adapter, "main:3", focused_pane="%1", focus=True)  # type: ignore[arg-type]

    assert result == "noop stack focus %1: already focused"
    assert not any(command[0] == "resize-pane" for command in adapter.commands)


def test_adopt_joins_existing_pane_without_splitting_a_fresh_shell():
    adapter = FakeLegionAdapter(
        rows=[
            "%C\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t25\tclaude\tfalse",
            # The durable Malcador seat is already present (builder creates it),
            # so enforce docks rather than re-splits it. (Pax moved to council.)
            "%M\tmechanicus:orchestrator\tmechanicus\t0\t0\t25\t80\t25\tclaude\tfalse",
        ]
    )

    pane = add_stack_pane(  # type: ignore[arg-type]
        adapter,
        "main",
        "mechanicus",
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
    assert ("set-option", "-p", "-t", "%live", "@PANE_ID", "mechanicus:1") in adapter.commands
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
            "%C\tmechanicus:fabricator-general\tmechanicus\t0\t0\t0\t80\t25\tclaude\tfalse",
            # Durable Malcador seat present alongside the worker stack.
            "%M\tmechanicus:orchestrator\tmechanicus\t0\t0\t25\t80\t25\tclaude\tfalse",
            "%1\tmechanicus:1\tstack-worker\t1\t81\t0\t80\t10\tclaude\tfalse",
        ]
    )

    pane = add_stack_pane(  # type: ignore[arg-type]
        adapter,
        "main",
        "mechanicus",
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
    # Existing worker keeps mechanicus:1; the adopted pane takes the next ordinal.
    assert ("set-option", "-p", "-t", "%live", "@PANE_ID", "mechanicus:2") in adapter.commands


def test_adopt_creates_legion_window_and_custodes_before_joining():
    adapter = FakeLegionAdapter(rows=[], window_present=False)

    add_stack_pane(  # type: ignore[arg-type]
        adapter,
        "main",
        "mechanicus",
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
        if command[:2] == ("set-option", "-p")
        and command[-2:] == ("@PANE_ID", "mechanicus:fabricator-general")
    )
    join_idx = next(i for i, command in enumerate(adapter.commands) if command[0] == "join-pane")

    assert new_window_idx < join_idx
    assert custodes_idx < join_idx


def test_adopt_does_not_kill_the_live_pane_when_enforce_fails(monkeypatch):
    import tmuxctl._stack_core as stack_core

    adapter = FakeLegionAdapter(
        rows=[
            "%C\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse",
        ]
    )

    def _boom(*_args, **_kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(stack_core, "enforce_stack_layout", _boom)

    with pytest.raises(OSError):
        stack_core.add_orchestrator_stack_pane(
            adapter, "main", "mechanicus", cwd="/tmp", focus=False, adopt_pane="%live"
        )

    # The user's live agent must survive a post-join enforce failure.
    assert not any(command[0] == "kill-pane" and "%live" in command for command in adapter.commands)
    assert any(row.startswith("%live\t") for row in adapter.rows or [])


def test_adopt_no_focus_does_not_select_or_record_focused_pane():
    adapter = FakeLegionAdapter(
        rows=[
            "%C\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse",
        ]
    )

    add_stack_pane(  # type: ignore[arg-type]
        adapter,
        "main",
        "mechanicus",
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


def test_merged_stack_stack_uses_pax_left_column_and_worker_right_stack():
    adapter = FakeLegionAdapter(
        window_name="mechanicus",
        rows=[
            "%P\tmechanicus:fabricator-general\tmechanicus\t0\t0\t0\t80\t50\tclaude",
            "%1\tmechanicus:worker\tstack-worker\t1\t81\t0\t80\t42\tclaude",
        ],
    )

    result = enforce_stack_layout(adapter, "main:5", focused_pane="%1", focus=True)  # type: ignore[arg-type]

    assert result == "focused stack %1 in main:5"
    assert ("set-window-option", "-t", "main:5", "main-pane-width", "80") in adapter.commands
    assert ("resize-pane", "-t", "%P", "-x", "80") in adapter.commands
    assert adapter.window_options["@STACK_FOCUSED_PANE"] == "%1"


def test_merged_stack_orchestrator_is_not_treated_as_worker_and_workers_are_numeric():
    adapter = FakeLegionAdapter(
        window_name="mechanicus",
        rows=[
            "%P\tmechanicus:fabricator-general\tmechanicus\t0\t0\t0\t80\t25\tclaude\tfalse",
            "%O\tmechanicus:orchestrator\tmechanicus\t0\t0\t26\t80\t24\tclaude\tfalse",
            "%W\tmechanicus:worker\tstack-worker\t1\t81\t0\t80\t42\tclaude\tfalse",
        ],
    )

    enforce_stack_layout(adapter, "main:5")  # type: ignore[arg-type]

    # The orchestrator seat is a secondary persona, never reclassified as a worker.
    assert ("set-option", "-p", "-t", "%O", "@PANE_ID", "mechanicus:1") not in adapter.commands
    assert ("set-option", "-p", "-t", "%W", "@PANE_ID", "mechanicus:1") in adapter.commands


def test_merged_stack_enforce_creates_orchestrator_pane_when_missing():
    adapter = FakeLegionAdapter(
        window_name="mechanicus",
        rows=[
            "%P\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse",
        ],
    )

    enforce_stack_layout(adapter, "main:5")  # type: ignore[arg-type]

    assert (
        "set-option",
        "-p",
        "-t",
        "%N",
        "@PANE_ID",
        "mechanicus:orchestrator",
    ) in adapter.commands


def test_merged_stack_stack_dispatch_creates_managed_worker_and_launches_command():
    adapter = FakeLegionAdapter(
        window_name="mechanicus",
        rows=[
            "%P\tmechanicus:fabricator-general\tmechanicus\t1\t0\t0\t80\t50\tclaude\tfalse",
        ],
    )

    pane = dispatch_stack_command(  # type: ignore[arg-type]
        adapter,
        "main",
        "mechanicus",
        "echo civic",
        cwd="/Volumes/Civic/Pax-ENV",
        settle_seconds=0,
    )

    assert pane == "%N"
    assert ("set-option", "-p", "-t", "%N", "@PANE_ID", "mechanicus:1") in adapter.commands
    assert ("set-option", "-p", "-t", "%N", "@PANE_TYPE", "stack-worker") in adapter.commands
    assert ("send-keys", "-t", "%N", "echo civic", "Enter") in adapter.commands


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


def test_reservists_pinned_roles_are_spec_scoped_not_global_singletons():
    from tmuxctl._stack_core import STACK_PAGE_SPECS, _is_pinned_role
    from tmuxctl.singleton_labels import is_persona_singleton_label

    spec = STACK_PAGE_SPECS["reservists"]

    assert _is_pinned_role("reservists:civic", spec)
    assert _is_pinned_role("reservists:token-os", spec)
    assert not is_persona_singleton_label("reservists:civic")
    assert not is_persona_singleton_label("reservists:token-os")


class FakeReservistsAdapter:
    def __init__(self, rows: list[list[str]] | None = None) -> None:
        self.rows = rows or [
            ["%C", "reservists:civic", "reservists", "1", "0", "0", "100", "50", "claude", "false"],
            [
                "%T",
                "reservists:token-os",
                "reservists",
                "0",
                "100",
                "0",
                "100",
                "50",
                "claude",
                "false",
            ],
        ]
        self.commands: list[tuple[str, ...]] = []
        self.window_options: dict[str, str] = {}
        self._next_pane = 200
        self.band_h = 22

    def show_window_option(self, target: str, option: str) -> str:
        return self.window_options.get(option, "")

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.run("send-keys", "-t", target, *keys, allow_failure=allow_failure)

    def _alloc(self) -> str:
        self._next_pane += 1
        return f"%{self._next_pane}"

    def _row(self, pane: str) -> list[str] | None:
        return next((row for row in self.rows if row[0] == pane), None)

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.commands.append(args)
        if args[0] == "display-message":
            target = args[args.index("-t") + 1] if "-t" in args else ""
            fmt = args[-1]
            if fmt == "#{session_name}\t#{window_index}\t#{window_name}":
                return "main\t7\treservists\n"
            if fmt == "#{window_width}":
                return "200\n"
            if fmt == "#{window_height}":
                return "50\n"
            if fmt == "#{window_name}":
                return "reservists\n"
            if fmt == "#{window_zoomed_flag}":
                return "0\n"
            if fmt == "#{session_name}:#{window_index}":
                return "main:7\n"
            if fmt == "#{session_name}:#{window_name}":
                return "main:reservists\n"
            if fmt == "#{pane_id}":
                return f"{target}\n" if target else "%C\n"
            return ""
        if args[0] == "list-windows":
            if args[-1] == "#{window_index}\t#{window_name}":
                return "7\treservists\n8\tmars\n"
            if args[-1] == "#{window_name}":
                return "reservists\nmars\n"
        if args[0] == "list-panes":
            return "\n".join("\t".join(row) for row in self.rows)
        if args[0] == "set-option" and "-w" in args:
            self.window_options[args[-2]] = args[-1]
        if args[0] == "set-option" and "-p" in args:
            target = args[args.index("-t") + 1]
            row = self._row(target)
            if row:
                option = args[-2]
                value = args[-1]
                if option == "@PANE_ID":
                    row[1] = value
                elif option == "@PANE_TYPE":
                    row[2] = value
                elif option == "@STACK_PENDING":
                    row[9] = value
        if args[0] == "split-window":
            pane = self._alloc()
            if "-h" in args:
                self.rows.append([pane, "", "", "0", "100", "0", "100", "50", "zsh", "false"])
            elif "-f" in args:
                self.rows.append(
                    [pane, "", "", "0", "0", str(self.band_h), "200", "3", "zsh", "false"]
                )
            else:
                self.rows.append(
                    [pane, "", "", "0", "0", str(self.band_h + 4), "200", "3", "zsh", "false"]
                )
            return f"{pane}\n"
        if args[0] == "join-pane":
            src = args[args.index("-s") + 1]
            row = self._row(src)
            if row:
                row[4], row[5], row[6] = "0", str(self.band_h), "200"
        if args[0] == "resize-pane":
            target = args[args.index("-t") + 1]
            row = self._row(target)
            if row:
                if "-x" in args:
                    row[6] = args[args.index("-x") + 1]
                if "-y" in args:
                    row[7] = args[args.index("-y") + 1]
                    if row[1] == "reservists:civic":
                        token = next((r for r in self.rows if r[1] == "reservists:token-os"), None)
                        if token:
                            token[4], token[5], token[6], token[7] = (
                                row[6],
                                "0",
                                str(200 - int(row[6])),
                                row[7],
                            )
        if args[0] == "kill-pane":
            target = args[args.index("-t") + 1]
            self.rows = [row for row in self.rows if row[0] != target]
        return ""


def test_reservists_enforce_pins_top_row_and_full_width_workers():
    adapter = FakeReservistsAdapter(
        rows=[
            ["%C", "reservists:civic", "reservists", "1", "0", "0", "100", "50", "claude", "false"],
            [
                "%T",
                "reservists:token-os",
                "reservists",
                "0",
                "100",
                "0",
                "100",
                "50",
                "claude",
                "false",
            ],
            ["%W", "reservists:9", "stack-worker", "0", "100", "0", "100", "20", "codex", "false"],
        ]
    )

    result = enforce_stack_layout(adapter, "main:7")  # type: ignore[arg-type]

    assert result == "normalized pinned stack layout main:7"
    by_role = {row[1]: row for row in adapter.rows}
    assert by_role["reservists:civic"][4:8] == ["0", "0", "100", "22"]
    assert by_role["reservists:token-os"][4:8] == ["100", "0", "100", "22"]
    worker = by_role["reservists:1"]
    assert worker[4] == "0"
    assert int(worker[5]) >= 22
    assert worker[6] == "200"
    assert ("join-pane", "-v", "-f", "-d", "-s", "%W", "-t", "%C") in adapter.commands
    assert not any(command[0] == "select-layout" for command in adapter.commands)


def test_reservists_enforce_recreates_missing_pinned_seat_and_retains_workers():
    adapter = FakeReservistsAdapter(
        rows=[
            ["%C", "reservists:civic", "reservists", "1", "0", "0", "200", "50", "claude", "false"],
            ["%W", "reservists:1", "stack-worker", "0", "0", "25", "200", "10", "codex", "false"],
        ]
    )

    enforce_stack_layout(adapter, "main:7")  # type: ignore[arg-type]

    roles = [row[1] for row in adapter.rows]
    assert "reservists:civic" in roles
    assert "reservists:token-os" in roles
    assert "reservists:1" in roles
    assert any(command[0] == "split-window" and "-h" in command for command in adapter.commands)
    token_pane = next(row[0] for row in adapter.rows if row[1] == "reservists:token-os")
    assert ("set-option", "-p", "-t", token_pane, "@TOKEN_OS_RESERVIST", "1") in adapter.commands


def test_reservists_first_add_uses_full_window_bottom_band_without_carving_seats():
    adapter = FakeReservistsAdapter()

    pane = add_stack_pane(adapter, "main", "reservists", cwd="/tmp", focus=False)  # type: ignore[arg-type]

    assert pane.startswith("%")
    assert any(
        command[:5] == ("split-window", "-v", "-f", "-t", "%C")
        and command[command.index("-l") + 1] == str(STACK_COLLAPSED_HEIGHT)
        for command in adapter.commands
    )
    assert "reservists:civic" in [row[1] for row in adapter.rows]
    assert "reservists:token-os" in [row[1] for row in adapter.rows]
    assert ("set-option", "-p", "-t", pane, "@PANE_ID", "reservists:1") in adapter.commands


def test_reservists_workers_renumber_after_removal_without_retagging_pinned_seats():
    adapter = FakeReservistsAdapter(
        rows=[
            ["%C", "reservists:civic", "reservists", "1", "0", "0", "100", "22", "claude", "false"],
            [
                "%T",
                "reservists:token-os",
                "reservists",
                "0",
                "100",
                "0",
                "100",
                "22",
                "claude",
                "false",
            ],
            ["%A", "reservists:1", "stack-worker", "0", "0", "22", "200", "3", "codex", "false"],
            ["%B", "reservists:3", "stack-worker", "0", "0", "26", "200", "3", "codex", "false"],
        ]
    )

    enforce_stack_layout(adapter, "main:7")  # type: ignore[arg-type]

    assert ("set-option", "-p", "-t", "%B", "@PANE_ID", "reservists:2") in adapter.commands
    assert not any(
        command[:5] == ("set-option", "-p", "-t", "%C", "@PANE_ID")
        and command[-1].startswith("reservists:")
        and command[-1][-1:].isdigit()
        for command in adapter.commands
    )
    assert not any(
        command[:5] == ("set-option", "-p", "-t", "%T", "@PANE_ID")
        and command[-1].startswith("reservists:")
        and command[-1][-1:].isdigit()
        for command in adapter.commands
    )


def test_reservists_rebuilds_missing_seats_without_stealing_live_workers():
    adapter = FakeReservistsAdapter(
        rows=[
            ["%A", "reservists:1", "stack-worker", "1", "0", "22", "200", "3", "codex", "false"],
            ["%B", "reservists:2", "stack-worker", "0", "0", "26", "200", "3", "codex", "false"],
        ]
    )

    enforce_stack_layout(adapter, "main:7")  # type: ignore[arg-type]

    roles = {row[1] for row in adapter.rows}
    assert {"reservists:civic", "reservists:token-os", "reservists:1", "reservists:2"} <= roles
    assert ("set-option", "-p", "-t", "%A", "@PANE_ID", "reservists:civic") not in adapter.commands
    assert (
        "set-option",
        "-p",
        "-t",
        "%A",
        "@PANE_ID",
        "reservists:token-os",
    ) not in adapter.commands


def test_reservists_add_rejects_adopt():
    adapter = FakeReservistsAdapter()

    with pytest.raises(ValueError):
        add_stack_pane(  # type: ignore[arg-type]
            adapter,
            "main",
            "reservists",
            cwd="/tmp",
            focus=False,
            adopt_pane="%live",
        )


def test_dispatch_reflow_routes_by_stack_page_spec_not_orchestrator_set(monkeypatch):
    import tmuxctl.stack as stackmod

    class TinyAdapter:
        def __init__(self, window_name: str) -> None:
            self.window_name = window_name
            self.commands: list[tuple[str, ...]] = []

        def run(self, *args: str, allow_failure: bool = False) -> str:
            self.commands.append(args)
            if args[0] == "display-message" and args[-1] == "#{session_name}:#{window_name}":
                return f"main:{self.window_name}\n"
            if args[0] == "display-message" and args[-1] == "#{session_name}:#{window_index}":
                return "main:7\n"
            if args[0] == "display-message" and args[-1] in {
                "#{session_name}:#{window_index}\t#{pane_id}\t#{window_zoomed_flag}\t#{client_activity}",
                "#{session_name}:#{window_index}\t#{pane_id}",
            }:
                return "main:7\t%X\n"
            if args[0] == "display-message" and args[-1] == "#{pane_id}":
                return "%X\n"
            return ""

        def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
            self.run("send-keys", "-t", target, *keys, allow_failure=allow_failure)

    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(stackmod, "add_stack_pane", lambda adapter, session, base, **kw: "%NEW")
    monkeypatch.setattr(
        stackmod,
        "enforce_stack_layout",
        lambda adapter, target, **kw: calls.append((adapter.window_name, target)) or "ok",
    )

    stackmod.dispatch_stack_command(
        TinyAdapter("reservists"), "main", "reservists", "echo r", settle_seconds=0
    )
    stackmod.dispatch_stack_command(TinyAdapter("mars"), "main", "mars", "echo m", settle_seconds=0)

    assert calls == [("reservists", "main:7")]


def test_mechanicus_enforce_transcript_stays_byte_identical():
    adapter = FakeLegionAdapter(
        window_name="mechanicus",
        rows=[
            "%F\tmechanicus:fabricator-general\tmechanicus\t0\t0\t0\t80\t50\tclaude\tfalse",
            "%O\tmechanicus:orchestrator\tmechanicus\t0\t0\t26\t80\t24\tclaude\tfalse",
            "%W\tmechanicus:worker\tstack-worker\t1\t81\t0\t80\t42\tcodex\tfalse",
        ],
    )

    result = enforce_stack_layout(adapter, "main:5")  # type: ignore[arg-type]

    assert result == "normalized stack layout main:5"
    assert adapter.commands == [
        (
            "display-message",
            "-p",
            "#{session_name}:#{window_index}\t#{pane_id}\t#{window_zoomed_flag}\t#{client_activity}",
        ),
        ("display-message", "-p", "#{session_name}:#{window_index}\t#{pane_id}"),
        ("display-message", "-p", "#{session_name}:#{window_index}"),
        ("display-message", "-p", "#{pane_id}"),
        ("display-message", "-t", "main:5", "-p", "#{window_name}"),
        (
            "list-panes",
            "-t",
            "main:5",
            "-F",
            "#{pane_id}\t#{@PANE_ID}\t#{@PANE_TYPE}\t#{pane_active}\t#{pane_left}\t#{pane_top}\t#{pane_width}\t#{pane_height}\t#{pane_current_command}\t#{@STACK_PENDING}",
        ),
        ("display-message", "-t", "main:5", "-p", "#{window_zoomed_flag}"),
        ("display-message", "-t", "main:5", "-p", "#{window_height}"),
        ("set-option", "-p", "-t", "%W", "@PANE_ID", "mechanicus:1"),
        ("set-option", "-p", "-t", "%W", "@PANE_TYPE", "stack-worker"),
        ("set-option", "-p", "-t", "%W", "@GRID_STATE", "small"),
        (
            "list-panes",
            "-t",
            "main:5",
            "-F",
            "#{pane_id}\t#{@PANE_ID}\t#{@PANE_TYPE}\t#{pane_active}\t#{pane_left}\t#{pane_top}\t#{pane_width}\t#{pane_height}\t#{pane_current_command}\t#{@STACK_PENDING}",
        ),
        ("display-message", "-t", "main:5", "-p", "#{window_width}"),
        ("display-message", "-t", "main:5", "-p", "#{window_height}"),
        ("set-option", "-w", "-t", "main:5", "@STACK_FOCUS_GUARD", "true"),
        ("set-option", "-p", "-t", "%F", "@PANE_ID", "mechanicus:fabricator-general"),
        ("set-option", "-p", "-t", "%F", "@PANE_TYPE", "mechanicus"),
        ("set-option", "-p", "-t", "%F", "@GRID_STATE", "small"),
        ("set-option", "-p", "-t", "%W", "@PANE_TYPE", "stack-worker"),
        ("set-option", "-p", "-t", "%W", "@GRID_STATE", "small"),
        ("set-window-option", "-t", "main:5", "main-pane-width", "80"),
        ("select-layout", "-t", "main:5", "main-vertical"),
        ("resize-pane", "-t", "%F", "-x", "80"),
        (
            "list-panes",
            "-t",
            "main:5",
            "-F",
            "#{pane_id}\t#{@PANE_ID}\t#{@PANE_TYPE}\t#{pane_active}\t#{pane_left}\t#{pane_top}\t#{pane_width}\t#{pane_height}\t#{pane_current_command}\t#{@STACK_PENDING}",
        ),
        ("display-message", "-t", "main:5", "-p", "#{window_height}"),
        ("resize-pane", "-t", "%F", "-x", "80"),
        ("resize-pane", "-t", "%F", "-y", "24"),
        ("resize-pane", "-t", "%O", "-x", "80"),
        ("set-option", "-w", "-t", "main:5", "@STACK_FOCUS_GUARD", "false"),
    ]
