from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl.resolver import list_free_panes
from tmuxctl.service import TmuxControlPlane


class FakeAdapter:
    """Minimal adapter stub: serves a canned `list-panes -a` scan.

    `rows` is a list of (pane_id, @PANE_CLEAN, @INSTANCE_ID, @PANE_ID,
    window_name) tuples mirroring the freelist format string.
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
    # clean + no agent → FREE
    ("%24", "1", "", "palace:N", "palace"),
    # clean but a live agent owns it → NOT free
    ("%25", "1", "uuid-agent", "palace:E", "palace"),
    # dirty shell (no stamp) → NOT free
    ("%29", "", "", "somnium:NE", "somnium"),
    # clean + no agent + no cardinal role → FREE, role None
    ("%43", "1", "", "", "scratch"),
    # explicit non-1 stamp value → NOT free
    ("%50", "0", "", "legion:S", "legion"),
]


def test_list_free_panes_returns_clean_agent_free_only():
    free = list_free_panes(FakeAdapter(LIVE))
    ids = [p.pane_id for p in free]
    assert ids == ["%24", "%43"]


def test_list_free_panes_excludes_agent_owned_clean_pane():
    free = list_free_panes(FakeAdapter(LIVE))
    assert all(p.pane_id != "%25" for p in free)


def test_list_free_panes_role_canonicalized_and_optional():
    free = {p.pane_id: p for p in list_free_panes(FakeAdapter(LIVE))}
    assert free["%24"].pane_role == "palace:N"
    assert free["%43"].pane_role is None
    assert free["%43"].window_name == "scratch"


def test_list_free_panes_empty_when_none_clean():
    free = list_free_panes(FakeAdapter([("%1", "", "", "x:N", "w")]))
    assert free == []


def test_list_free_panes_single_scan():
    adapter = FakeAdapter(LIVE)
    list_free_panes(adapter)
    list_panes_calls = [c for c in adapter.calls if c[:2] == ("list-panes", "-a")]
    assert len(list_panes_calls) == 1


def test_service_freelist_shape():
    plane = TmuxControlPlane(adapter=FakeAdapter(LIVE))
    out = plane.freelist()
    assert out == [
        {"pane_id": "%24", "pane_role": "palace:N", "window_name": "palace"},
        {"pane_id": "%43", "pane_role": "", "window_name": "scratch"},
    ]
