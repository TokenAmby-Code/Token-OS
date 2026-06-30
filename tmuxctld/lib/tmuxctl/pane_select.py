from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from ._stack_core import STACK_PAGE_SPECS
from .focus_guard import allow_human_focus
from .labels import canonical_pane_role
from .stack import stack_base_of
from .tmux_adapter import TmuxAdapter

Direction = str
Mode = str

_DIRECTION_FLAGS: dict[Direction, str] = {
    "left": "-L",
    "right": "-R",
    "up": "-U",
    "down": "-D",
}

_ABSOLUTE_GRID_TARGETS: dict[str, dict[Direction, str]] = {
    "palace": {
        "left": "palace:W",
        "right": "palace:E",
        "up": "palace:N",
        "down": "palace:S",
    },
    "somnium": {
        "left": "somnium:W",
        "right": "somnium:NE",
        "up": "somnium:N",
        "down": "somnium:S",
    },
}

_LEGACY_STACK_FOCUSED_PANE_OPTION = "@LEGION_FOCUSED_PANE"
_STACK_FOCUSED_PANE_OPTION = "@STACK_FOCUSED_PANE"
_ZOOM_RESTORE_PENDING_OPTION = "@PANE_SELECT_ZOOM_RESTORE_PENDING"


@dataclass(frozen=True)
class CurrentPaneContext:
    pane_id: str
    session_name: str
    window_index: str
    window_name: str
    window_base: str
    window_target: str
    pane_role: str
    pane_type: str
    window_zoomed: bool = False


@dataclass(frozen=True)
class StackPaneInfo:
    pane_id: str
    role: str
    pane_type: str
    left: int
    top: int


@contextmanager
def _human_selection_env() -> Iterator[None]:
    """Mark tmux selection in this process as direct human navigation.

    The persistent, client-scoped fact is stored by ``allow_human_focus``.  The
    environment bit only prevents the Python adapter's fail-closed automation
    guard from blocking this explicit UI selection command before tmux hooks can
    observe the client marker.
    """
    old = os.environ.get("IMPERIUM_ALLOW_TMUX_FOCUS")
    os.environ["IMPERIUM_ALLOW_TMUX_FOCUS"] = "1"
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("IMPERIUM_ALLOW_TMUX_FOCUS", None)
        else:
            os.environ["IMPERIUM_ALLOW_TMUX_FOCUS"] = old


def _parse_int(value: str) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


def _window_base(window_name: str) -> str:
    return window_name.split("(", 1)[0]


def _display_args(client: str, fmt: str) -> tuple[str, ...]:
    if client:
        return ("display-message", "-c", client, "-p", fmt)
    return ("display-message", "-p", fmt)


def _current_context(adapter: TmuxAdapter, client: str = "") -> CurrentPaneContext:
    # window_zoomed_flag rides along in the same display-message so the
    # per-keypress hot path does not spend a second tmux subprocess on it.
    raw = adapter.run(
        *_display_args(
            client,
            "#{pane_id}\t#{session_name}\t#{window_index}\t#{window_name}\t#{window_zoomed_flag}",
        ),
        allow_failure=True,
    ).strip()
    zoomed_flag = ""
    if raw.count("\t") != 4:
        # Compatibility for small unit-test fakes and degraded tmux contexts.
        pane_id = adapter.run(*_display_args(client, "#{pane_id}"), allow_failure=True).strip()
        session = adapter.run(*_display_args(client, "#{session_name}"), allow_failure=True).strip()
        index = adapter.run(*_display_args(client, "#{window_index}"), allow_failure=True).strip()
        name = adapter.run(*_display_args(client, "#{window_name}"), allow_failure=True).strip()
    else:
        pane_id, session, index, name, zoomed_flag = raw.split("\t", 4)
    if not pane_id:
        raise ValueError("current pane not found")
    base = stack_base_of(_window_base(name)) or _window_base(name)
    window_target = f"{session}:{index}" if session and index else name
    if not zoomed_flag:
        zoomed_flag = "1" if _window_zoomed(adapter, window_target) else "0"
    return CurrentPaneContext(
        pane_id=pane_id,
        session_name=session,
        window_index=index,
        window_name=name,
        window_base=base,
        window_target=window_target,
        pane_role=canonical_pane_role(adapter.show_pane_option(pane_id, "@PANE_ID")),
        pane_type=adapter.show_pane_option(pane_id, "@PANE_TYPE"),
        window_zoomed=zoomed_flag == "1",
    )


def _window_zoomed(adapter: TmuxAdapter, window_target: str) -> bool:
    return (
        adapter.run(
            "display-message",
            "-t",
            window_target,
            "-p",
            "#{window_zoomed_flag}",
            allow_failure=True,
        ).strip()
        == "1"
    )


def _set_zoom_restore_pending(adapter: TmuxAdapter, window_target: str, value: bool) -> None:
    if value:
        adapter.run(
            "set-option",
            "-w",
            "-t",
            window_target,
            _ZOOM_RESTORE_PENDING_OPTION,
            "true",
            allow_failure=True,
        )
    else:
        adapter.run(
            "set-option",
            "-wu",
            "-t",
            window_target,
            _ZOOM_RESTORE_PENDING_OPTION,
            allow_failure=True,
        )


def _reexpand_if_needed(
    adapter: TmuxAdapter,
    *,
    was_zoomed: bool,
    window_target: str,
    pane_id: str,
) -> None:
    if not was_zoomed:
        return
    if _window_zoomed(adapter, window_target):
        return
    with _human_selection_env():
        adapter.run("resize-pane", "-Z", "-t", pane_id, allow_failure=True)


def _list_stack_panes(adapter: TmuxAdapter, window_target: str) -> list[StackPaneInfo]:
    fmt = "\t".join(
        [
            "#{pane_id}",
            "#{@PANE_ID}",
            "#{@PANE_TYPE}",
            "#{pane_left}",
            "#{pane_top}",
        ]
    )
    panes: list[StackPaneInfo] = []
    for line in adapter.run(
        "list-panes", "-t", window_target, "-F", fmt, allow_failure=True
    ).splitlines():
        parts = line.split("\t")
        if len(parts) < 5:
            continue
        pane_id, role, pane_type, left, top = parts[:5]
        panes.append(
            StackPaneInfo(
                pane_id=pane_id,
                role=canonical_pane_role(role),
                pane_type=pane_type,
                left=_parse_int(left),
                top=_parse_int(top),
            )
        )
    return panes


def _is_stack_worker(pane: StackPaneInfo, base: str) -> bool:
    if pane.pane_type == "stack-worker":
        return True
    if not pane.role.startswith(f"{base}:"):
        return False
    slot = pane.role.rsplit(":", 1)[1]
    return slot == "worker" or slot.isdigit()


def _stable_target_for(pane: StackPaneInfo, panes: list[StackPaneInfo]) -> str:
    if pane.role:
        duplicates = sum(1 for candidate in panes if candidate.role == pane.role)
        if duplicates == 1:
            return pane.role
    return pane.pane_id


def _stored_focused_worker(
    adapter: TmuxAdapter,
    window_target: str,
    panes: list[StackPaneInfo],
    base: str,
) -> StackPaneInfo | None:
    stored = adapter.show_window_option(window_target, _STACK_FOCUSED_PANE_OPTION)
    if not stored:
        stored = adapter.show_window_option(window_target, _LEGACY_STACK_FOCUSED_PANE_OPTION)
    if not stored:
        return None
    for pane in panes:
        if pane.pane_id == stored and _is_stack_worker(pane, base):
            return pane
    return None


def _uppermost_worker(panes: list[StackPaneInfo], base: str) -> StackPaneInfo | None:
    workers = [pane for pane in panes if _is_stack_worker(pane, base)]
    if not workers:
        return None
    return min(workers, key=lambda pane: (pane.top, pane.left, pane.pane_id))


def _lowermost_worker(panes: list[StackPaneInfo], base: str) -> StackPaneInfo | None:
    workers = [pane for pane in panes if _is_stack_worker(pane, base)]
    if not workers:
        return None
    return max(workers, key=lambda pane: (pane.top, pane.left, pane.pane_id))


def _focused_or_uppermost_worker_target(adapter: TmuxAdapter, ctx: CurrentPaneContext) -> str:
    panes = _list_stack_panes(adapter, ctx.window_target)
    worker = _stored_focused_worker(adapter, ctx.window_target, panes, ctx.window_base)
    if worker is None:
        worker = _uppermost_worker(panes, ctx.window_base)
    return _stable_target_for(worker, panes) if worker else ""


def _persona_roles_for_base(base: str) -> tuple[str, ...]:
    spec = STACK_PAGE_SPECS.get(base)
    if spec is None:
        return ()
    return (spec.orchestrator_role, *(persona.role for persona in spec.secondary_personas))


def _persona_panes(panes: list[StackPaneInfo], base: str) -> list[StackPaneInfo]:
    roles = set(_persona_roles_for_base(base))
    return [pane for pane in panes if pane.role in roles]


def _topmost_persona(panes: list[StackPaneInfo], base: str) -> StackPaneInfo | None:
    personas = _persona_panes(panes, base)
    if not personas:
        return None
    return min(personas, key=lambda pane: (pane.top, pane.left, pane.pane_id))


def _bottommost_persona(panes: list[StackPaneInfo], base: str) -> StackPaneInfo | None:
    personas = _persona_panes(panes, base)
    if not personas:
        return None
    return max(personas, key=lambda pane: (pane.top, pane.left, pane.pane_id))


def _is_persona_pane(ctx: CurrentPaneContext) -> bool:
    if ctx.pane_type in STACK_PAGE_SPECS:
        return True
    return ctx.pane_role in _persona_roles_for_base(ctx.window_base)


def _absolute_target(
    adapter: TmuxAdapter, ctx: CurrentPaneContext, direction: Direction
) -> str | None:
    page_targets = _ABSOLUTE_GRID_TARGETS.get(ctx.window_base)
    if page_targets:
        return page_targets[direction]

    if ctx.window_base not in STACK_PAGE_SPECS:
        return ""
    panes = _list_stack_panes(adapter, ctx.window_target)
    if direction == "left":
        persona = _topmost_persona(panes, ctx.window_base)
    elif direction == "right":
        persona = _bottommost_persona(panes, ctx.window_base)
    elif direction == "up":
        persona = _uppermost_worker(panes, ctx.window_base)
    else:
        persona = _lowermost_worker(panes, ctx.window_base)
    return _stable_target_for(persona, panes) if persona else None


def _relative_special_target(
    adapter: TmuxAdapter, ctx: CurrentPaneContext, direction: Direction
) -> str:
    if direction != "right":
        return ""
    if ctx.window_base not in STACK_PAGE_SPECS:
        return ""
    if not _is_persona_pane(ctx):
        return ""
    return _focused_or_uppermost_worker_target(adapter, ctx)


def _select_target(adapter: TmuxAdapter, ctx: CurrentPaneContext, target: str) -> str:
    was_zoomed = ctx.window_zoomed
    pane_id = adapter.run(
        "display-message", "-t", target, "-p", "#{pane_id}", allow_failure=True
    ).strip()
    if was_zoomed:
        _set_zoom_restore_pending(adapter, ctx.window_target, True)
    try:
        with _human_selection_env():
            if was_zoomed:
                adapter.run("select-pane", "-Z", "-t", target)
            else:
                adapter.run("select-pane", "-t", target)
        pane_id = (
            pane_id
            or adapter.run(
                "display-message", "-t", target, "-p", "#{pane_id}", allow_failure=True
            ).strip()
        )
        _reexpand_if_needed(
            adapter,
            was_zoomed=was_zoomed,
            window_target=ctx.window_target,
            pane_id=pane_id or target,
        )
    finally:
        if was_zoomed:
            _set_zoom_restore_pending(adapter, ctx.window_target, False)
    return pane_id or target


def _select_relative(adapter: TmuxAdapter, ctx: CurrentPaneContext, direction: Direction) -> str:
    was_zoomed = ctx.window_zoomed
    if was_zoomed:
        _set_zoom_restore_pending(adapter, ctx.window_target, True)
    try:
        with _human_selection_env():
            if was_zoomed:
                adapter.run("select-pane", "-Z", "-t", ctx.pane_id, _DIRECTION_FLAGS[direction])
            else:
                adapter.run("select-pane", "-t", ctx.pane_id, _DIRECTION_FLAGS[direction])
        pane_id = adapter.run(
            "display-message", "-t", ctx.window_target, "-p", "#{pane_id}", allow_failure=True
        ).strip()
        _reexpand_if_needed(
            adapter,
            was_zoomed=was_zoomed,
            window_target=ctx.window_target,
            pane_id=pane_id or ctx.pane_id,
        )
    finally:
        if was_zoomed:
            _set_zoom_restore_pending(adapter, ctx.window_target, False)
    return pane_id or ctx.pane_id


def select_pane(
    adapter: TmuxAdapter,
    *,
    mode: Mode,
    direction: Direction,
    client: str = "",
) -> str:
    """Select a pane for the explicit tmux pane-select key table."""
    if mode not in {"absolute", "relative"}:
        raise ValueError(f"invalid pane-select mode: {mode}")
    if direction not in _DIRECTION_FLAGS:
        raise ValueError(f"invalid pane-select direction: {direction}")

    allow_human_focus(
        adapter,
        client=client,
        reason=f"pane-select-{mode}-{direction}",
        actor="tmuxctl pane-select",
    )
    ctx = _current_context(adapter, client=client)

    target: str | None = ""
    if mode == "absolute":
        target = _absolute_target(adapter, ctx, direction)
    else:
        target = _relative_special_target(adapter, ctx, direction)

    if target:
        _select_target(adapter, ctx, target)
        return f"pane-select {mode} {direction}: {target}"
    if target is None:
        return f"pane-select {mode} {direction}: noop"

    _select_relative(adapter, ctx, direction)
    return f"pane-select {mode} {direction}: relative"
