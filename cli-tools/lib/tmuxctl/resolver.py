from __future__ import annotations

from dataclasses import dataclass, field

from .enums import GridState, PaneKind
from .labels import canonical_pane_role, indexable_pane_roles
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


DEPRECATED_PUBLIC_POSITIONS = {"TL", "TR", "BL", "BR", "NW", "SW"}


def _is_deprecated_public_target(target: str) -> bool:
    if target.startswith("%") or ":" not in target:
        return False
    return target.rsplit(":", 1)[1] in DEPRECATED_PUBLIC_POSITIONS


def _role_position_aliases(role: str) -> tuple[str, ...]:
    if not role or ":" not in role:
        return ()
    canonical = canonical_pane_role(role)
    if canonical.endswith(":custodes") or canonical.endswith(":fabricator-general"):
        return tuple(dict.fromkeys((canonical.rsplit(":", 1)[1], "0")))
    position = canonical.rsplit(":", 1)[1]
    return (position,) if position else ()


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
        if _is_deprecated_public_target(value):
            # No explicit legacy rejection: old position labels are simply not
            # part of the public address space, so they miss the canonical index.
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


def resolve_to_public(adapter: TmuxAdapter, target: str) -> str:
    resolved = resolve_pane(adapter, target)
    if not resolved.pane_role:
        raise ValueError(f"pane target has no public @PANE_ID: {target}")
    return canonical_pane_role(resolved.pane_role)


def resolve_to_physical(adapter: TmuxAdapter, target: str) -> str:
    return resolve_pane(adapter, target).pane_id


@dataclass(frozen=True)
class InstanceResolution:
    """Live resolution of an agent instance UUID to its tmux pane.

    The association is read purely from tmux: the agent's pane self-identifies
    via the ``@INSTANCE_ID`` user option (stamped at registration, unset on
    teardown). There is no DB involvement — when the agent process ends, the
    stamp is gone and ``found`` is False. This is the fail-closed primitive that
    replaces stored ``tmux_pane``/``pane_label`` columns.
    """

    instance_id: str
    pane_id: str | None
    pane_role: str | None

    @property
    def found(self) -> bool:
        return self.pane_id is not None


def _instance_pane_index(adapter: TmuxAdapter) -> dict[str, tuple[str, str]]:
    """Single global tmux scan → {instance_id: (pane_id, canonical_role)}.

    Panes without an ``@INSTANCE_ID`` stamp are skipped. The role is
    canonicalized; an unset ``@PANE_ID`` yields an empty role string.
    """
    raw = adapter.run(
        "list-panes",
        "-a",
        "-F",
        "\t".join(["#{pane_id}", "#{@INSTANCE_ID}", "#{@PANE_ID}"]),
        allow_failure=True,
    )
    index: dict[str, tuple[str, str]] = {}
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        pane_id, instance_id, pane_role = parts
        instance_id = instance_id.strip()
        if not instance_id:
            continue
        role = canonical_pane_role(pane_role.strip()) if pane_role.strip() else ""
        # First writer wins; a UUID should only ever stamp one live pane, but if
        # geometry is mid-move prefer the earliest enumerated pane deterministically.
        index.setdefault(instance_id, (pane_id, role))
    return index


def resolve_instance(adapter: TmuxAdapter, instance_id: str) -> InstanceResolution:
    """Resolve an instance UUID to its live pane via a single global tmux scan.

    Fails closed: if no live pane carries ``@INSTANCE_ID == instance_id`` the
    result has ``pane_id is None`` (``found`` is False). Pure tmux, no DB.
    """
    pane_id, role = _instance_pane_index(adapter).get(instance_id, (None, None))
    return InstanceResolution(
        instance_id=instance_id,
        pane_id=pane_id,
        pane_role=role or None,
    )
