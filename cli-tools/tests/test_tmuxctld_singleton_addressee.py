"""Delivery-time singleton addressee assertion (make-impossible layer).

Cluster A P0: a ``council:custodes``-addressed report was delivered into the
live ``council:malcador`` pane. Resolution and delivery are separated in time;
whatever the resolver picked, the byte-bearing send must re-verify that the
pane it is about to write into is *currently* stamped with the requested
persona singleton label, and that exactly one live pane carries that label.
Mismatch, duplicate, or missing stamp → loud refusal, zero bytes.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import occupancy as occupancy_module
from tmuxctl import service as service_module
from tmuxctl.service import TmuxControlPlane


class StampScanAdapter:
    """Fake adapter exposing live @PANE_ID stamps + recording byte sends."""

    def __init__(self, stamps: dict[str, str]) -> None:
        self.stamps = dict(stamps)
        self.sends: list[tuple[str, str]] = []

    def show_pane_option(self, pane_id: str, option: str) -> str:
        if option == "@PANE_ID":
            return self.stamps.get(pane_id, "")
        return ""

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[0] == "list-panes":
            return "\n".join(f"{pid}\t{role}" for pid, role in self.stamps.items())
        return ""

    def send_text_then_submit(self, target: str, text: str, *, clear_prompt: bool = False) -> None:
        self.sends.append((target, text))


@pytest.fixture
def _quiet_occupancy(monkeypatch: pytest.MonkeyPatch):
    """Neutralize the (already-tested) occupancy gate; this file tests identity."""
    monkeypatch.setattr(
        occupancy_module,
        "assert_comms_delivery_target_occupied",
        lambda adapter, pane: None,
    )


def _plane_resolving_to(monkeypatch: pytest.MonkeyPatch, adapter, phys: str) -> TmuxControlPlane:
    """Control plane whose label resolution returns ``phys`` (simulating the
    churn/stale window where the resolver picked that pane for the label)."""
    monkeypatch.setattr(service_module, "resolve_to_physical", lambda _a, _t: phys)
    return TmuxControlPlane(adapter)


def test_send_to_custodes_label_refuses_when_pane_is_stamped_malcador(
    monkeypatch: pytest.MonkeyPatch, _quiet_occupancy
) -> None:
    """THE misroute red: resolver handed back %30 for council:custodes, but %30
    is live-stamped council:malcador. Bytes must not land."""
    adapter = StampScanAdapter({"%28": "council:custodes", "%30": "council:malcador"})
    plane = _plane_resolving_to(monkeypatch, adapter, "%30")
    with pytest.raises(ValueError, match="singleton_addressee"):
        plane.send_text("council:custodes", "MILESTONE for council:custodes")
    assert adapter.sends == []


def test_send_to_custodes_label_refuses_on_duplicate_custodes_stamps(
    monkeypatch: pytest.MonkeyPatch, _quiet_occupancy
) -> None:
    """Duplicate live custodes stamps = churn window mid-flight; refuse even if
    the resolver happened to pick one of them."""
    adapter = StampScanAdapter({"%28": "council:custodes", "%30": "council:custodes"})
    plane = _plane_resolving_to(monkeypatch, adapter, "%28")
    with pytest.raises(ValueError, match="singleton_addressee"):
        plane.send_text("council:custodes", "report")
    assert adapter.sends == []


def test_send_to_custodes_label_refuses_when_no_pane_carries_the_stamp(
    monkeypatch: pytest.MonkeyPatch, _quiet_occupancy
) -> None:
    """A stale resolution to a since-restamped pane must fail loud, never
    deliver into whatever occupies the pane now."""
    adapter = StampScanAdapter({"%30": "council:malcador", "%29": "council:pax"})
    plane = _plane_resolving_to(monkeypatch, adapter, "%30")
    with pytest.raises(ValueError, match="singleton_addressee"):
        plane.send_text("council:custodes", "report")
    assert adapter.sends == []


def test_send_to_custodes_label_delivers_when_stamp_matches_uniquely(
    monkeypatch: pytest.MonkeyPatch, _quiet_occupancy
) -> None:
    """Happy path stays deliverable: unique matching stamp → bytes flow."""
    adapter = StampScanAdapter({"%28": "council:custodes", "%30": "council:malcador"})
    plane = _plane_resolving_to(monkeypatch, adapter, "%28")
    result = plane.send_text("council:custodes", "report")
    assert result["status"] == "submitted"
    assert adapter.sends == [("%28", "report")]


def test_non_singleton_targets_are_not_gated_by_addressee_assertion(
    monkeypatch: pytest.MonkeyPatch, _quiet_occupancy
) -> None:
    """Worker/stack sends keep today's semantics; the identity gate is scoped
    to persona singleton addresses."""
    adapter = StampScanAdapter({"%41": "mechanicus:1", "%42": "mechanicus:2"})
    plane = _plane_resolving_to(monkeypatch, adapter, "%41")
    result = plane.send_text("mechanicus:1", "task")
    assert result["status"] == "submitted"
    assert adapter.sends == [("%41", "task")]


def test_physical_target_send_bypasses_label_identity_gate(
    monkeypatch: pytest.MonkeyPatch, _quiet_occupancy
) -> None:
    """An explicit %NN request asserts nothing about labels — the caller chose
    a physical pane; occupancy gating still applies upstream."""
    adapter = StampScanAdapter({"%30": "council:malcador"})
    plane = TmuxControlPlane(adapter)
    result = plane.send_text("%30", "direct")
    assert result["status"] == "submitted"
    assert adapter.sends == [("%30", "direct")]
