from __future__ import annotations

import shlex

from .enums import PaneKind
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


def _show(adapter: TmuxAdapter, target: str, fmt: str) -> str:
    return adapter.run("display-message", "-t", target, "-p", fmt).strip()


def _pane_option(adapter: TmuxAdapter, pane_id: str, option: str) -> str:
    return adapter.show_pane_option(pane_id, option)


def _set_pane_option(adapter: TmuxAdapter, pane_id: str, option: str, value: str) -> None:
    adapter.run("set-option", "-p", "-t", pane_id, option, value, allow_failure=True)


def _clear_pane_option(adapter: TmuxAdapter, pane_id: str, option: str) -> None:
    adapter.run("set-option", "-pu", "-t", pane_id, option, allow_failure=True)


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
    rows = adapter.run("list-panes", "-t", audience_target, "-F", fmt, allow_failure=True).splitlines()
    if len(rows) <= 1:
        return
    for line in rows:
        pane_id, role, pane_type, source, command = line.split("\t")
        if role or pane_type or source:
            continue
        if command in {"bash", "zsh", "sh", "fish"}:
            adapter.run("kill-pane", "-t", pane_id, allow_failure=True)


def _select_pane(adapter: TmuxAdapter, pane_id: str) -> None:
    target_window = _show(adapter, pane_id, "#{session_name}:#{window_id}")
    adapter.run("select-window", "-t", target_window, allow_failure=True)
    adapter.run("select-pane", "-t", pane_id, allow_failure=True)


def _native_zoom(adapter: TmuxAdapter, pane_id: str) -> str:
    target_window = _show(adapter, pane_id, "#{session_name}:#{window_index}")
    for opt in ("@GRID_EXPANDED", "@GRID_STASH", "@GENERIC_EXPANDED", "@GENERIC_STASH", "@SIDE_EXPANDED"):
        value = "none" if opt.endswith("EXPANDED") else ""
        adapter.run("set-option", "-w", "-t", target_window, opt, value, allow_failure=True)
    adapter.run("resize-pane", "-Z", "-t", pane_id)
    return f"zoomed unmanaged pane {pane_id}"


def _spawn_tombstone(adapter: TmuxAdapter, source_pane: str, source_role: str, target_pane: str) -> str:
    path = _show(adapter, source_pane, "#{pane_current_path}") or "~"
    tombstone = adapter.run(
        "split-window",
        "-d",
        "-P",
        "-F",
        "#{pane_id}",
        "-t",
        source_pane,
        "-c",
        path,
    ).strip()
    grid_state = _pane_option(adapter, source_pane, "@GRID_STATE") or "small"
    reserved = _pane_option(adapter, source_pane, "@GRID_RESERVED") or "false"
    _set_pane_option(adapter, tombstone, "@PANE_ID", source_role)
    _set_pane_option(adapter, tombstone, "@PANE_TYPE", PaneKind.TOMBSTONE.value)
    _set_pane_option(adapter, tombstone, "@GRID_STATE", grid_state)
    _set_pane_option(adapter, tombstone, "@GRID_RESERVED", reserved)
    _set_pane_option(adapter, tombstone, "@TOMBSTONE_SOURCE", source_role)
    _set_pane_option(adapter, tombstone, "@TOMBSTONE_TARGET", target_pane)
    cmd = f"exec tmux-tombstone {shlex.quote(source_role)} {shlex.quote(target_pane)}"
    adapter.run("send-keys", "-t", tombstone, cmd, "Enter", allow_failure=True)
    return tombstone


def _find_source_tombstone(adapter: TmuxAdapter, real_pane: str, source_role: str) -> str:
    fmt = "#{pane_id}\t#{@PANE_ID}\t#{@PANE_TYPE}\t#{@TOMBSTONE_TARGET}"
    for line in adapter.run("list-panes", "-a", "-F", fmt, allow_failure=True).splitlines():
        pane_id, role, pane_type, target = line.split("\t")
        if role == source_role and pane_type == PaneKind.TOMBSTONE.value:
            if target == real_pane or resolve_pane(adapter, pane_id).pane_id == real_pane:
                return pane_id
    raise ValueError(f"source tombstone not found for {source_role} -> {real_pane}")


def audience_expand(adapter: TmuxAdapter, target: str) -> str:
    resolved = resolve_pane(adapter, target)
    pane_id = resolved.pane_id
    role = _pane_option(adapter, pane_id, "@PANE_ID")
    page = _page_from_role(role)
    if not page or (page not in {"palace", "somnium"} and role not in AUDIENCE_ROLES):
        return _native_zoom(adapter, pane_id)

    session_name = _show(adapter, pane_id, "#{session_name}")
    audience_window = _ensure_audience_window(adapter, session_name, PAGE_AUDIENCE[page])
    tombstone = _spawn_tombstone(adapter, pane_id, role, pane_id)
    adapter.run("swap-pane", "-s", pane_id, "-t", tombstone)
    adapter.run("join-pane", "-d", "-s", pane_id, "-t", f"{audience_window}.1")
    _cleanup_audience_placeholders(adapter, audience_window)
    _set_pane_option(adapter, pane_id, "@TOMBSTONE_SOURCE", role)
    _set_pane_option(adapter, pane_id, "@PANE_ID", f"audience:{role}")
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
    if not source_role:
        raise ValueError(f"pane has no @TOMBSTONE_SOURCE: {pane_id}")
    tombstone = _find_source_tombstone(adapter, pane_id, source_role)
    adapter.run("swap-pane", "-s", pane_id, "-t", tombstone)
    adapter.run("kill-pane", "-t", tombstone, allow_failure=True)
    _clear_pane_option(adapter, pane_id, "@TOMBSTONE_SOURCE")
    _clear_pane_option(adapter, pane_id, "@TOMBSTONE_TARGET")
    _set_pane_option(adapter, pane_id, "@PANE_ID", source_role)
    _select_pane(adapter, pane_id)
    return f"returned {pane_id} to {source_role}"


def audience_toggle(adapter: TmuxAdapter, target: str) -> str:
    pane_id = adapter.run("display-message", "-t", target, "-p", "#{pane_id}").strip()
    pane_type = _pane_option(adapter, pane_id, "@PANE_TYPE")
    window_name = _window_base(_show(adapter, pane_id, "#{window_name}"))

    if pane_type == PaneKind.TOMBSTONE.value:
        return audience_jump(adapter, pane_id)
    if window_name in set(PAGE_AUDIENCE.values()):
        return audience_return(adapter, pane_id)
    return audience_expand(adapter, pane_id)
