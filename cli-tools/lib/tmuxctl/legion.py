from __future__ import annotations

import os
from dataclasses import dataclass

from .labels import canonical_pane_role
from .stack import stack_base_of
from .tmux_adapter import TmuxAdapter

CUSTODES_ROLE = "legion:custodes"
FABRICATOR_ROLE = "mechanicus:fabricator-general"
REGIMENT_ROLE = "legion:worker"
LEGION_COLLAPSED_HEIGHT = 3
LEGION_CUSTODES_RATIO = 40
SHELL_COMMANDS = {"bash", "zsh", "sh", "fish"}


@dataclass(frozen=True)
class StackPageSpec:
    base: str
    orchestrator_role: str
    orchestrator_type: str
    worker_role: str
    worker_type: str = "stack-worker"
    orchestrator_ratio: int = LEGION_CUSTODES_RATIO


STACK_PAGE_SPECS: dict[str, StackPageSpec] = {
    "legion": StackPageSpec(
        base="legion",
        orchestrator_role=CUSTODES_ROLE,
        orchestrator_type="legion",
        worker_role=REGIMENT_ROLE,
    ),
    "mechanicus": StackPageSpec(
        base="mechanicus",
        orchestrator_role=FABRICATOR_ROLE,
        orchestrator_type="mechanicus",
        worker_role="mechanicus:worker",
    ),
}

LEGACY_WORKER_ROLES = {"legion:regiment"}


@dataclass(frozen=True)
class LegionPane:
    pane_id: str
    role: str
    pane_type: str
    active: bool
    left: int
    top: int
    width: int
    height: int
    command: str
    pending: bool = False

    @property
    def clear(self) -> bool:
        return self.command in SHELL_COMMANDS


def _show(adapter: TmuxAdapter, target: str, fmt: str) -> str:
    return adapter.run("display-message", "-t", target, "-p", fmt, allow_failure=True).strip()


def _set_window_option(adapter: TmuxAdapter, target: str, option: str, value: str) -> None:
    adapter.run("set-option", "-w", "-t", target, option, value, allow_failure=True)


def _set_pane_option(adapter: TmuxAdapter, pane: str, option: str, value: str) -> None:
    adapter.run("set-option", "-p", "-t", pane, option, value, allow_failure=True)


def _window_base(name: str) -> str:
    return name.split("(", 1)[0]


def _pane_window(adapter: TmuxAdapter, pane: str) -> tuple[str, str, str]:
    raw = _show(adapter, pane, "#{session_name}\t#{window_index}\t#{window_name}")
    if not raw or "\t" not in raw:
        raise ValueError(f"pane not found: {pane}")
    session, window_index, window_name = raw.split("\t", 2)
    return session, window_index, window_name


def _target_for_pane(adapter: TmuxAdapter, pane: str) -> str:
    session, window_index, _ = _pane_window(adapter, pane)
    return f"{session}:{window_index}"


def _legion_panes(adapter: TmuxAdapter, target: str) -> list[LegionPane]:
    fmt = "\t".join(
        [
            "#{pane_id}",
            "#{@PANE_ID}",
            "#{@PANE_TYPE}",
            "#{pane_active}",
            "#{pane_left}",
            "#{pane_top}",
            "#{pane_width}",
            "#{pane_height}",
            "#{pane_current_command}",
            "#{@STACK_PENDING}",
        ]
    )
    panes: list[LegionPane] = []
    for line in adapter.run("list-panes", "-t", target, "-F", fmt, allow_failure=True).splitlines():
        parts = line.split("\t")
        if len(parts) == 5:
            # Compatibility for older unit-test fakes.
            pane_id, role, active, top, height = parts
            pane_type, left, width, command = "", "0", "0", "claude"
            pending = "false"
        else:
            if len(parts) == 9:
                pane_id, role, pane_type, active, left, top, width, height, command = parts
                pending = "false"
            else:
                pane_id, role, pane_type, active, left, top, width, height, command, pending = parts
        panes.append(
            LegionPane(
                pane_id=pane_id,
                role=canonical_pane_role(role),
                pane_type=pane_type,
                active=active == "1",
                left=int(left or 0),
                top=int(top or 0),
                width=int(width or 0),
                height=int(height or 0),
                command=command,
                pending=pending == "true",
            )
        )
    return panes


def _stack_spec_for_window(window_name: str) -> StackPageSpec | None:
    return STACK_PAGE_SPECS.get(stack_base_of(_window_base(window_name)))


def _orchestrator_and_workers(
    panes: list[LegionPane],
    spec: StackPageSpec,
) -> tuple[LegionPane | None, list[LegionPane]]:
    orchestrators = [pane for pane in panes if pane.role == spec.orchestrator_role]
    non_clear = [pane for pane in orchestrators if not pane.clear]
    orchestrator = non_clear[0] if non_clear else (orchestrators[0] if orchestrators else None)

    workers = [pane for pane in panes if pane.pane_id != (orchestrator.pane_id if orchestrator else "")]
    workers.sort(key=lambda pane: (pane.left, pane.top, pane.pane_id))
    return orchestrator, workers


def _is_legion_window(window_name: str) -> bool:
    return stack_base_of(_window_base(window_name)) == "legion"


def _is_managed_stack_window(window_name: str) -> bool:
    return _stack_spec_for_window(window_name) is not None


def _tag_orchestrator(adapter: TmuxAdapter, pane: str, spec: StackPageSpec) -> None:
    _set_pane_option(adapter, pane, "@PANE_ID", spec.orchestrator_role)
    _set_pane_option(adapter, pane, "@PANE_TYPE", spec.orchestrator_type)
    _set_pane_option(adapter, pane, "@GRID_STATE", "small")


def _tag_worker(adapter: TmuxAdapter, pane: str, spec: StackPageSpec) -> None:
    _set_pane_option(adapter, pane, "@PANE_ID", spec.worker_role)
    _set_pane_option(adapter, pane, "@PANE_TYPE", spec.worker_type)
    _set_pane_option(adapter, pane, "@GRID_STATE", "small")


def _clear_pending(adapter: TmuxAdapter, pane: str) -> None:
    adapter.run("set-option", "-pu", "-t", pane, "@STACK_PENDING", allow_failure=True)


def focus_selected(adapter: TmuxAdapter, pane: str) -> str:
    """Make the selected legion regiment the only expanded right-side pane.

    Selecting Custodes intentionally does nothing. This function is idempotent
    and guarded so tmux hooks can call it on every pane selection.
    """
    session, window_index, window_name = _pane_window(adapter, pane)
    if not _is_legion_window(window_name):
        return f"noop legion focus {pane}: not a legion window"

    target = f"{session}:{window_index}"
    if adapter.show_window_option(target, "@LEGION_FOCUS_GUARD") == "true":
        return f"noop legion focus {pane}: guarded"

    panes = _legion_panes(adapter, target)
    spec = _stack_spec_for_window(window_name)
    if spec is None:
        return f"noop legion focus {pane}: not a managed stack window"
    selected = next((row for row in panes if row.pane_id == pane), None)
    if selected is None:
        return f"noop legion focus {pane}: pane not in window"
    if selected.role == spec.orchestrator_role:
        return f"noop legion focus {pane}: custodes"

    return enforce_stack_layout(adapter, target, focused_pane=pane)


def enforce_legion_layout(adapter: TmuxAdapter, target: str, *, focused_pane: str = "") -> str:
    return enforce_stack_layout(adapter, target, focused_pane=focused_pane)


def enforce_stack_layout(adapter: TmuxAdapter, target: str, *, focused_pane: str = "") -> str:
    window_name = _show(adapter, target, "#{window_name}")
    spec = _stack_spec_for_window(window_name)
    if spec is None:
        return f"noop stack layout {target}: unsupported window {window_name}"

    panes = _legion_panes(adapter, target)
    if not panes:
        raise ValueError(f"{spec.base} window has no panes: {target}")

    orchestrator, workers = _orchestrator_and_workers(panes, spec)
    if orchestrator is None:
        if len(panes) == 1:
            orchestrator = panes[0]
            _tag_orchestrator(adapter, orchestrator.pane_id, spec)
            workers = []
        else:
            raise ValueError(f"{spec.base} window must contain {spec.orchestrator_role}")
    elif orchestrator.clear:
        untyped_live = [
            pane for pane in panes
            if pane.pane_id != orchestrator.pane_id
            and not pane.clear
            and pane.role not in {spec.worker_role, *LEGACY_WORKER_ROLES}
        ]
        if len(untyped_live) == 1:
            _tag_worker(adapter, orchestrator.pane_id, spec)
            _tag_orchestrator(adapter, untyped_live[0].pane_id, spec)
            panes = _legion_panes(adapter, target)
            orchestrator, workers = _orchestrator_and_workers(panes, spec)

    for worker in list(workers):
        if worker.role == spec.orchestrator_role and worker.clear:
            _tag_worker(adapter, worker.pane_id, spec)
            worker = LegionPane(
                worker.pane_id,
                spec.worker_role,
                spec.worker_type,
                worker.active,
                worker.left,
                worker.top,
                worker.width,
                worker.height,
                worker.command,
                worker.pending,
            )
        elif worker.role not in {spec.worker_role, *LEGACY_WORKER_ROLES} or worker.pane_type not in {spec.worker_type, "legion"}:
            _tag_worker(adapter, worker.pane_id, spec)
        if worker.clear and not worker.pending:
            adapter.run("kill-pane", "-t", worker.pane_id, allow_failure=True)
        elif not worker.clear and worker.pending:
            _clear_pending(adapter, worker.pane_id)

    panes = _legion_panes(adapter, target)
    orchestrator, workers = _orchestrator_and_workers(panes, spec)
    if orchestrator is None:
        raise ValueError(f"{spec.base} window must contain {spec.orchestrator_role}")
    if not workers:
        _tag_orchestrator(adapter, orchestrator.pane_id, spec)
        return f"normalized {spec.base} layout {target}: orchestrator only"

    if not focused_pane:
        active = next((pane for pane in workers if pane.active), None)
        focused_pane = active.pane_id if active else workers[0].pane_id

    if focused_pane == orchestrator.pane_id:
        focused_pane = workers[0].pane_id
    if focused_pane not in {pane.pane_id for pane in workers}:
        active = next((pane for pane in workers if pane.active), None)
        focused_pane = active.pane_id if active else workers[0].pane_id

    win_w = int(_show(adapter, target, "#{window_width}") or "0")
    win_h = int(_show(adapter, target, "#{window_height}") or "0")
    orchestrator_w = max(1, (win_w * spec.orchestrator_ratio) // 100)
    collapsed = [pane for pane in workers if pane.pane_id != focused_pane]
    expanded_h = max(LEGION_COLLAPSED_HEIGHT, win_h - (len(collapsed) * (LEGION_COLLAPSED_HEIGHT + 1)))

    _set_window_option(adapter, target, "@LEGION_FOCUS_GUARD", "true")
    try:
        _tag_orchestrator(adapter, orchestrator.pane_id, spec)
        for worker in workers:
            _tag_worker(adapter, worker.pane_id, spec)
        adapter.run("select-pane", "-t", orchestrator.pane_id, allow_failure=True)
        adapter.run("set-window-option", "-t", target, "main-pane-width", str(orchestrator_w), allow_failure=True)
        adapter.run("select-layout", "-t", target, "main-vertical", allow_failure=True)
        adapter.run("resize-pane", "-t", orchestrator.pane_id, "-x", str(orchestrator_w), allow_failure=True)

        for worker in collapsed:
            adapter.run(
                "resize-pane",
                "-t",
                worker.pane_id,
                "-y",
                str(LEGION_COLLAPSED_HEIGHT),
                allow_failure=True,
            )
        adapter.run("resize-pane", "-t", focused_pane, "-y", str(expanded_h), allow_failure=True)
        adapter.run("select-pane", "-t", focused_pane, allow_failure=True)
        _set_window_option(adapter, target, "@LEGION_FOCUSED_PANE", focused_pane)
    finally:
        _set_window_option(adapter, target, "@LEGION_FOCUS_GUARD", "false")

    return f"focused {spec.base} {focused_pane} in {target}"


def add_orchestrator_stack_pane(
    adapter: TmuxAdapter,
    session: str,
    base: str,
    *,
    cwd: str | None = None,
) -> str:
    spec = STACK_PAGE_SPECS.get(base)
    if spec is None:
        raise ValueError(f"not a managed orchestrator stack: {base}")
    cwd = cwd or os.path.expanduser("~")
    window = base
    target = f"{session}:{window}"
    names = [
        name.split("(", 1)[0]
        for name in adapter.run("list-windows", "-t", session, "-F", "#{window_name}", allow_failure=True).splitlines()
    ]
    if window not in names:
        adapter.run("new-window", "-t", session, "-n", window, "-d", "-c", cwd)

    panes = _legion_panes(adapter, target)
    orchestrator, workers = _orchestrator_and_workers(panes, spec)
    if orchestrator is None:
        first = _show(adapter, target, "#{pane_id}")
        _tag_orchestrator(adapter, first, spec)
        orchestrator = LegionPane(
            first, spec.orchestrator_role, spec.orchestrator_type, False, 0, 0, 0, 0, "zsh"
        )

    if not workers:
        win_w = int(_show(adapter, target, "#{window_width}") or "240")
        right_w = max(1, win_w - ((win_w * spec.orchestrator_ratio) // 100) - 1)
        pane = adapter.run(
            "split-window",
            "-h",
            "-t",
            orchestrator.pane_id,
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-l",
            str(right_w),
            "-c",
            cwd,
        ).strip()
    else:
        focus = adapter.show_window_option(target, "@LEGION_FOCUSED_PANE") or workers[0].pane_id
        pane = adapter.run(
            "split-window",
            "-v",
            "-t",
            focus,
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-l",
            str(LEGION_COLLAPSED_HEIGHT),
            "-c",
            cwd,
        ).strip()

    _tag_worker(adapter, pane, spec)
    _set_pane_option(adapter, pane, "@STACK_PENDING", "true")
    adapter.run("select-pane", "-T", "regiment", "-t", pane, allow_failure=True)
    enforce_stack_layout(adapter, target, focused_pane=pane)
    return pane


def add_regiment_pane(
    adapter: TmuxAdapter,
    session: str,
    *,
    cwd: str | None = None,
    window: str = "legion",
) -> str:
    return add_orchestrator_stack_pane(adapter, session, window, cwd=cwd)
