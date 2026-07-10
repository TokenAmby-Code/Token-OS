"""Billable-vs-personal work classification + the fractional x/y accrual model.

Pure logic, no runtime I/O (only `os.path.expanduser` at import to resolve HOME).

The TimerEngine historically had ONE work pool + ONE signed break balance and a
binary "human working / not working" productivity flag. That conflates two
genuinely different kinds of work:

  - **billable** (Civic / askCivic): the literal day job. Paid, on-the-clock,
    should run during billable hours, eventually tracked as a real time-card.
  - **personal** (Imperium / Token-OS): this agent system and personal infra.
    Real work, helps future-him, but NOT paid — today it masquerades as "work"
    in the same pool.
  - **unknown**: anything we cannot confidently place.

Billable-vs-personal is a TAG on each instance's contribution, not a separate
segmented subsystem: pool membership falls out of the tag. See the session doc
``Mars/Sessions/break-accrual-true-work-split.md`` and the memory note
``[[billable-vs-personal-infra-work-pools]]``.

This module owns two things:
  1. ``classify_work_class(working_dir, legion)`` — the tag (working_dir is the
     reliable signal; ``legion`` is an explicit override).
  2. The pure x/y accrual math (``accrual_weight``, ``trickle_numerator``) — the
     fractional work-vs-distraction model. These are wired into the live
     shadow accounting (descriptive) and are the Phase-2 enforcement primitives
     (flagged off) — see the design doc.
"""

from __future__ import annotations

import os
from enum import Enum

# Class keys used as plain strings across the timer/accounting boundary so the
# pure TimerEngine never has to import this module.
CLASS_KEYS: tuple[str, str, str] = ("billable", "personal", "unknown")


class WorkClass(str, Enum):
    BILLABLE = "billable"  # Civic / askCivic — on the clock, paid
    PERSONAL = "personal"  # Imperium / Token-OS — personal infra, unpaid
    UNKNOWN = "unknown"


_HOME = os.path.expanduser("~")

# Billable path prefixes. The askCivic worktree parent lives UNDER $HOME
# (~/worktrees/askCivic), so it MUST be matched before the generic personal
# home prefix below — order is load-bearing in classify_work_class.
BILLABLE_PATH_PREFIXES: tuple[str, ...] = (
    "/Volumes/Civic",
    os.path.join(_HOME, "worktrees", "askCivic"),
)

# Personal path prefixes. Home is personal *unless* it is an askCivic worktree
# (billable check runs first).
PERSONAL_PATH_PREFIXES: tuple[str, ...] = (
    "/Volumes/Imperium",
    _HOME,
)

# Legion overrides. `civic` is the day-job legion (Civic Initiatives > askCivic
# > Pax). The rest are personal-infra legions of the Imperium fleet.
BILLABLE_LEGIONS: frozenset[str] = frozenset({"civic", "pax"})
PERSONAL_LEGIONS: frozenset[str] = frozenset(
    {"mechanicus", "custodes", "astartes", "fabricator", "administratum"}
)


def _path_has_prefix(path: str, prefix: str) -> bool:
    """True if `path` is `prefix` or sits beneath it (boundary-safe)."""
    if not path or not prefix:
        return False
    return path == prefix or path.startswith(prefix.rstrip(os.sep) + os.sep)


# Fleet-queue domain prefixes: cwd under any of these = the askCivic system
# (the cockpit's RIGHT rails). askPax is civic-side tooling, so it rides along.
ASKCIVIC_DOMAIN_PREFIXES: tuple[str, ...] = (
    "/Volumes/Civic",
    os.path.join(_HOME, "worktrees", "askCivic"),
    os.path.join(_HOME, "worktrees", "askPax"),
)

# The two fleet-queue domains. Plain strings across the API boundary — the
# cockpit contract carries the enum value, never a raw path.
DOMAIN_KEYS: tuple[str, str] = ("token-os", "askcivic")


def classify_domain(working_dir: str | None) -> str:
    """Fleet-queue domain oracle: which worker system an instance belongs to.

    Deliberately NOT classify_work_class: binary (no 'unknown'), cwd-only (no
    legion override), and it fails toward 'token-os' — the home fleet is the
    default LEFT system, so a null/foreign cwd must never file a worker onto
    the civic side. This function is the single seam the incoming hardware
    split (the new work PC) will replace with a machine oracle.
    """
    wd = os.path.normpath(working_dir.strip()) if working_dir and working_dir.strip() else ""
    for pref in ASKCIVIC_DOMAIN_PREFIXES:
        if _path_has_prefix(wd, os.path.normpath(pref)):
            return "askcivic"
    return "token-os"


def classify_work_class(working_dir: str | None, legion: str | None = None) -> WorkClass:
    """Tag a contribution billable / personal / unknown.

    Precedence (first match wins):
      1. working_dir under a billable path (Civic / askCivic worktree)
      2. legion is a billable legion (explicit civic/pax)
      3. working_dir under a personal path (Imperium / home)
      4. legion is a personal-infra legion
      5. unknown

    Billable-by-path and billable-by-legion both beat personal so an explicit
    civic agent working out of an Imperium checkout still reads as on-the-clock.
    """
    wd = os.path.normpath(working_dir.strip()) if working_dir and working_dir.strip() else ""
    legion_l = (legion or "").strip().lower()

    for pref in BILLABLE_PATH_PREFIXES:
        if _path_has_prefix(wd, os.path.normpath(pref)):
            return WorkClass.BILLABLE

    if legion_l in BILLABLE_LEGIONS:
        return WorkClass.BILLABLE

    for pref in PERSONAL_PATH_PREFIXES:
        if _path_has_prefix(wd, os.path.normpath(pref)):
            return WorkClass.PERSONAL

    if legion_l in PERSONAL_LEGIONS:
        return WorkClass.PERSONAL

    return WorkClass.UNKNOWN


def accrual_weight(active_count: int) -> float:
    """Sub-linear multi-instance work weight (the x numerator multiplier).

    Pillar 1 of the model: every active instance contributes, and N concurrent
    provably-working instances should cumulatively outweigh a single distraction
    timer — but not let you farm break by spawning idle agents. Diminishing
    returns: 0->0, 1->1.0, 2->2.0, 4->3.0, 8->4.0 (1 + log2 n for n>=1).

    Phase-2 enforcement primitive — currently used only for the descriptive
    x/y preview surfaced in the cockpit, NOT for the live break balance.
    """
    if active_count <= 0:
        return 0.0
    if active_count == 1:
        return 1.0
    # 1 + log2(n): bounded, monotonic, strongly diminishing.
    import math

    return 1.0 + math.log2(active_count)


def trickle_numerator(x_work: float, y_distraction: float) -> float:
    """Fractional break rate while multitasking (x work + y distraction > 0).

    Pillar 2/3 of the model: break accrues as a ratio, not a boolean. When there
    is concurrent provable work (x) alongside some distraction (y), today's
    engine gives a flat 0:0 neutral — concurrent productive instances get
    punished as idle. The fractional model instead trickles break at x/(x+y),
    in [0, 1], so productive multitasking still earns (slowly) instead of
    stalling.

    Phase-2 enforcement primitive — descriptive only until the flag flips.
    """
    denom = x_work + y_distraction
    if denom <= 0:
        return 0.0
    return max(0.0, min(1.0, x_work / denom))
