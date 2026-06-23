"""tmuxctld parity: the daemon is a faithful TRANSPORT, not a fork. For a fixed
workspace snapshot, the daemon endpoint result must equal the direct
``TmuxControlPlane`` method result, which must equal ``render_workspace`` of the
fixture. (Live three-way parity against the ``tmuxctl`` CLI subprocess is the
laptop live-path step; in-process we pin the snapshot and compare the rendered
bytes so a daemon/CLI divergence can only come from the transport, which we
exercise here.)"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import daemon, service
from tmuxctl.enums import GridState, PaneKind, WindowArchetype
from tmuxctl.inspect import render_workspace
from tmuxctl.models import PaneSnapshot, WindowSnapshot, WorkspaceSnapshot
from tmuxctl.service import TmuxControlPlane


class StubAdapter:
    def list_sessions(self):
        return []

    def run(self, *args, allow_failure=False):
        return ""


def _pane(pane_id: str, role: str) -> PaneSnapshot:
    return PaneSnapshot(
        pane_id=pane_id,
        session_name="main",
        window_index=3,
        window_name="mechanicus",
        pane_index=0,
        width=120,
        height=40,
        current_command="claude",
        tty="/dev/ttys003",
        pane_role=role,
        grid_state=GridState.SMALL,
        pane_kind=PaneKind.MECHANICUS,
        reserved=False,
        active=True,
    )


def _workspace() -> WorkspaceSnapshot:
    window = WindowSnapshot(
        session_name="main",
        window_index=3,
        window_name="mechanicus",
        archetype=WindowArchetype.MECHANICUS_STACK,
        focused=False,
        grid_expanded="none",
        grid_stash="",
        side_expanded="none",
        panes=(
            _pane("%29", "mechanicus:fabricator-general"),
            _pane("%80", "mechanicus:1"),
        ),
    )
    return WorkspaceSnapshot(session_name="main", windows=(window,))


def _serve(adapter_factory):
    server = daemon.TmuxctldServer(
        ("127.0.0.1", 0), adapter_factory=adapter_factory, version="t", sha="t"
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _get(server, path):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_inspect_workspace_three_way_parity(monkeypatch):
    fixture = _workspace()
    # Pin the snapshot so every path renders the SAME workspace; any difference
    # then comes only from the transport (which is what parity guards).
    monkeypatch.setattr(service, "build_workspace_snapshot", lambda adapter, session: fixture)

    expected = render_workspace(fixture)
    direct = TmuxControlPlane(StubAdapter()).inspect_workspace("main")
    assert direct == expected, "direct method diverged from render fixture"

    server = _serve(StubAdapter)
    try:
        payload = _get(server, "/inspect/workspace?session=main")
        assert payload["ok"] is True
        assert payload["result"] == expected, "daemon endpoint diverged from render fixture"
        assert payload["result"] == direct, "daemon endpoint diverged from direct method"
    finally:
        server.shutdown()


def test_inspect_workspace_canonical_only_by_default(monkeypatch):
    fixture = _workspace()
    monkeypatch.setattr(service, "build_workspace_snapshot", lambda adapter, session: fixture)
    server = _serve(StubAdapter)
    try:
        payload = _get(server, "/inspect/workspace?session=main")
        out = payload["result"]
        # Default render is canonical-only — no raw physical %NN leak.
        assert "%29" not in out and "%80" not in out
        assert "mechanicus:fabricator-general" in out
    finally:
        server.shutdown()


def test_inspect_workspace_physical_flag_restores_raw_ids(monkeypatch):
    fixture = _workspace()
    monkeypatch.setattr(service, "build_workspace_snapshot", lambda adapter, session: fixture)
    server = _serve(StubAdapter)
    try:
        payload = _get(server, "/inspect/workspace?session=main&physical=1")
        out = payload["result"]
        assert "%29" in out and "%80" in out
    finally:
        server.shutdown()
