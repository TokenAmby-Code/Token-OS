from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.resolver import instance_id_for_pane, resolve_instance
from tmuxctl.service import TmuxControlPlane


class FakeAdapter:
    """Minimal adapter stub: serves a canned `list-panes -a` scan.

    `rows` is a list of (pane_id, instance_id, pane_role) tuples mirroring the
    `#{pane_id}\\t#{@INSTANCE_ID}\\t#{@PANE_ID}` format string.
    """

    def __init__(self, rows: list[tuple[str, str, str]]):
        self._rows = rows
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.calls.append(args)
        if args[:2] == ("list-panes", "-a"):
            return "\n".join("\t".join(r) for r in self._rows) + ("\n" if self._rows else "")
        raise AssertionError(f"unexpected tmux call: {args}")


LIVE = [
    ("%24", "uuid-palace-n", "palace:N"),
    ("%25", "uuid-palace-e", "palace:E"),
    ("%29", "uuid-somnium-ne", "somnium:NE"),
    ("%43", "", ""),  # unstamped scratch pane — must be skipped
    ("%50", "uuid-no-role", ""),  # stamped but no @PANE_ID role
]


def test_resolve_instance_found_returns_pane_and_role():
    resolved = resolve_instance(FakeAdapter(LIVE), "uuid-palace-n")
    assert resolved.found is True
    assert resolved.pane_id == "%24"
    assert resolved.pane_role == "palace:N"


def test_resolve_instance_not_found_fails_closed():
    resolved = resolve_instance(FakeAdapter(LIVE), "uuid-gone")
    assert resolved.found is False
    assert resolved.pane_id is None
    assert resolved.pane_role is None


def test_resolve_instance_unstamped_panes_are_skipped():
    # The empty-@INSTANCE_ID pane (%43) must never be addressable by "".
    resolved = resolve_instance(FakeAdapter(LIVE), "")
    assert resolved.found is False


def test_resolve_instance_stamped_without_role_returns_pane_no_role():
    resolved = resolve_instance(FakeAdapter(LIVE), "uuid-no-role")
    assert resolved.found is True
    assert resolved.pane_id == "%50"
    assert resolved.pane_role is None


def test_resolve_instance_canonicalizes_role():
    # Legacy/alias positions canonicalize through the same path as resolve-pane.
    resolved = resolve_instance(FakeAdapter([("%29", "u", "somnium:NE")]), "u")
    assert resolved.pane_role == "somnium:NE"


def test_service_resolve_instance_shape_found():
    plane = TmuxControlPlane(adapter=FakeAdapter(LIVE))
    out = plane.resolve_instance("uuid-somnium-ne")
    # The service shape is canonical-role-first (the ledger-identity crusade): the
    # tmux stamp fallback reports the canonical role as both pane_id and pane_role,
    # never a raw %NN.
    assert out == {
        "instance_id": "uuid-somnium-ne",
        "pane_id": "somnium:NE",
        "pane_role": "somnium:NE",
        "found": True,
        "agent": "auto",
        "live_agent": False,
    }


def test_service_resolve_instance_shape_not_found():
    plane = TmuxControlPlane(adapter=FakeAdapter(LIVE))
    out = plane.resolve_instance("uuid-gone")
    assert out == {
        "instance_id": "uuid-gone",
        "pane_id": "",
        "pane_role": "",
        "found": False,
        "agent": "auto",
        "live_agent": False,
    }


def test_resolve_instance_single_scan():
    # The whole resolution must cost exactly one global tmux scan.
    adapter = FakeAdapter(LIVE)
    resolve_instance(adapter, "uuid-palace-e")
    list_panes_calls = [c for c in adapter.calls if c[:2] == ("list-panes", "-a")]
    assert len(list_panes_calls) == 1


# ── Reverse: pane -> live @INSTANCE_ID (the ack-verification key) ──────────────
# Regression: a pane carrying a live stamp but absent from / unbound in the
# wrapper ledger must still resolve to its instance_id. An empty result makes the
# daemon ack sniffer return immediately and reports every delivered send as a
# false ``unverified`` (the brief submit-ack false-negative).


def test_instance_id_for_pane_resolves_public_role():
    # A public page:id (never a valid `show-pane-option -t` target) resolves via
    # the canonical-role match — the exact codex-worker case that returned "".
    assert instance_id_for_pane(FakeAdapter(LIVE), "palace:N") == "uuid-palace-n"


def test_instance_id_for_pane_resolves_physical_pane_id():
    assert instance_id_for_pane(FakeAdapter(LIVE), "%29") == "uuid-somnium-ne"


def test_instance_id_for_pane_unstamped_pane_is_empty():
    # %43 is live but carries no @INSTANCE_ID — fail closed, never guess.
    assert instance_id_for_pane(FakeAdapter([("%43", "", "palace:S")]), "palace:S") == ""


def test_instance_id_for_pane_absent_pane_is_empty():
    assert instance_id_for_pane(FakeAdapter(LIVE), "reservists:W") == ""


def test_instance_id_for_pane_single_scan():
    adapter = FakeAdapter(LIVE)
    instance_id_for_pane(adapter, "palace:E")
    list_panes_calls = [c for c in adapter.calls if c[:2] == ("list-panes", "-a")]
    assert len(list_panes_calls) == 1


def test_service_instance_id_for_pane_falls_back_to_stamp():
    # Ledger miss (empty test ledger) must NOT strand a stamped pane at "".
    plane = TmuxControlPlane(adapter=FakeAdapter(LIVE))
    out = plane.instance_id_for_pane("palace:E")
    assert out["found"] is True
    assert out["instance_id"] == "uuid-palace-e"
    assert out["pane"] == "palace:E"


def test_service_instance_id_for_pane_unresolved_fails_closed():
    plane = TmuxControlPlane(adapter=FakeAdapter(LIVE))
    out = plane.instance_id_for_pane("reservists:SW")
    assert out["found"] is False
    assert out["instance_id"] == ""
