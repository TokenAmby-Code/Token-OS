from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import occupancy
from tmuxctl.resolver import list_free_panes
from tmuxctl.service import TmuxControlPlane


def _seed_wrapper_ledger(role: str, *, instance_id: str = "ledger-instance") -> None:
    from tmuxctl import wrapper_ledger

    wrapper_ledger.LEDGER.upsert(
        wrapper_id=f"wrap-{role}",
        instance_id=instance_id,
        pane_positional_id=role,
        engine="codex",
        state="OPEN",
    )


class FakeAdapter:
    """Minimal adapter stub: serves a canned `list-panes -a` scan.

    `rows` are legacy-compatible 5-tuples accepted by the parser. Availability
    is derived from wrapper-ledger occupancy plus singleton/boot-grace guards;
    the allocator walk must not ps-sniff candidate panes.
    """

    def __init__(self, rows: list[tuple[str, str, str, str, str]]):
        self._rows = rows
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.calls.append(args)
        if args[:2] == ("list-panes", "-a"):
            return "\n".join("\t".join(r) for r in self._rows) + ("\n" if self._rows else "")
        raise AssertionError(f"unexpected tmux call: {args}")


LIVE = [
    # no instance + no agent + non-singleton → FREE
    ("%24", "", "palace:N", "palace", "100"),
    # wrapper ledger owns it in tests that seed palace:E → NOT free
    ("%25", "uuid-agent", "palace:E", "palace", "101"),
    # no instance + no agent + no cardinal role → FREE, role None
    ("%43", "", "", "scratch", "103"),
]


def test_list_free_panes_returns_unoccupied_agent_free_only():
    _seed_wrapper_ledger("palace:E", instance_id="uuid-agent")
    free = list_free_panes(FakeAdapter(LIVE))
    ids = [p.pane_id for p in free]
    assert ids == ["%24", "%43"]


def test_list_free_panes_excludes_instance_owned_pane():
    _seed_wrapper_ledger("palace:E", instance_id="uuid-agent")
    free = list_free_panes(FakeAdapter(LIVE))
    assert all(p.pane_id != "%25" for p in free)


def test_list_free_panes_excludes_ledger_owned_and_live_unbound_panes(monkeypatch):
    """Allocator walk excludes wrapper-ledger occupancy and stale live unbound panes."""

    sniffed: list[int | None] = []

    def fake_active(pane_pid):
        sniffed.append(pane_pid)
        return pane_pid == 1000

    monkeypatch.setattr(occupancy, "_active_agent", fake_active)
    _seed_wrapper_ledger("mechanicus:1", instance_id="owned")
    rows = [
        ("%occupied", "", "mechanicus:1", "mechanicus", "999"),
        ("%worker", "", "mechanicus:2", "mechanicus", "1000"),
    ]

    free = list_free_panes(FakeAdapter(rows))

    assert [p.pane_id for p in free] == []
    assert sniffed == [999, 1000]


def test_list_free_panes_role_canonicalized_and_optional():
    _seed_wrapper_ledger("palace:E", instance_id="uuid-agent")
    free = {p.pane_id: p for p in list_free_panes(FakeAdapter(LIVE))}
    assert free["%24"].pane_role == "palace:N"
    assert free["%43"].pane_role is None
    assert free["%43"].window_name == "scratch"


def test_list_free_panes_empty_when_all_occupied():
    _seed_wrapper_ledger("x:N", instance_id="live-inst")
    free = list_free_panes(FakeAdapter([("%1", "live-inst", "x:N", "w", "100")]))
    assert free == []


def test_list_free_panes_single_scan():
    adapter = FakeAdapter(LIVE)
    list_free_panes(adapter)
    list_panes_calls = [c for c in adapter.calls if c[:2] == ("list-panes", "-a")]
    assert len(list_panes_calls) == 1


def test_service_freelist_shape():
    _seed_wrapper_ledger("palace:E", instance_id="uuid-agent")
    plane = TmuxControlPlane(adapter=FakeAdapter(LIVE))
    out = plane.freelist()
    assert out == [
        {"pane_id": "%24", "pane_role": "palace:N", "window_name": "palace"},
        {"pane_id": "%43", "pane_role": "", "window_name": "scratch"},
    ]


def test_list_free_panes_hard_excludes_singleton_label_even_without_stamp_or_agent():
    rows = [
        ("%custodes", "", "legion:custodes", "legion", "999"),
        ("%fg", "", "mechanicus:fabricator-general", "mechanicus", "1000"),
        ("%admin", "", "mechanicus:admin", "mechanicus", "1001"),
        ("%worker", "", "mechanicus:1", "mechanicus", "1002"),
    ]

    free = list_free_panes(FakeAdapter(rows))

    assert [p.pane_id for p in free] == ["%worker"]
