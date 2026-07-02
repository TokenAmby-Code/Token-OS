"""Daemon-native pane occupancy/liveness ledger.

This module is the single tmuxctl source of truth for dispatch seat availability:
occupancy is derived from live tmux pane state plus the process-tree liveness
oracle, never from Token-API registry rows.  It is deliberately small and
stdlib-only so both the tmuxctld daemon handlers and the in-process service paths
consume the same ledger instead of growing split-brain guards.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from .singleton_labels import canonical_singleton_label, is_persona_singleton_label
from .tmux_adapter import TmuxAdapter

# Cold-start boot grace for dispatch seat availability.  A worker pane is born
# (its `@PANE_BORN` epoch is stamped) a beat BEFORE its agent process becomes
# observable to the liveness oracle and well before its SessionStart writes the
# `@INSTANCE_ID` bind-stamp.  Under fleet load — and for codex especially — that
# window is seconds long.  A pane inside it has neither a live agent nor an
# instance stamp yet, so the naive ``instance_id or live_agent or singleton``
# test reads it as FREE and a concurrent dispatch can select+clobber the worker
# coming to life there.  Treat a just-born pane as occupied until it ages past
# the grace: availability keys on tmux liveness/birth, never on the bind-stamp
# landing.  Mirrors ``assertions.STACK_WORKER_BOOT_GRACE_SECONDS``; override for
# slower cold starts with ``TMUXCTL_DISPATCH_BOOT_GRACE_SECONDS`` (0 disables).
_DEFAULT_BOOT_GRACE_SECONDS = 30.0


def _boot_grace_seconds() -> float:
    raw = os.environ.get("TMUXCTL_DISPATCH_BOOT_GRACE_SECONDS")
    if not raw:
        return _DEFAULT_BOOT_GRACE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_BOOT_GRACE_SECONDS
    return value if value >= 0 else _DEFAULT_BOOT_GRACE_SECONDS


def _recently_born(born_raw: str) -> bool:
    """True while a pane is still inside its post-birth boot grace.

    Fails CLOSED toward occupied: a future-stamped birth (host clock skew) reads
    as just-born, never as 'long past grace'.
    """
    grace = _boot_grace_seconds()
    if grace <= 0:
        return False
    born_raw = (born_raw or "").strip()
    if not born_raw:
        return False
    try:
        born = float(born_raw)
    except ValueError:
        return False
    age = time.time() - born
    if age < 0:
        return True
    return age < grace


@dataclass(frozen=True)
class PaneOccupancy:
    pane_id: str
    pane_role: str
    window_name: str
    pane_pid: int | None
    instance_id: str
    live_agent: bool
    recently_born: bool = False

    @property
    def singleton(self) -> bool:
        return is_persona_singleton_label(self.pane_role)

    @property
    def occupied(self) -> bool:
        # Stamps are advisory for occupancy.  Live process liveness and singleton
        # labels are sufficient to exclude a pane even when @INSTANCE_ID is empty,
        # stale, or contaminated.  A just-born pane (still inside boot grace) is
        # also excluded: its agent is cold-starting, so it is occupied even though
        # neither a live process nor the @INSTANCE_ID bind-stamp is observable yet.
        return bool(self.instance_id) or self.live_agent or self.singleton or self.recently_born

    @property
    def dispatch_available(self) -> bool:
        # A pane is dispatch-available iff it is not occupied. Occupancy is derived
        # purely from the daemon ledger signals (instance stamp, live agent,
        # singleton label, boot grace) — the retired @PANE_CLEAN "clean" stamp is
        # no longer consulted.
        return not self.occupied


def _parse_pid(raw: str) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _active_agent(pane_pid: int | None) -> bool:
    # Lazy import avoids the historical custodes.py -> stack.py -> _stack_core.py
    # cycle while still using the shared process-tree oracle.
    from .custodes import active_agent_in_pane

    return active_agent_in_pane(pane_pid) is not None


def scan_pane_occupancy(adapter: TmuxAdapter) -> list[PaneOccupancy]:
    """Return the live occupancy ledger for every pane in tmux.

    One tmux scan supplies pane labels/stamps/pids; process liveness is resolved
    through the shared Claude/Codex subtree oracle.  No DB rows participate.
    """
    raw = adapter.run(
        "list-panes",
        "-a",
        "-F",
        "\t".join(
            [
                "#{pane_id}",
                "#{@INSTANCE_ID}",
                "#{@PANE_ID}",
                "#{window_name}",
                "#{pane_pid}",
                "#{@PANE_BORN}",
            ]
        ),
        allow_failure=True,
    )
    ledger: list[PaneOccupancy] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        # 6 columns from live tmux; tolerate the 5-column legacy form (and unit
        # fakes) by defaulting an absent @PANE_BORN to empty (no boot grace).
        if len(parts) not in (5, 6):
            continue
        pane_id, instance_id, pane_role, window_name, pane_pid_raw = parts[:5]
        born_raw = parts[5] if len(parts) == 6 else ""
        role = canonical_singleton_label(pane_role.strip()) if pane_role.strip() else ""
        pane_pid = _parse_pid(pane_pid_raw)
        ledger.append(
            PaneOccupancy(
                pane_id=pane_id,
                pane_role=role,
                window_name=window_name.strip(),
                pane_pid=pane_pid,
                instance_id=instance_id.strip(),
                live_agent=_active_agent(pane_pid),
                recently_born=_recently_born(born_raw),
            )
        )
    return ledger


def occupancy_for_pane(adapter: TmuxAdapter, pane: str) -> PaneOccupancy | None:
    """Resolve one pane and return its occupancy, or None if it vanished."""
    try:
        resolved = adapter._resolve_pane_target_arg(pane)
    except Exception:
        resolved = pane
    raw = adapter.run(
        "display-message",
        "-t",
        resolved,
        "-p",
        "\t".join(
            [
                "#{pane_id}",
                "#{@INSTANCE_ID}",
                "#{@PANE_ID}",
                "#{window_name}",
                "#{pane_pid}",
                "#{@PANE_BORN}",
            ]
        ),
        allow_failure=True,
    ).strip()
    if not raw:
        return None
    parts = raw.split("\t")
    # 6 columns from live tmux; tolerate the 5-column legacy form (and unit fakes)
    # by defaulting an absent @PANE_BORN to empty (no boot grace).
    if len(parts) not in (5, 6):
        return None
    pane_id, instance_id, pane_role, window_name, pane_pid_raw = parts[:5]
    born_raw = parts[5] if len(parts) == 6 else ""
    pane_pid = _parse_pid(pane_pid_raw)
    return PaneOccupancy(
        pane_id=pane_id,
        pane_role=canonical_singleton_label(pane_role.strip()) if pane_role.strip() else "",
        window_name=window_name.strip(),
        pane_pid=pane_pid,
        instance_id=instance_id.strip(),
        live_agent=_active_agent(pane_pid),
        recently_born=_recently_born(born_raw),
    )


def assert_dispatch_target_available(adapter: TmuxAdapter, pane: str) -> PaneOccupancy:
    """Fail closed unless pane is safe for dispatch launcher bytes."""
    occupancy = occupancy_for_pane(adapter, pane)
    if occupancy is None:
        raise ValueError(f"pane target not found: {pane}")
    if occupancy.singleton:
        raise ValueError(
            f"dispatch target is protected singleton seat: {occupancy.pane_role or occupancy.pane_id}"
        )
    if occupancy.instance_id:
        raise ValueError(f"dispatch target is occupied: @INSTANCE_ID={occupancy.instance_id}")
    if occupancy.live_agent:
        raise ValueError(
            f"dispatch target has live Claude/Codex agent: pane_pid={occupancy.pane_pid}"
        )
    return occupancy


def looks_like_dispatch_launcher_payload(text: str) -> bool:
    value = (text or "").strip()
    if value == "clear":
        return True
    return "dispatch-agent." in value or "TOKEN_API_INTERNAL_DISPATCH=1" in value
