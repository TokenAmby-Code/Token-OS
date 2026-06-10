from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from .labels import canonical_pane_role
from .stack import stack_base_of
from .tmux_adapter import TmuxAdapter

CUSTODES_ROLE = "legion:custodes"
FABRICATOR_ROLE = "mechanicus:fabricator-general"
REGIMENT_ROLE = "legion:worker"
STACK_COLLAPSED_HEIGHT = 3
STACK_ORCHESTRATOR_RATIO = 40
STACK_FOCUS_GUARD_OPTION = "@STACK_FOCUS_GUARD"
STACK_FOCUSED_PANE_OPTION = "@STACK_FOCUSED_PANE"
LEGACY_FOCUS_GUARD_OPTION = "@LEGION_FOCUS_GUARD"
LEGACY_FOCUSED_PANE_OPTION = "@LEGION_FOCUSED_PANE"
PANE_SELECT_ZOOM_RESTORE_PENDING_OPTION = "@PANE_SELECT_ZOOM_RESTORE_PENDING"
SHELL_COMMANDS = {"bash", "zsh", "sh", "fish"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PersonaPaneSpec:
    role: str
    pane_type: str


@dataclass(frozen=True)
class StackPageSpec:
    base: str
    orchestrator_role: str
    orchestrator_type: str
    worker_role: str
    worker_type: str = "stack-worker"
    orchestrator_ratio: int = STACK_ORCHESTRATOR_RATIO
    secondary_personas: tuple[PersonaPaneSpec, ...] = ()


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
        secondary_personas=(PersonaPaneSpec("mechanicus:admin", "mechanicus"),),
    ),
}

LEGACY_WORKER_ROLES = {"legion:regiment"}


@dataclass(frozen=True)
class StackPane:
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


def _stack_window_option(adapter: TmuxAdapter, target: str, option: str) -> str:
    value = adapter.show_window_option(target, option)
    if value:
        return value
    if option == STACK_FOCUS_GUARD_OPTION:
        return adapter.show_window_option(target, LEGACY_FOCUS_GUARD_OPTION)
    if option == STACK_FOCUSED_PANE_OPTION:
        return adapter.show_window_option(target, LEGACY_FOCUSED_PANE_OPTION)
    return value


def _window_zoomed(adapter: TmuxAdapter, target: str) -> bool:
    return _show(adapter, target, "#{window_zoomed_flag}") == "1"


def _set_pane_option(adapter: TmuxAdapter, pane: str, option: str, value: str) -> None:
    adapter.run("set-option", "-p", "-t", pane, option, value, allow_failure=True)


def _mechanicus_focus_allowed() -> bool:
    return os.environ.get("IMPERIUM_ALLOW_MECHANICUS_FOCUS") == "1"


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


def _stack_panes(adapter: TmuxAdapter, target: str) -> list[StackPane]:
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
    panes: list[StackPane] = []
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
            StackPane(
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
    panes: list[StackPane],
    spec: StackPageSpec,
) -> tuple[StackPane | None, list[StackPane]]:
    orchestrators = [pane for pane in panes if pane.role == spec.orchestrator_role]
    non_clear = [pane for pane in orchestrators if not pane.clear]
    orchestrator = non_clear[0] if non_clear else (orchestrators[0] if orchestrators else None)

    workers = [
        pane
        for pane in panes
        if pane.pane_id != (orchestrator.pane_id if orchestrator else "")
        and not _is_secondary_persona_role(pane.role, spec)
    ]
    workers.sort(key=lambda pane: (pane.left, pane.top, pane.pane_id))
    return orchestrator, workers


def _secondary_persona_panes(panes: list[StackPane], spec: StackPageSpec) -> dict[str, StackPane]:
    return {pane.role: pane for pane in panes if _is_secondary_persona_role(pane.role, spec)}


def _is_legion_window(window_name: str) -> bool:
    return stack_base_of(_window_base(window_name)) == "legion"


def _is_managed_stack_window(window_name: str) -> bool:
    return _stack_spec_for_window(window_name) is not None


def _tag_orchestrator(adapter: TmuxAdapter, pane: str, spec: StackPageSpec) -> None:
    _set_pane_option(adapter, pane, "@PANE_ID", spec.orchestrator_role)
    _set_pane_option(adapter, pane, "@PANE_TYPE", spec.orchestrator_type)
    _set_pane_option(adapter, pane, "@GRID_STATE", "small")


def _tag_persona(adapter: TmuxAdapter, pane: str, persona: PersonaPaneSpec) -> None:
    _set_pane_option(adapter, pane, "@PANE_ID", persona.role)
    _set_pane_option(adapter, pane, "@PANE_TYPE", persona.pane_type)
    _set_pane_option(adapter, pane, "@GRID_STATE", "small")


def _log_retag(pane: str, old_role: str, new_role: str, reason: str) -> None:
    if old_role == new_role:
        return
    logger.info(
        "stack pane identity retag pane=%s old_role=%s new_role=%s reason=%s",
        pane,
        old_role or "(empty)",
        new_role,
        reason,
    )


def _is_secondary_persona_role(role: str, spec: StackPageSpec) -> bool:
    return any(persona.role == role for persona in spec.secondary_personas)


def _is_worker_role(role: str, spec: StackPageSpec) -> bool:
    prefix, _, suffix = role.partition(":")
    return (
        role == spec.worker_role
        or role.startswith(f"{spec.worker_role}-")
        or (prefix == spec.base and suffix.isdigit() and int(suffix) > 0)
        or role in LEGACY_WORKER_ROLES
    )


def _worker_role(spec: StackPageSpec, ordinal: int) -> str:
    return f"{spec.base}:{ordinal}"


def _worker_ordinal(role: str, spec: StackPageSpec) -> int | None:
    prefix, _, suffix = role.partition(":")
    if prefix == spec.base and suffix.isdigit():
        value = int(suffix)
        return value if value > 0 else None
    if role.startswith(f"{spec.worker_role}-"):
        raw = role.removeprefix(f"{spec.worker_role}-")
        if raw.isdigit():
            value = int(raw)
            return value if value > 0 else None
    return None


def _lowest_available_worker_ordinal(workers: list[StackPane], spec: StackPageSpec) -> int:
    used = {
        value for worker in workers if (value := _worker_ordinal(worker.role, spec)) is not None
    }
    ordinal = 1
    while ordinal in used:
        ordinal += 1
    return ordinal


def _ensure_secondary_persona_panes(
    adapter: TmuxAdapter,
    target: str,
    panes: list[StackPane],
    orchestrator: StackPane,
    spec: StackPageSpec,
) -> list[StackPane]:
    existing = _secondary_persona_panes(panes, spec)
    created: list[StackPane] = []
    if not spec.secondary_personas:
        return created
    win_h = int(_show(adapter, target, "#{window_height}") or "50")
    target_pane = orchestrator.pane_id
    for persona in spec.secondary_personas:
        if persona.role in existing:
            continue
        pane = adapter.run(
            "split-window",
            "-v",
            "-t",
            target_pane,
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-l",
            str(max(1, win_h // (len(spec.secondary_personas) + 1))),
            allow_failure=True,
        ).strip()
        if not pane:
            continue
        logger.info(
            "stack pane identity retag pane=%s old_role=(empty) new_role=%s reason=persona_birth",
            pane,
            persona.role,
        )
        _tag_persona(adapter, pane, persona)
        created.append(StackPane(pane, persona.role, persona.pane_type, False, 0, 0, 0, 0, "zsh"))
        target_pane = pane
    return created


def _dock_secondary_personas_under_orchestrator(
    adapter: TmuxAdapter,
    target: str,
    orchestrator: StackPane,
    spec: StackPageSpec,
    orchestrator_w: int,
) -> None:
    selected_before = _show(adapter, target, "#{pane_id}")
    personas = [
        pane
        for pane in _stack_panes(adapter, target)
        if _is_secondary_persona_role(pane.role, spec)
    ]
    if not personas:
        return
    for pane in personas:
        if pane.left == orchestrator.left and pane.width == orchestrator_w:
            continue
        adapter.run(
            "join-pane",
            "-v",
            "-s",
            pane.pane_id,
            "-t",
            orchestrator.pane_id,
            allow_failure=True,
        )
    win_h = int(_show(adapter, target, "#{window_height}") or "0")
    persona_h = max(1, (win_h - len(personas)) // (len(personas) + 1)) if win_h else 1
    adapter.run(
        "resize-pane", "-t", orchestrator.pane_id, "-x", str(orchestrator_w), allow_failure=True
    )
    adapter.run("resize-pane", "-t", orchestrator.pane_id, "-y", str(persona_h), allow_failure=True)
    for pane in personas:
        adapter.run(
            "resize-pane", "-t", pane.pane_id, "-x", str(orchestrator_w), allow_failure=True
        )
    if selected_before and not (spec.base == "mechanicus" and not _mechanicus_focus_allowed()):
        adapter.run("select-pane", "-t", selected_before, allow_failure=True)


def _tag_worker(
    adapter: TmuxAdapter,
    pane: str,
    spec: StackPageSpec,
    ordinal: int | None = None,
    *,
    old_role: str = "",
    reason: str = "worker_identity",
) -> None:
    new_role = _worker_role(spec, ordinal or 1)
    _log_retag(pane, old_role, new_role, reason)
    _set_pane_option(adapter, pane, "@PANE_ID", new_role)
    _refresh_worker_metadata(adapter, pane, spec)


def _refresh_worker_metadata(adapter: TmuxAdapter, pane: str, spec: StackPageSpec) -> None:
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
        return f"noop stack focus {pane}: not a legion window"

    target = f"{session}:{window_index}"
    if _stack_window_option(adapter, target, STACK_FOCUS_GUARD_OPTION) == "true":
        return f"noop stack focus {pane}: guarded"

    panes = _stack_panes(adapter, target)
    spec = _stack_spec_for_window(window_name)
    if spec is None:
        return f"noop stack focus {pane}: not a managed stack window"
    selected = next((row for row in panes if row.pane_id == pane), None)
    if selected is None:
        return f"noop stack focus {pane}: pane not in window"
    if selected.role == spec.orchestrator_role:
        return f"noop stack focus {pane}: custodes"

    return enforce_stack_layout(adapter, target, focused_pane=pane, focus=True)


def enforce_stack_page_layout(
    adapter: TmuxAdapter,
    target: str,
    *,
    focused_pane: str = "",
    focus: bool = False,
) -> str:
    return enforce_stack_layout(adapter, target, focused_pane=focused_pane, focus=focus)


def enforce_stack_layout(
    adapter: TmuxAdapter,
    target: str,
    *,
    focused_pane: str = "",
    focus: bool = False,
    admit: bool = False,
    kill_pending_clear: bool = False,
) -> str:
    from .focus_guard import preserve_focus

    with preserve_focus(
        adapter,
        source="tmuxctl stack enforce",
        attempted_target=focused_pane or target,
        enabled=os.environ.get("IMPERIUM_ALLOW_TMUX_FOCUS") != "1",
    ):
        return _enforce_stack_layout_impl(
            adapter,
            target,
            focused_pane=focused_pane,
            focus=focus,
            admit=admit,
            kill_pending_clear=kill_pending_clear,
        )


def _enforce_stack_layout_impl(
    adapter: TmuxAdapter,
    target: str,
    *,
    focused_pane: str = "",
    focus: bool = False,
    admit: bool = False,
    kill_pending_clear: bool = False,
) -> str:
    window_name = _show(adapter, target, "#{window_name}")
    spec = _stack_spec_for_window(window_name)
    if spec is None:
        return f"noop stack layout {target}: unsupported window {window_name}"
    if _stack_window_option(adapter, target, STACK_FOCUS_GUARD_OPTION) == "true":
        return f"noop stack layout {target}: guarded"

    panes = _stack_panes(adapter, target)
    if not panes:
        raise ValueError(f"{spec.base} window has no panes: {target}")

    if focus and focused_pane:
        selected = next((pane for pane in panes if pane.pane_id == focused_pane), None)
        if selected and (
            selected.role == spec.orchestrator_role
            or _is_secondary_persona_role(selected.role, spec)
        ):
            return f"noop stack focus {focused_pane}: persona pane"
        if (
            selected
            and _is_worker_role(selected.role, spec)
            and _stack_window_option(adapter, target, STACK_FOCUSED_PANE_OPTION) == focused_pane
        ):
            return f"noop stack focus {focused_pane}: already focused"

    if _window_zoomed(adapter, target):
        return f"noop stack layout {target}: window zoomed"
    if _stack_window_option(adapter, target, PANE_SELECT_ZOOM_RESTORE_PENDING_OPTION) == "true":
        return f"noop stack layout {target}: pane-select zoom restore pending"

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
            pane
            for pane in panes
            if pane.pane_id != orchestrator.pane_id
            and not pane.clear
            and not _is_worker_role(pane.role, spec)
            and not _is_secondary_persona_role(pane.role, spec)
        ]
        if len(untyped_live) == 1:
            _tag_worker(
                adapter,
                orchestrator.pane_id,
                spec,
                old_role=orchestrator.role,
                reason="orchestrator_blank_demoted",
            )
            _tag_orchestrator(adapter, untyped_live[0].pane_id, spec)
            panes = _stack_panes(adapter, target)
            orchestrator, workers = _orchestrator_and_workers(panes, spec)

    if orchestrator is not None:
        personas = _ensure_secondary_persona_panes(adapter, target, panes, orchestrator, spec)
        if personas:
            panes = _stack_panes(adapter, target)
            orchestrator, workers = _orchestrator_and_workers(panes, spec)
        try:
            from .assertions import assert_persona

            assert_persona(adapter, spec.orchestrator_role)
            for persona_spec in spec.secondary_personas:
                assert_persona(adapter, persona_spec.role)
        except Exception as exc:  # layout should still normalize if Token-API is down
            logger.warning("stack persona assertion failed target=%s: %s", target, exc)

    assigned_ordinals = {
        value for worker in workers if (value := _worker_ordinal(worker.role, spec)) is not None
    }

    def claim_ordinal() -> int:
        ordinal = 1
        while ordinal in assigned_ordinals:
            ordinal += 1
        assigned_ordinals.add(ordinal)
        return ordinal

    for worker in list(workers):
        if worker.role == spec.orchestrator_role and worker.clear:
            ordinal = claim_ordinal()
            _tag_worker(
                adapter,
                worker.pane_id,
                spec,
                ordinal,
                old_role=worker.role,
                reason="clear_orchestrator_worker",
            )
            worker = StackPane(
                worker.pane_id,
                _worker_role(spec, ordinal),
                spec.worker_type,
                worker.active,
                worker.left,
                worker.top,
                worker.width,
                worker.height,
                worker.command,
                worker.pending,
            )
        elif not _is_worker_role(worker.role, spec):
            ordinal = claim_ordinal()
            _tag_worker(
                adapter,
                worker.pane_id,
                spec,
                ordinal,
                old_role=worker.role,
                reason="untyped_worker",
            )
            worker = StackPane(
                worker.pane_id,
                _worker_role(spec, ordinal),
                spec.worker_type,
                worker.active,
                worker.left,
                worker.top,
                worker.width,
                worker.height,
                worker.command,
                worker.pending,
            )
        elif _worker_ordinal(worker.role, spec) is None:
            ordinal = claim_ordinal()
            _tag_worker(
                adapter,
                worker.pane_id,
                spec,
                ordinal,
                old_role=worker.role,
                reason="legacy_worker_label",
            )
            worker = StackPane(
                worker.pane_id,
                _worker_role(spec, ordinal),
                spec.worker_type,
                worker.active,
                worker.left,
                worker.top,
                worker.width,
                worker.height,
                worker.command,
                worker.pending,
            )
        elif worker.pane_type not in {spec.worker_type, "legion"}:
            _refresh_worker_metadata(adapter, worker.pane_id, spec)
        if admit and worker.clear:
            _set_pane_option(adapter, worker.pane_id, "@STACK_PENDING", "true")
            worker = StackPane(
                worker.pane_id,
                spec.worker_role,
                spec.worker_type,
                worker.active,
                worker.left,
                worker.top,
                worker.width,
                worker.height,
                worker.command,
                True,
            )
        if worker.clear and not (worker.pending and not kill_pending_clear):
            try:
                from .assertions import assert_instance

                assert_instance(adapter, worker.pane_id, prune=True)
            except Exception:
                adapter.run("kill-pane", "-t", worker.pane_id, allow_failure=True)
        elif not worker.clear and worker.pending:
            _clear_pending(adapter, worker.pane_id)

    panes = _stack_panes(adapter, target)
    orchestrator, workers = _orchestrator_and_workers(panes, spec)
    if orchestrator is None:
        raise ValueError(f"{spec.base} window must contain {spec.orchestrator_role}")
    if not workers:
        _tag_orchestrator(adapter, orchestrator.pane_id, spec)
        win_w = int(_show(adapter, target, "#{window_width}") or "0")
        orchestrator_w = max(1, (win_w * spec.orchestrator_ratio) // 100)
        _dock_secondary_personas_under_orchestrator(
            adapter, target, orchestrator, spec, orchestrator_w
        )
        return f"normalized stack layout {target}: orchestrator only"

    win_w = int(_show(adapter, target, "#{window_width}") or "0")
    win_h = int(_show(adapter, target, "#{window_height}") or "0")
    orchestrator_w = max(1, (win_w * spec.orchestrator_ratio) // 100)
    worker_ids = {pane.pane_id for pane in workers}
    stored_focus = _stack_window_option(adapter, target, STACK_FOCUSED_PANE_OPTION)
    effective_focus = ""
    if focus and focused_pane in worker_ids:
        effective_focus = focused_pane
    elif stored_focus in worker_ids:
        effective_focus = stored_focus

    _set_window_option(adapter, target, STACK_FOCUS_GUARD_OPTION, "true")
    try:
        _tag_orchestrator(adapter, orchestrator.pane_id, spec)
        assigned_ordinals = {
            value for worker in workers if (value := _worker_ordinal(worker.role, spec)) is not None
        }
        for worker in workers:
            ordinal = _worker_ordinal(worker.role, spec)
            if ordinal is None:
                ordinal = 1
                while ordinal in assigned_ordinals:
                    ordinal += 1
                assigned_ordinals.add(ordinal)
                _tag_worker(
                    adapter,
                    worker.pane_id,
                    spec,
                    ordinal,
                    old_role=worker.role,
                    reason="missing_numeric_worker_label",
                )
            else:
                _refresh_worker_metadata(adapter, worker.pane_id, spec)
        adapter.run(
            "set-window-option",
            "-t",
            target,
            "main-pane-width",
            str(orchestrator_w),
            allow_failure=True,
        )
        adapter.run("select-layout", "-t", target, "main-vertical", allow_failure=True)
        adapter.run(
            "resize-pane", "-t", orchestrator.pane_id, "-x", str(orchestrator_w), allow_failure=True
        )
        _dock_secondary_personas_under_orchestrator(
            adapter, target, orchestrator, spec, orchestrator_w
        )

        if effective_focus:
            collapsed = [pane for pane in workers if pane.pane_id != effective_focus]
            expanded_h = max(
                STACK_COLLAPSED_HEIGHT,
                win_h - (len(collapsed) * (STACK_COLLAPSED_HEIGHT + 1)),
            )
            for worker in collapsed:
                adapter.run(
                    "resize-pane",
                    "-t",
                    worker.pane_id,
                    "-y",
                    str(STACK_COLLAPSED_HEIGHT),
                    allow_failure=True,
                )
            adapter.run(
                "resize-pane", "-t", effective_focus, "-y", str(expanded_h), allow_failure=True
            )
            if focus and focused_pane in worker_ids:
                _set_window_option(adapter, target, STACK_FOCUSED_PANE_OPTION, focused_pane)
    finally:
        _set_window_option(adapter, target, STACK_FOCUS_GUARD_OPTION, "false")

    if focus and focused_pane in worker_ids:
        return f"focused stack {focused_pane} in {target}"
    return f"normalized stack layout {target}"


def add_orchestrator_stack_pane(
    adapter: TmuxAdapter,
    session: str,
    base: str,
    *,
    cwd: str | None = None,
    focus: bool = True,
) -> str:
    spec = STACK_PAGE_SPECS.get(base)
    if spec is None:
        raise ValueError(f"not a managed orchestrator stack: {base}")
    cwd = cwd or os.path.expanduser("~")
    window = base
    target = f"{session}:{window}"
    names = [
        name.split("(", 1)[0]
        for name in adapter.run(
            "list-windows", "-t", session, "-F", "#{window_name}", allow_failure=True
        ).splitlines()
    ]
    if window not in names:
        adapter.run("new-window", "-t", session, "-n", window, "-d", "-c", cwd)

    panes = _stack_panes(adapter, target)
    orchestrator, workers = _orchestrator_and_workers(panes, spec)
    if orchestrator is None:
        first = _show(adapter, target, "#{pane_id}")
        _tag_orchestrator(adapter, first, spec)
        orchestrator = StackPane(
            first, spec.orchestrator_role, spec.orchestrator_type, False, 0, 0, 0, 0, "zsh"
        )

    _set_window_option(adapter, target, STACK_FOCUS_GUARD_OPTION, "true")
    try:
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
            worker_ids = {worker.pane_id for worker in workers}
            stored_focus = _stack_window_option(adapter, target, STACK_FOCUSED_PANE_OPTION)
            split_target = stored_focus if stored_focus in worker_ids else workers[0].pane_id
            pane = adapter.run(
                "split-window",
                "-v",
                "-t",
                split_target,
                "-d",
                "-P",
                "-F",
                "#{pane_id}",
                "-l",
                str(STACK_COLLAPSED_HEIGHT),
                "-c",
                cwd,
            ).strip()
    finally:
        _set_window_option(adapter, target, STACK_FOCUS_GUARD_OPTION, "false")

    _tag_worker(
        adapter,
        pane,
        spec,
        _lowest_available_worker_ordinal(workers, spec),
        reason="worker_birth",
    )
    _set_pane_option(adapter, pane, "@STACK_PENDING", "true")
    adapter.run("select-pane", "-T", "regiment", "-t", pane, allow_failure=True)
    focus_new_worker = focus and not (base == "mechanicus" and not _mechanicus_focus_allowed())
    enforce_stack_layout(adapter, target, focused_pane=pane, focus=focus_new_worker)
    return pane


def add_stack_worker_pane(
    adapter: TmuxAdapter,
    session: str,
    *,
    cwd: str | None = None,
    window: str = "legion",
    focus: bool = True,
) -> str:
    return add_orchestrator_stack_pane(adapter, session, window, cwd=cwd, focus=focus)
