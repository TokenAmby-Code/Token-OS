"""Bounty-board lane — speculative tests for UNBUILT vault features.

Every test module dropped under ``tests/bounty_board/`` is auto-marked here, so
authors write plain pytest with no per-test decorators:

* ``bounty``               — lets the required regression run exclude the whole
                             lane with ``-m "not bounty"`` (see prod-gate.yml).
* ``xfail(strict=False)``  — an unbuilt feature's test *should* fail; that is an
                             OPEN bounty and the lane stays green. The day the
                             feature ships the test starts passing → it surfaces
                             as an **XPASS**, the signal to graduate it into the
                             real regression suite (move it out of this dir and
                             drop the bounty marker).

This is the non-blocking half of the Coherence Wave's two-lane model
(regression = blocking, bounty board = advisory). See
``Sessions/coherence-wave.md`` in the Imperium vault for the full contract.
"""

from __future__ import annotations

import pathlib

import pytest

_HERE = pathlib.Path(__file__).parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Auto-apply the bounty + xfail markers to everything in this directory."""
    for item in items:
        item_path = pathlib.Path(str(item.fspath))
        if _HERE == item_path.parent or _HERE in item_path.parents:
            item.add_marker(pytest.mark.bounty)
            item.add_marker(
                pytest.mark.xfail(
                    strict=False,
                    reason="bounty: speculative test for an unbuilt vault feature",
                )
            )
