"""Unit tests for the event-driven persona reconcile surface.

`TmuxControlPlane.handle_event` / `.reconcile_personas` are the engine behind the
daemon `/event` and `/reconcile` routes that replaced the retired 2-min
assert-personas poll. These drive the routing logic directly (a persona pane-died
must re-seat; a non-must-fill pane no-ops; a reservist is noted) plus a daemon
HTTP smoke test for the wiring.
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import urllib.request
from types import SimpleNamespace

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import assertions, daemon, service
from tmuxctl.service import TmuxControlPlane


class FakeAdapter:
    def __init__(self, options: dict | None = None) -> None:
        self.options = options or {}
        self.last_send_gate_result = None

    def list_sessions(self) -> list:
        return []

    def run(self, *args, allow_failure: bool = False) -> str:
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self.options.get(option, "")


def _control(options=None) -> TmuxControlPlane:
    return TmuxControlPlane(adapter=FakeAdapter(options or {}))


def test_handle_event_ignores_unhandled_event_type() -> None:
    out = _control().handle_event("pane-focus-in", pane="council:custodes")
    assert out["action"] == "ignored"
    assert "unhandled_event" in out["reason"]


def test_handle_event_requires_a_pane() -> None:
    out = _control().handle_event("pane-died", pane="")
    assert out["ok"] is False
    assert out["reason"] == "no_pane"


def test_handle_event_unresolved_pane_is_benign() -> None:
    # A dead/missing pane that no longer resolves must not 500 — it's benign.
    def boom(*a, **k):
        raise ValueError("no such pane")

    control = _control()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "resolve_pane", boom)
        out = control.handle_event("pane-died", pane="%999")
    assert out["ok"] is True
    assert out["action"] == "ignored"
    assert "unresolved_pane" in out["reason"]


def test_handle_event_persona_seat_reseats_via_assert_instance() -> None:
    control = _control({"@PANE_ID": "council:custodes", "@PANE_TYPE": "council"})
    calls = {}

    def fake_resolve(adapter, target, session_name=None):
        return SimpleNamespace(pane_id="%5", pane_role="council:custodes")

    def fake_assert(adapter, target, *, session=None):
        calls["target"] = target
        calls["session"] = session
        return {"ok": True, "action": "launched", "reason": "launched"}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "resolve_pane", fake_resolve)
        mp.setattr(assertions, "assert_instance", fake_assert)
        out = control.handle_event("pane-died", pane="council:custodes", session="main")

    assert calls["target"] == "council:custodes"
    assert calls["session"] == "main"
    assert out["ok"] is True
    assert out["action"] == "launched"
    assert out["pane_label"] == "council:custodes"


def test_handle_event_non_must_fill_pane_is_ignored() -> None:
    # The council true-terminal is a plain shell — no vacancy policy applies.
    control = _control({"@PANE_ID": "council:true-terminal", "@PANE_TYPE": "council"})

    def fake_resolve(adapter, target, session_name=None):
        return SimpleNamespace(pane_id="%9", pane_role="council:true-terminal")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "resolve_pane", fake_resolve)
        out = control.handle_event("pane-died", pane="council:true-terminal")

    assert out["action"] == "ignored"
    assert "not_must_fill" in out["reason"]


def test_handle_event_reservist_is_noted_not_acted() -> None:
    control = _control({"@PANE_ID": "reservists:civic", "@PANE_TYPE": "reservists"})

    def fake_resolve(adapter, target, session_name=None):
        return SimpleNamespace(pane_id="%3", pane_role="reservists:civic")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "resolve_pane", fake_resolve)
        out = control.handle_event("pane-died", pane="reservists:civic")

    assert out["ok"] is True
    assert out["action"] == "noted"
    assert out["reason"] == "reservist_refill_followon"


def test_reconcile_personas_sweeps_all_must_fill_labels() -> None:
    seen = []

    def fake_sweep(adapter, *, session=None):
        seen.append(session)
        return [{"ok": True, "pane_label": label} for label in assertions.PERSONA_LABELS]

    control = _control()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(assertions, "sweep_persona_panes", fake_sweep)
        results = control.reconcile_personas(session="main")

    assert seen == ["main"]
    assert len(results) == len(assertions.PERSONA_LABELS)


# -- daemon HTTP wiring smoke -------------------------------------------------


def _serve(adapter_factory):
    server = daemon.TmuxctldServer(
        ("127.0.0.1", 0), adapter_factory=adapter_factory, version="9.9.9", sha="deadbee"
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    assert server.ready.wait(timeout=5)
    return server


def _post(server, path, body):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_event_route_envelope_for_unhandled_event() -> None:
    server = _serve(FakeAdapter)
    try:
        status, payload = _post(server, "/event", {"event": "pane-focus-in", "pane": "x"})
        assert status == 200
        assert payload["ok"] is True
        assert payload["result"]["action"] == "ignored"
    finally:
        server.shutdown()


def test_reconcile_route_returns_results_envelope() -> None:
    # With a stub adapter no persona pane resolves, so each label errors and is
    # captured per-label (the sweep never aborts) — a results list of all six.
    server = _serve(FakeAdapter)
    try:
        status, payload = _post(server, "/reconcile", {})
        assert status == 200
        assert payload["ok"] is True
        assert len(payload["result"]["results"]) == len(assertions.PERSONA_LABELS)
    finally:
        server.shutdown()
