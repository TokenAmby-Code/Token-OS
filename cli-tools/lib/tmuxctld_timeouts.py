"""Shared tmuxctld long-hold ceilings and client transport budgets."""

from __future__ import annotations

import os

# Daemon send endpoints can hold the HTTP response while waiting for the
# level-2 UserPromptSubmit ack.  Keep caller transport strictly above this.
SEND_HOLD_CEILING_SECONDS = 60.0
LIFECYCLE_HOLD_CEILING_SECONDS = 60.0
CLIENT_TIMEOUT_MARGIN_SECONDS = 15.0


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


SEND_CLIENT_TIMEOUT_SECONDS = client_timeout_for_ceiling(SEND_HOLD_CEILING_SECONDS)
LIFECYCLE_CLIENT_TIMEOUT_SECONDS = client_timeout_for_ceiling(LIFECYCLE_HOLD_CEILING_SECONDS)
