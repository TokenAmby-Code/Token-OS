"""Regression suite for the stack ``:new`` duplicate-canonical-id fault (Fault C).

Emperor ruling (2026-07-02): ``new`` is a stateless launch alias, never a stored
id. Two ``:new`` dispatches at a non-orchestrator stack page (reservists/mars/
kreig) must each mint a *distinct* real ``{page}:{id}`` — never the shared
``{base}:worker`` default label, never ``new`` itself — so ``talk``/``brief``
resolve each worker unambiguously.

Daemon/labeling tests never touch live tmux: fake pane ids + modeled window state.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.enums import GridState, PaneKind, WindowArchetype
from tmuxctl.models import PaneSnapshot, WindowSnapshot, WorkspaceSnapshot
from tmuxctl.resolver import resolve_pane_in_snapshot
from tmuxctl.stack import _base_worker_ordinal, add_stack_pane


class FakeStackAdapter:
    """Minimal multi-window tmux fake for the non-orchestrator stack path.

    Models the base's spill windows and each window's panes (``pane_id`` +
    ``@PANE_ID`` role) so the allocator's live-ledger scan can mint a unique
    ordinal. Only the tmux verbs ``add_stack_pane`` touches are implemented.
    """

    def __init__(self, *, session: str = "main") -> None:
        self.session = session
        # window_name -> list of [pane_id, pane_role, pane_type]
        self.windows: dict[str, list[list[str]]] = {}
        self.commands: list[tuple[str, ...]] = []
        self.window_options: dict[str, str] = {}
        self._next_pane = 100

    def _alloc_pane(self) -> str:
        self._next_pane += 1
        return f"%{self._next_pane}"

    def _win_of(self, target: str) -> str:
        if ":" in target:
            return target.split(":", 1)[1]
        for name, rows in self.windows.items():
            if any(row[0] == target for row in rows):
                return name
        return target

    def role_of(self, pane_id: str) -> str:
        for rows in self.windows.values():
            for row in rows:
                if row[0] == pane_id:
                    return row[1]
        return ""

    def show_window_option(self, target: str, option: str) -> str:
        return self.window_options.get(option, "")

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.run("send-keys", "-t", target, *keys, allow_failure=allow_failure)

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.commands.append(args)
        cmd = args[0]
        if cmd == "list-windows":
            if args[-1] == "#{window_name}":
                return "\n".join(self.windows.keys())
            if args[-1] == "#{window_index}\t#{window_name}":
                return "\n".join(f"{i}\t{name}" for i, name in enumerate(self.windows, start=1))
            return ""
        if cmd == "new-window":
            name = args[args.index("-n") + 1]
            pane = self._alloc_pane()
            self.windows[name] = [[pane, "", ""]]
            return ""
        if cmd == "display-message":
            fmt = args[-1]
            target = args[args.index("-t") + 1] if "-t" in args else ""
            if fmt == "#{pane_id}":
                win = self._win_of(target)
                rows = self.windows.get(win)
                return f"{rows[0][0]}\n" if rows else "\n"
            if fmt == "#{session_name}:#{window_name}":
                return f"{self.session}:{self._win_of(target)}\n"
            if fmt == "#{session_name}:#{window_index}":
                return f"{self.session}:1\n"
            if fmt == "#{session_name}\t#{window_index}\t#{window_name}":
                return f"{self.session}\t1\t{self._win_of(target)}\n"
            if fmt == "#{window_name}":
                return f"{self._win_of(target)}\n"
            if fmt == "#{window_width}":
                return "200\n"
            if fmt == "#{window_height}":
                return "50\n"
            if fmt == "#{window_zoomed_flag}":
                return "0\n"
            return ""
        if cmd == "split-window":
            target = args[args.index("-t") + 1]
            win = self._win_of(target)
            pane = self._alloc_pane()
            self.windows.setdefault(win, []).append([pane, "", ""])
            return f"{pane}\n"
        if cmd == "list-panes":
            target = args[args.index("-t") + 1]
            win = self._win_of(target)
            if args[-1] == "#{pane_id}\t#{@PANE_ID}":
                return "\n".join(f"{row[0]}\t{row[1]}" for row in self.windows.get(win, []))
            if args[-1] == (
                "#{pane_id}\t#{@PANE_ID}\t#{@PANE_TYPE}\t#{pane_active}\t#{pane_left}\t"
                "#{pane_top}\t#{pane_width}\t#{pane_height}\t#{pane_current_command}\t#{@STACK_PENDING}"
            ):
                lines = []
                for index, row in enumerate(self.windows.get(win, [])):
                    pane, role, pane_type = row
                    if role in {"reservists:civic", "reservists:token-os"}:
                        pane_type = pane_type or "reservists"
                        left = "0" if role == "reservists:civic" else "100"
                        top, width, height, command = "0", "100", "22", "claude"
                    else:
                        left, top, width, height, command = (
                            "0",
                            str(22 + index * 4),
                            "200",
                            "3",
                            "zsh",
                        )
                    lines.append(
                        "\t".join(
                            [pane, role, pane_type, "0", left, top, width, height, command, "false"]
                        )
                    )
                return "\n".join(lines)
            return ""
        if cmd == "set-option" and "-w" in args:
            self.window_options[args[-2]] = args[-1]
        if cmd == "set-option" and "-p" in args:
            target = args[args.index("-t") + 1]
            option, value = args[-2], args[-1]
            for rows in self.windows.values():
                for row in rows:
                    if row[0] == target:
                        if option == "@PANE_ID":
                            row[1] = value
                        elif option == "@PANE_TYPE":
                            row[2] = value
        return ""


def _pane_id_assignments(adapter: FakeStackAdapter) -> list[str]:
    """Every value written to a pane's ``@PANE_ID`` across the run."""
    return [
        cmd[-1]
        for cmd in adapter.commands
        if cmd[0] == "set-option"
        and "-p" in cmd
        and "@PANE_ID" in cmd
        and _base_worker_ordinal(cmd[-1], "reservists") is not None
    ]


def test_two_reservists_new_dispatches_get_distinct_canonical_ids():
    adapter = FakeStackAdapter()

    p1 = add_stack_pane(adapter, "main", "reservists", cwd="/tmp", focus=False)  # type: ignore[arg-type]
    p2 = add_stack_pane(adapter, "main", "reservists", cwd="/tmp", focus=False)  # type: ignore[arg-type]

    role1 = adapter.role_of(p1)
    role2 = adapter.role_of(p2)

    assert role1 == "reservists:1"
    assert role2 == "reservists:2"
    assert role1 != role2
    # Neither pane is stored under the shared default label or the launch alias.
    for role in (role1, role2):
        assert role != "reservists:worker"
        assert not role.endswith(":worker")
        assert "new" not in role


def test_new_and_default_label_never_persisted_as_pane_id():
    adapter = FakeStackAdapter()

    add_stack_pane(adapter, "main", "reservists", cwd="/tmp", focus=False)  # type: ignore[arg-type]
    add_stack_pane(adapter, "main", "reservists", cwd="/tmp", focus=False)  # type: ignore[arg-type]

    assigned = _pane_id_assignments(adapter)
    assert "reservists:worker" not in assigned
    assert "reservists:new" not in assigned
    assert "new" not in assigned
    # Exactly the two distinct real ids were minted.
    assert set(assigned) == {"reservists:1", "reservists:2"}


def test_third_dispatch_reuses_lowest_free_ordinal_after_teardown():
    adapter = FakeStackAdapter()

    p1 = add_stack_pane(adapter, "main", "reservists", cwd="/tmp", focus=False)  # type: ignore[arg-type]
    add_stack_pane(adapter, "main", "reservists", cwd="/tmp", focus=False)  # type: ignore[arg-type]

    # Worker 1 exits: drop its pane from the modeled window state.
    for rows in adapter.windows.values():
        rows[:] = [row for row in rows if row[0] != p1]

    p3 = add_stack_pane(adapter, "main", "reservists", cwd="/tmp", focus=False)  # type: ignore[arg-type]

    # Pinned reservists reflow renumbers workers densely after removal, then the
    # new worker takes the next free ordinal without colliding.
    assert adapter.role_of(p3) == "reservists:2"
    live_roles = {
        row[1]
        for rows in adapter.windows.values()
        for row in rows
        if _base_worker_ordinal(row[1], "reservists") is not None
    }
    assert live_roles == {"reservists:1", "reservists:2"}


def _reservists_pane(pane_id: str, role: str, pane_index: int) -> PaneSnapshot:
    return PaneSnapshot(
        pane_id=pane_id,
        session_name="main",
        window_index=7,
        window_name="reservists",
        pane_index=pane_index,
        width=80,
        height=20,
        current_command="claude",
        tty="/dev/ttys00",
        pane_role=role,
        grid_state=GridState.UNKNOWN,
        pane_kind=PaneKind.UNKNOWN,
        reserved=False,
        active=pane_index == 0,
    )


def _reservists_workspace(role_a: str, role_b: str) -> WorkspaceSnapshot:
    window = WindowSnapshot(
        session_name="main",
        window_index=7,
        window_name="reservists",
        archetype=WindowArchetype.UNKNOWN,
        focused=True,
        grid_expanded="",
        grid_stash="",
        side_expanded="",
        panes=(
            _reservists_pane("%87", role_a, 0),
            _reservists_pane("%88", role_b, 1),
        ),
    )
    return WorkspaceSnapshot(session_name="main", windows=(window,))


def test_talk_brief_resolution_disambiguates_unique_worker_ids():
    workspace = _reservists_workspace("reservists:1", "reservists:2")

    r1 = resolve_pane_in_snapshot(workspace, "reservists:1")
    r2 = resolve_pane_in_snapshot(workspace, "reservists:2")

    assert r1.pane_id == "%87"
    assert r2.pane_id == "%88"
    assert r1.pane_id != r2.pane_id


def test_duplicate_worker_label_is_the_ambiguity_unique_ids_prevent():
    # The pre-fix symptom: both panes share reservists:worker. The resolver now
    # refuses ambiguous labels outright (custodes→malcador misroute hardening),
    # so the duplicate label is loudly unaddressable. Unique ids (asserted
    # above) are what make both workers independently routable.
    workspace = _reservists_workspace("reservists:worker", "reservists:worker")

    with pytest.raises(ValueError, match="ambiguous"):
        resolve_pane_in_snapshot(workspace, "reservists:worker")
