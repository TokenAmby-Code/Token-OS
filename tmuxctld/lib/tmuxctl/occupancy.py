"""Daemon-native pane occupancy/liveness gates.

This module is the single tmuxctl source of truth for dispatch seat availability
and comms delivery safety. Allocation walks live tmux pane identities plus the
wrapper→pane ledger first and does not sniff process trees until one candidate is
selected. Delivery gates then cross-check that selected pane with one process
sniff; any wrapper-ledger/sniff disagreement is a loud P0, never a fallback.
Token-API registry rows do not participate.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

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


def _parse_ledger_identity_parts(parts: list[str]) -> tuple[str, str, str, int | None, str] | None:
    """Parse a pane identity row without instance-stamp authority.

    Live callers request the 5-column row
    ``pane_id, @PANE_ID, window_name, pane_pid, @PANE_BORN``.  Some older unit
    fakes still return the retired occupancy shape
    ``pane_id, @INSTANCE_ID, @PANE_ID, window_name, pane_pid[, @PANE_BORN]``.
    Tolerate those fakes, but return only the positional role and tmux pid; the
    legacy instance stamp is intentionally discarded.
    """

    if len(parts) == 4:
        pane_id, pane_role, window_name, pane_pid_raw = parts
        born_raw = ""
    elif len(parts) == 5:
        if _parse_pid(parts[3]) is not None or parts[3].strip() == "":
            pane_id, pane_role, window_name, pane_pid_raw, born_raw = parts
        else:
            pane_id, _instance_id, pane_role, window_name, pane_pid_raw = parts
            born_raw = ""
    elif len(parts) == 6:
        pane_id, _instance_id, pane_role, window_name, pane_pid_raw, born_raw = parts
    else:
        return None
    role = canonical_singleton_label(pane_role.strip()) if pane_role.strip() else ""
    return pane_id, role, window_name.strip(), _parse_pid(pane_pid_raw), born_raw


def _active_agent(pane_pid: int | None) -> bool:
    # Lazy import avoids the historical custodes.py -> stack.py -> _stack_core.py
    # cycle while still using the shared process-tree oracle.
    from .custodes import active_agent_in_pane

    return active_agent_in_pane(pane_pid) is not None


def _active_wrapper_row_for_role(pane_role: str) -> dict[str, Any] | None:
    """Return the active wrapper-ledger row occupying ``pane_role``, if any.

    The wrapper→pane ledger is the delivery/occupancy authority for managed
    agents.  Tmux process sniffing is deliberately kept out of this lookup so
    allocation can walk ledger occupancy first and sniff only the selected pane.
    """

    role = canonical_singleton_label(pane_role.strip()) if pane_role.strip() else ""
    if not role:
        return None
    try:
        from .wrapper_ledger import LEDGER

        row = LEDGER.resolve(pane_positional_id=role)
    except Exception as exc:
        raise ValueError(f"wrapper ledger occupancy lookup failed for {role}") from exc
    return row.as_dict() if row is not None else None


def _pane_row(
    adapter: TmuxAdapter,
    pane: str,
    *,
    resolve: bool = True,
) -> tuple[str, str, str, int | None, str] | None:
    """Read one pane's identity row without process sniffing."""

    target = pane
    if resolve:
        try:
            target = adapter._resolve_pane_target_arg(pane)
        except Exception:
            target = pane
    raw = adapter.run(
        "display-message",
        "-t",
        target,
        "-p",
        "\t".join(
            [
                "#{pane_id}",
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
    return _parse_ledger_identity_parts(raw.split("\t"))


@dataclass(frozen=True)
class LedgerPaneOccupancy:
    pane_id: str
    pane_role: str
    window_name: str
    pane_pid: int | None
    ledger_row: dict[str, Any] | None
    sniff_live_agent: bool
    recently_born: bool = False

    @property
    def singleton(self) -> bool:
        return is_persona_singleton_label(self.pane_role)

    @property
    def ledger_occupied(self) -> bool:
        return self.ledger_row is not None


def ledger_occupancy_for_pane(adapter: TmuxAdapter, pane: str) -> LedgerPaneOccupancy | None:
    """Ledger-first occupancy for exactly one pane, then one process sniff.

    This is the belt-and-suspenders check: first resolve the pane's positional
    identity and consult the wrapper→pane ledger, then perform a single live
    process-tree sniff for that same selected pane.  Callers must treat any
    disagreement as a P0 infrastructure failure, never as a fallback signal.
    """

    row = _pane_row(adapter, pane)
    if row is None:
        return None
    pane_id, pane_role, window_name, pane_pid, born_raw = row
    return LedgerPaneOccupancy(
        pane_id=pane_id,
        pane_role=pane_role,
        window_name=window_name,
        pane_pid=pane_pid,
        ledger_row=_active_wrapper_row_for_role(pane_role),
        sniff_live_agent=_active_agent(pane_pid),
        recently_born=_recently_born(born_raw),
    )


def _p0_incongruency(occ: LedgerPaneOccupancy, *, purpose: str) -> ValueError:
    return ValueError(
        "P0_LEDGER_SNIFF_INCONGRUENCY "
        f"purpose={purpose} pane={occ.pane_role or occ.pane_id} "
        f"ledger_occupied={str(occ.ledger_occupied).lower()} "
        f"sniff_live_agent={str(occ.sniff_live_agent).lower()}"
    )


def assert_comms_delivery_target_occupied(
    adapter: TmuxAdapter,
    pane: str,
) -> LedgerPaneOccupancy:
    """Fail closed unless ``pane`` is an occupied managed-agent delivery target."""

    occ = ledger_occupancy_for_pane(adapter, pane)
    if occ is None:
        raise ValueError(f"pane target not found: {pane}")
    if occ.ledger_occupied != occ.sniff_live_agent:
        raise _p0_incongruency(occ, purpose="comms_delivery")
    if not occ.ledger_occupied:
        raise ValueError(
            "ledger_unoccupied: refusing non-delivery into blank/unoccupied pane "
            f"{occ.pane_role or occ.pane_id}"
        )
    return occ


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


def scan_ledger_dispatch_availability(adapter: TmuxAdapter) -> list[PaneOccupancy]:
    """Return dispatch availability from the wrapper ledger without ps sniffing.

    This is the allocator's first pass.  It walks every live pane once, consults
    only the wrapper→pane ledger (plus singleton/boot-grace structural guards),
    and intentionally does not call :func:`_active_agent`.  The selected free
    candidate is later cross-checked by :func:`assert_dispatch_target_available`.
    """

    raw = adapter.run(
        "list-panes",
        "-a",
        "-F",
        "\t".join(
            [
                "#{pane_id}",
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
        parsed = _parse_ledger_identity_parts(line.split("\t"))
        if parsed is None:
            continue
        pane_id, role, window_name, pane_pid, born_raw = parsed
        ledger_row = _active_wrapper_row_for_role(role)
        ledger.append(
            PaneOccupancy(
                pane_id=pane_id,
                pane_role=role,
                window_name=window_name.strip(),
                pane_pid=pane_pid,
                instance_id=str((ledger_row or {}).get("instance_id") or ""),
                live_agent=False,
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
    """Fail closed unless pane is safe for dispatch launcher bytes.

    Availability is ledger-first: a dispatch target must be unoccupied in the
    wrapper→pane ledger, then a single process sniff of that selected pane must
    agree it is empty.  Any ledger/sniff disagreement is a loud P0 failure.
    """

    ledger_occ = ledger_occupancy_for_pane(adapter, pane)
    if ledger_occ is None:
        raise ValueError(f"pane target not found: {pane}")
    if ledger_occ.singleton:
        raise ValueError(
            f"dispatch target is protected singleton seat: {ledger_occ.pane_role or ledger_occ.pane_id}"
        )
    if ledger_occ.ledger_occupied != ledger_occ.sniff_live_agent:
        raise _p0_incongruency(ledger_occ, purpose="dispatch_allocation")
    if ledger_occ.ledger_occupied:
        instance_id = str((ledger_occ.ledger_row or {}).get("instance_id") or "")
        detail = f": ledger instance_id={instance_id}" if instance_id else ""
        raise ValueError(
            f"dispatch target is occupied in wrapper ledger{detail}"
        )
    return PaneOccupancy(
        pane_id=ledger_occ.pane_id,
        pane_role=ledger_occ.pane_role,
        window_name=ledger_occ.window_name,
        pane_pid=ledger_occ.pane_pid,
        instance_id="",
        live_agent=False,
        recently_born=ledger_occ.recently_born,
    )


def looks_like_dispatch_launcher_payload(text: str) -> bool:
    value = (text or "").strip()
    if value == "clear":
        return True
    return "dispatch-agent." in value or "TOKEN_API_INTERNAL_DISPATCH=1" in value
