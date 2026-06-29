"""Smoke test proving the bounty-board lane is wired correctly.

This is intentionally a *failing* assertion: it stands in for an unbuilt
feature. Because `conftest.py` auto-applies `xfail(strict=False)`, it reports as
an XFAIL (an open bounty) and keeps the lane green. If the auto-marking ever
breaks, this turns into a hard failure and the lane wiring is caught.

Delete or replace this once real bounties populate the board.
"""


def test_bounty_lane_is_wired() -> None:
    # Stand-in for "a vault feature that does not exist yet".
    feature_built = False
    assert feature_built, "open bounty: example unbuilt feature (expected xfail)"
