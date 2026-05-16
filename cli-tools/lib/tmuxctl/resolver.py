from __future__ import annotations

from dataclasses import dataclass, field

from .enums import GridState, PaneKind
from .labels import canonical_pane_role, indexable_pane_roles, pane_role_aliases
from .models import PaneSnapshot, WindowSnapshot, WorkspaceSnapshot
from .tmux_adapter import TmuxAdapter


@dataclass(frozen=True)
class PaneResolution:
    requested: str
    pane_id: str
    pane_role: str
    pane_kind: PaneKind
    chain: tuple[str, ...] = field(default_factory=tuple)

    @property
    def was_tombstone(self) -> bool:
        return len(self.chain) > 1


def _target_exists(adapter: TmuxAdapter, target: str) -> str:
    return adapter.run(
        "display-message",
        "-t",
        target,
        "-p",
        "#{pane_id}",
        allow_failure=True,
    ).strip()


def _snapshot_from_live(adapter: TmuxAdapter, target: str) -> PaneSnapshot:
    pane_id = _target_exists(adapter, target)
    if not pane_id:
        raise ValueError(f"pane target not found: {target}")

    row = adapter.run(
        "display-message",
        "-t",
        pane_id,
        "-p",
        "\t".join(
            [
                "#{pane_id}",
                "#{session_name}",
                "#{window_index}",
                "#{window_name}",
                "#{pane_index}",
                "#{pane_width}",
                "#{pane_height}",
                "#{pane_current_command}",
                "#{pane_tty}",
                "#{pane_active}",
            ]
        ),
    ).strip()
    (
        live_pane_id,
        session_name,
        window_index,
        window_name,
        pane_index,
        width,
        height,
        command,
        tty,
        active,
    ) = row.split("\t")
    pane_type = adapter.show_pane_option(live_pane_id, "@PANE_TYPE")
    try:
        kind = PaneKind(pane_type)
    except ValueError:
        kind = PaneKind.UNKNOWN
    return PaneSnapshot(
        pane_id=live_pane_id,
        session_name=session_name,
        window_index=int(window_index),
        window_name=window_name,
        pane_index=int(pane_index),
        width=int(width),
        height=int(height),
        current_command=command,
        tty=tty,
        pane_role=canonical_pane_role(adapter.show_pane_option(live_pane_id, "@PANE_ID")),
        grid_state=GridState.UNKNOWN,
        pane_kind=kind,
        reserved=adapter.show_pane_option(live_pane_id, "@GRID_RESERVED") == "true",
        active=active == "1",
        tombstone_target=adapter.show_pane_option(live_pane_id, "@TOMBSTONE_TARGET"),
        tombstone_source=adapter.show_pane_option(live_pane_id, "@TOMBSTONE_SOURCE"),
    )


def _window_base(window_name: str) -> str:
    return window_name.split("(", 1)[0]


def _role_position_aliases(role: str) -> tuple[str, ...]:
    if not role or ":" not in role:
        return ()
    canonical = canonical_pane_role(role)
    position = canonical.rsplit(":", 1)[1]
    aliases = [position]
    for alias in pane_role_aliases(canonical):
        if ":" in alias:
            aliases.append(alias.rsplit(":", 1)[1])
    return tuple(dict.fromkeys(alias for alias in aliases if alias))


def _add_unique(index: dict[str, PaneSnapshot], key: str, pane: PaneSnapshot) -> None:
    if key:
        index.setdefault(key, pane)


def _index_positionals(
    by_positional: dict[str, PaneSnapshot],
    window: WindowSnapshot,
    pane: PaneSnapshot,
) -> None:
    """Index public stable pane addresses for a pane.

    Supported address forms:
      - raw logical role, already handled elsewhere: ``palace:N``
      - window-index position: ``1:N``
      - page-name position: ``palace:N`` / ``somnium:SE``

    The index is derived only from live tmux state and @PANE_ID. Nothing here is
    persisted; it is intended to replace stale stored %pane references at
    runtime.
    """
    if not pane.pane_role:
        return
    window_names = tuple(
        dict.fromkeys(
            str(value)
            for value in (window.window_index, window.window_name, _window_base(window.window_name))
            if str(value)
        )
    )
    for position in _role_position_aliases(pane.pane_role):
        for window_name in window_names:
            _add_unique(by_positional, f"{window_name}:{position}", pane)


def _index_workspace(
    workspace: WorkspaceSnapshot,
) -> tuple[dict[str, PaneSnapshot], dict[str, PaneSnapshot], dict[str, PaneSnapshot]]:
    by_physical: dict[str, PaneSnapshot] = {}
    by_logical: dict[str, PaneSnapshot] = {}
    by_positional: dict[str, PaneSnapshot] = {}
    for window in workspace.windows:
        for pane in window.panes:
            by_physical[pane.pane_id] = pane
            _index_positionals(by_positional, window, pane)
            if pane.pane_role:
                for role in indexable_pane_roles(pane.pane_role):
                    by_logical.setdefault(role, pane)
    return by_physical, by_logical, by_positional


def resolve_pane_in_snapshot(workspace: WorkspaceSnapshot, target: str) -> PaneResolution:
    by_physical, by_logical, by_positional = _index_workspace(workspace)

    def lookup(value: str) -> PaneSnapshot | None:
        if value.startswith("%"):
            return by_physical.get(value)
        canonical = canonical_pane_role(value)
        return (
            by_positional.get(value)
            or by_positional.get(canonical)
            or by_logical.get(value)
            or by_logical.get(canonical)
            or by_physical.get(value)
        )

    current = lookup(target)
    if current is None:
        raise ValueError(f"pane target not found: {target}")

    seen: set[str] = set()
    chain: list[str] = []
    while True:
        marker = current.pane_role or current.pane_id
        if current.pane_id in seen or marker in seen:
            chain.append(marker)
            raise ValueError(f"tombstone cycle detected: {' -> '.join(chain)}")
        seen.add(current.pane_id)
        seen.add(marker)
        chain.append(marker)

        if current.pane_kind is not PaneKind.TOMBSTONE:
            return PaneResolution(
                requested=target,
                pane_id=current.pane_id,
                pane_role=current.pane_role,
                pane_kind=current.pane_kind,
                chain=tuple(chain),
            )

        if not current.tombstone_target:
            raise ValueError(f"tombstone {marker} missing @TOMBSTONE_TARGET")
        next_pane = lookup(current.tombstone_target)
        if next_pane is None:
            raise ValueError(f"tombstone {marker} target not found: {current.tombstone_target}")
        current = next_pane


def resolve_pane(adapter: TmuxAdapter, target: str) -> PaneResolution:
    """Resolve physical ids, @PANE_ID roles, and positional pane addresses.

    Positional addresses are live runtime aliases such as ``1:N``:
    ``<window-index-or-page>:<position>``. They are resolved from tmux state on
    every call and are not cached. The low-level tmux interceptors intentionally
    restrict custom pane-target interception to numeric window indexes and the
    managed page prefixes so tmux window targets like ``session:palace`` pass
    through unchanged.
    """
    if target.startswith("%"):
        first = _snapshot_from_live(adapter, target)
        session_name = first.session_name
    else:
        session_name = adapter.current_session_name()

    from .snapshot import build_workspace_snapshot

    workspace = build_workspace_snapshot(adapter, session_name)
    return resolve_pane_in_snapshot(workspace, target)
