from __future__ import annotations

import shlex
from dataclasses import dataclass

from .enums import PaneKind
from .labels import canonical_pane_role
from .resolver import resolve_pane
from .tmux_adapter import TmuxAdapter

PAGE_AUDIENCE = {
    "palace": "_palace_audience",
    "somnium": "_somnium_audience",
    "legion": "_legion_audience",
    "mechanicus": "_mechanicus_audience",
}

AUDIENCE_ROLES = {
    "legion:custodes",
}


@dataclass(frozen=True)
class PaneContext:
    pane_id: str
    session_name: str
    window_name: str
    pane_role: str
    pane_type: str
    tombstone_source: str
    grid_state: str
    grid_reserved: str
    current_path: str


def _show(adapter: TmuxAdapter, target: str, fmt: str) -> str:
    return adapter.run("display-message", "-t", target, "-p", fmt).strip()


def _pane_context(adapter: TmuxAdapter, target: str) -> PaneContext:
    fmt = "\t".join(
        [
            "#{pane_id}",
            "#{session_name}",
            "#{window_name}",
            "#{@PANE_ID}",
            "#{@PANE_TYPE}",
            "#{@TOMBSTONE_SOURCE}",
            "#{@GRID_STATE}",
            "#{@GRID_RESERVED}",
            "#{pane_current_path}",
        ]
    )
    row = adapter.run("display-message", "-t", target, "-p", fmt, allow_failure=True).strip()
    if not row:
        raise ValueError(f"pane target not found: {target}")
    (
        pane_id,
        session_name,
        window_name,
        pane_role,
        pane_type,
        tombstone_source,
        grid_state,
        grid_reserved,
        current_path,
    ) = row.split("\t", 8)
    return PaneContext(
        pane_id=pane_id,
        session_name=session_name,
        window_name=window_name,
        pane_role=canonical_pane_role(pane_role),
        pane_type=pane_type,
        tombstone_source=tombstone_source,
        grid_state=grid_state,
        grid_reserved=grid_reserved,
        current_path=current_path,
    )


def _pane_option(adapter: TmuxAdapter, pane_id: str, option: str) -> str:
    return adapter.show_pane_option(pane_id, option)


def _set_pane_option(adapter: TmuxAdapter, pane_id: str, option: str, value: str) -> None:
    adapter.run("set-option", "-p", "-t", pane_id, option, value, allow_failure=True)


def _clear_pane_option(adapter: TmuxAdapter, pane_id: str, option: str) -> None:
    adapter.run("set-option", "-pu", "-t", pane_id, option, allow_failure=True)


def _run_tmux_commands(
    adapter: TmuxAdapter,
    commands: list[tuple[str, ...]],
    *,
    allow_failure: bool = True,
) -> None:
    if not commands:
        return
    args: list[str] = []
    for index, command in enumerate(commands):
        if index:
            args.append(";")
        args.extend(command)
    adapter.run(*args, allow_failure=allow_failure)


def _set_pane_options(adapter: TmuxAdapter, pane_id: str, values: dict[str, str]) -> None:
    _run_tmux_commands(
        adapter,
        [("set-option", "-p", "-t", pane_id, option, value) for option, value in values.items()],
    )


def _window_base(name: str) -> str:
    base = name.split("(", 1)[0]
    head, sep, tail = base.rpartition("-")
    if sep and tail.isdigit() and head in PAGE_AUDIENCE:
        return head
    return base


def _page_from_role(role: str) -> str:
    if ":" not in role:
        return ""
    page = role.split(":", 1)[0]
    return page if page in PAGE_AUDIENCE else ""


def _audience_name_for_pane(adapter: TmuxAdapter, pane_id: str) -> str:
    source = _pane_option(adapter, pane_id, "@TOMBSTONE_SOURCE")
    page = _page_from_role(source)
    if not page:
        page = _window_base(_show(adapter, pane_id, "#{window_name}"))
    if page not in PAGE_AUDIENCE:
        raise ValueError(f"pane is not in a managed page: {pane_id}")
    return PAGE_AUDIENCE[page]


def _ensure_audience_window(adapter: TmuxAdapter, session_name: str, audience_name: str) -> str:
    target = f"{session_name}:{audience_name}"
    rows = adapter.run(
        "list-windows",
        "-t",
        session_name,
        "-F",
        "#{window_index}\t#{window_name}",
        allow_failure=True,
    ).splitlines()
    names = [row.split("\t", 1)[1] for row in rows]
    if audience_name not in names:
        last_index = max(int(row.split("\t", 1)[0]) for row in rows) if rows else 0
        adapter.run(
            "new-window",
            "-d",
            "-a",
            "-t",
            f"{session_name}:{last_index}",
            "-n",
            audience_name,
            "-c",
            "~",
        )
    return target


def _cleanup_audience_placeholders(adapter: TmuxAdapter, audience_target: str) -> None:
    fmt = "\t".join(
        [
            "#{pane_id}",
            "#{@PANE_ID}",
            "#{@PANE_TYPE}",
            "#{@TOMBSTONE_SOURCE}",
            "#{pane_current_command}",
        ]
    )
    rows = adapter.run(
        "list-panes", "-t", audience_target, "-F", fmt, allow_failure=True
    ).splitlines()
    if len(rows) <= 1:
        return
    for line in rows:
        pane_id, role, pane_type, source, command = line.split("\t")
        if role or pane_type or source:
            continue
        if command in {"bash", "zsh", "sh", "fish"}:
            adapter.run("kill-pane", "-t", pane_id, allow_failure=True)


def _list_audience_panes(
    adapter: TmuxAdapter, audience_target: str, page: str
) -> list[tuple[str, str]]:
    fmt = "\t".join(
        [
            "#{pane_id}",
            "#{@PANE_ID}",
            "#{@PANE_TYPE}",
            "#{@TOMBSTONE_SOURCE}",
        ]
    )
    rows = adapter.run(
        "list-panes", "-t", audience_target, "-F", fmt, allow_failure=True
    ).splitlines()
    audience_panes: list[tuple[str, str]] = []
    for line in rows:
        pane_id, role, pane_type, source = line.split("\t")
        source_role = canonical_pane_role(source or role.removeprefix("audience:"))
        if pane_type == PaneKind.AUDIENCE.value or role.startswith(f"audience:{page}:"):
            if _page_from_role(source_role) == page:
                audience_panes.append((pane_id, source_role))
    return audience_panes


def _select_pane(adapter: TmuxAdapter, pane_id: str) -> None:
    target_window = _show(adapter, pane_id, "#{session_name}:#{window_id}")
    adapter.run("select-window", "-t", target_window, allow_failure=True)
    adapter.run("select-pane", "-t", pane_id, allow_failure=True)


def _native_zoom(adapter: TmuxAdapter, pane_id: str) -> str:
    target_window = _show(adapter, pane_id, "#{session_name}:#{window_index}")
    for opt in (
        "@GRID_EXPANDED",
        "@GRID_STASH",
        "@GENERIC_EXPANDED",
        "@GENERIC_STASH",
        "@SIDE_EXPANDED",
    ):
        value = "none" if opt.endswith("EXPANDED") else ""
        adapter.run("set-option", "-w", "-t", target_window, opt, value, allow_failure=True)
    adapter.run("resize-pane", "-Z", "-t", pane_id)
    return f"zoomed unmanaged pane {pane_id}"


def _spawn_tombstone(
    adapter: TmuxAdapter,
    source: PaneContext,
    source_role: str,
    target_pane: str,
) -> str:
    path = source.current_path or "~"
    tombstone = adapter.run(
        "split-window",
        "-d",
        "-P",
        "-F",
        "#{pane_id}",
        "-t",
        source.pane_id,
        "-c",
        path,
    ).strip()
    grid_state = source.grid_state or "small"
    reserved = source.grid_reserved or "false"
    _set_pane_options(
        adapter,
        tombstone,
        {
            "@PANE_ID": source_role,
            "@PANE_TYPE": PaneKind.TOMBSTONE.value,
            "@GRID_STATE": grid_state,
            "@GRID_RESERVED": reserved,
            "@TOMBSTONE_SOURCE": source_role,
            "@TOMBSTONE_TARGET": target_pane,
        },
    )
    cmd = f"exec tmux-tombstone {shlex.quote(source_role)} {shlex.quote(target_pane)}"
    adapter.run("send-keys", "-t", tombstone, cmd, "Enter", allow_failure=True)
    return tombstone


def _find_source_tombstone(adapter: TmuxAdapter, real_pane: str, source_role: str) -> str:
    fmt = "#{pane_id}\t#{@PANE_ID}\t#{@PANE_TYPE}\t#{@TOMBSTONE_TARGET}"
    fallback: list[str] = []
    source_role = canonical_pane_role(source_role)
    for line in adapter.run("list-panes", "-a", "-F", fmt, allow_failure=True).splitlines():
        pane_id, role, pane_type, target = line.split("\t")
        if canonical_pane_role(role) == source_role and pane_type == PaneKind.TOMBSTONE.value:
            if target == real_pane:
                return pane_id
            fallback.append(pane_id)
    for pane_id in fallback:
        if resolve_pane(adapter, pane_id).pane_id == real_pane:
            return pane_id
    raise ValueError(f"source tombstone not found for {source_role} -> {real_pane}")


def _restore_audience_pane(
    adapter: TmuxAdapter,
    pane_id: str,
    source_role: str,
    *,
    select: bool,
) -> None:
    if not source_role:
        raise ValueError(f"pane has no @TOMBSTONE_SOURCE: {pane_id}")
    source_role = canonical_pane_role(source_role)
    tombstone = _find_source_tombstone(adapter, pane_id, source_role)
    source_kind = _pane_option(adapter, pane_id, "@AUDIENCE_SOURCE_PANE_TYPE")
    adapter.run("swap-pane", "-s", pane_id, "-t", tombstone)
    adapter.run("kill-pane", "-t", tombstone, allow_failure=True)
    commands: list[tuple[str, ...]] = [
        ("set-option", "-pu", "-t", pane_id, "@TOMBSTONE_SOURCE"),
        ("set-option", "-pu", "-t", pane_id, "@TOMBSTONE_TARGET"),
        ("set-option", "-pu", "-t", pane_id, "@AUDIENCE_SOURCE_PANE_TYPE"),
        ("set-option", "-p", "-t", pane_id, "@PANE_ID", source_role),
    ]
    if source_kind:
        commands.append(("set-option", "-p", "-t", pane_id, "@PANE_TYPE", source_kind))
    else:
        commands.append(("set-option", "-pu", "-t", pane_id, "@PANE_TYPE"))
    _run_tmux_commands(adapter, commands)
    if select:
        _select_pane(adapter, pane_id)


def _clear_audience_slot(
    adapter: TmuxAdapter, audience_window: str, page: str, *, except_pane: str = ""
) -> int:
    restored = 0
    for pane_id, source_role in _list_audience_panes(adapter, audience_window, page):
        if pane_id == except_pane:
            continue
        _restore_audience_pane(adapter, pane_id, source_role, select=False)
        restored += 1
    return restored


def audience_expand(adapter: TmuxAdapter, target: str) -> str:
    context = _pane_context(adapter, target)
    pane_id = context.pane_id
    role = context.pane_role
    page = _page_from_role(role)
    if not page or (page not in {"palace", "somnium"} and role not in AUDIENCE_ROLES):
        return _native_zoom(adapter, pane_id)

    audience_window = _ensure_audience_window(adapter, context.session_name, PAGE_AUDIENCE[page])
    restored = _clear_audience_slot(adapter, audience_window, page, except_pane=pane_id)
    if restored:
        audience_window = _ensure_audience_window(
            adapter, context.session_name, PAGE_AUDIENCE[page]
        )
    tombstone = _spawn_tombstone(adapter, context, role, pane_id)
    adapter.run("swap-pane", "-s", pane_id, "-t", tombstone)
    adapter.run("join-pane", "-d", "-s", pane_id, "-t", f"{audience_window}.1")
    _cleanup_audience_placeholders(adapter, audience_window)
    live_options = {
        "@TOMBSTONE_SOURCE": role,
        "@PANE_ID": f"audience:{role}",
        "@PANE_TYPE": PaneKind.AUDIENCE.value,
    }
    if context.pane_type:
        live_options["@AUDIENCE_SOURCE_PANE_TYPE"] = context.pane_type
    _set_pane_options(adapter, pane_id, live_options)
    if not context.pane_type:
        _clear_pane_option(adapter, pane_id, "@AUDIENCE_SOURCE_PANE_TYPE")
    _clear_pane_option(adapter, pane_id, "@TOMBSTONE_TARGET")
    _select_pane(adapter, pane_id)
    return f"expanded {role} to {PAGE_AUDIENCE[page]} ({pane_id})"


def audience_jump(adapter: TmuxAdapter, target: str) -> str:
    resolved = resolve_pane(adapter, target)
    _select_pane(adapter, resolved.pane_id)
    chain = " -> ".join(resolved.chain)
    return f"selected {resolved.pane_id}" + (f" via {chain}" if chain else "")


def audience_return(adapter: TmuxAdapter, target: str) -> str:
    resolved = resolve_pane(adapter, target)
    pane_id = resolved.pane_id
    source_role = _pane_option(adapter, pane_id, "@TOMBSTONE_SOURCE")
    _restore_audience_pane(adapter, pane_id, source_role, select=True)
    return f"returned {pane_id} to {source_role}"


def audience_toggle(adapter: TmuxAdapter, target: str) -> str:
    context = _pane_context(adapter, target)
    pane_id = context.pane_id
    pane_type = context.pane_type
    window_name = _window_base(context.window_name)

    if pane_type == PaneKind.TOMBSTONE.value:
        return audience_jump(adapter, pane_id)
    if window_name in set(PAGE_AUDIENCE.values()):
        return audience_return(adapter, pane_id)
    return audience_expand(adapter, pane_id)
