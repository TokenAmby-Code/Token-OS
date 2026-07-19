"""Shared tmuxctld long-hold ceilings and client transport budgets."""

from __future__ import annotations

import os

# Daemon send endpoints can hold the HTTP response while waiting for the
# level-2 UserPromptSubmit ack.  Keep caller transport strictly above this.
SEND_HOLD_CEILING_SECONDS = 60.0
CLIENT_TIMEOUT_MARGIN_SECONDS = 15.0

# Emperor decree (no-timeout-under-5min): no comms timeout below 5 minutes
# without a stamp.  The ceiling+margin derivation (75s) sat below this floor and
# false-negatived real sends — the tmuxctld send return leg runs 45-78s under
# wave load (root cause 2026-07-19, fix-custodes-send-transport), so a 75s client
# budget reports `transport timeout: delivery unknown` while bytes actually land.
# PR #752 left the 75s derivation on a "severance, not compliance" read that
# assumed 75s was the op's real max; live evidence shows the real max exceeds it,
# which is exactly the severer case the decree raises.
DECREE_MIN_COMMS_TIMEOUT_SECONDS = 300.0


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def client_timeout_for_ceiling(ceiling: float) -> float:
    margin = _env_float("TMUXCTLD_CLIENT_TIMEOUT_MARGIN", CLIENT_TIMEOUT_MARGIN_SECONDS)
    return float(ceiling) + margin


# Floor the derived budget at the decree minimum.  max() keeps the invariant
# "strictly above the daemon hold ceiling" intact even if the ceiling ever rises
# past the floor (ceiling+margin would then win).
SEND_CLIENT_TIMEOUT_SECONDS = max(
    client_timeout_for_ceiling(SEND_HOLD_CEILING_SECONDS),
    DECREE_MIN_COMMS_TIMEOUT_SECONDS,
)
