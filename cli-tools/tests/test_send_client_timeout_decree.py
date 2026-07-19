#!/usr/bin/env python3
"""Behavioral-pin: the comms send-client transport budget honors the Emperor's
no-timeout-under-5min decree (merged canon: PR #752 body — "no timeout <5min").

Root cause driving this pin (2026-07-19, fix-custodes-send-transport): the
tmuxctld send return leg runs 45-78s under wave load while the client budget was
75s (ceiling 60 + margin 15), so real sends false-negatived as
`transport timeout: delivery unknown` even though bytes landed. Raising the
client budget above the decree floor keeps the whole comms client family
(brief/talk/agent-cmd, all consuming SEND_CLIENT_TIMEOUT_SECONDS) from severing a
send that is still legitimately in flight.
"""

import sys
import unittest
from pathlib import Path

LIB = Path(__file__).parents[1] / "lib"
sys.path.insert(0, str(LIB))

import tmuxctld_timeouts  # noqa: E402

# Emperor decree: no comms timeout below 5 minutes (300s) without a stamp.
DECREE_MIN_COMMS_TIMEOUT_SECONDS = 300.0


class SendClientTimeoutDecreeTest(unittest.TestCase):
    def test_client_budget_honors_five_minute_decree(self) -> None:
        self.assertGreaterEqual(
            tmuxctld_timeouts.SEND_CLIENT_TIMEOUT_SECONDS,
            DECREE_MIN_COMMS_TIMEOUT_SECONDS,
            "SEND_CLIENT_TIMEOUT_SECONDS must be >= 300s (Emperor no-timeout-under-5min decree)",
        )

    def test_client_budget_stays_strictly_above_daemon_hold_ceiling(self) -> None:
        # Invariant preserved from tmuxctld_timeouts: the caller transport must
        # outwait the daemon's level-2 ack hold, never sever below it.
        self.assertGreater(
            tmuxctld_timeouts.SEND_CLIENT_TIMEOUT_SECONDS,
            tmuxctld_timeouts.SEND_HOLD_CEILING_SECONDS,
        )


if __name__ == "__main__":
    unittest.main()
