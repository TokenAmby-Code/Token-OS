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


def test_pane_died_hook_targets_tmuxctld_event_route() -> None:
    assert "POST /event" in daemon._PANE_DIED_HOOK
    assert "event=pane-died" in daemon._PANE_DIED_HOOK
    assert "pane=#{pane_id}" in daemon._PANE_DIED_HOOK


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


def test_handle_event_fill_if_row_stack_worker_is_ignored() -> None:
    # A FILL_IF_ROW stack worker is reconciled by the stack sweep / the bash
    # mechanicus pane-died branch — handle_event no-ops it (never reaches router).
    control = _control({"@PANE_ID": "mechanicus:3", "@PANE_TYPE": "stack-worker"})

    def fake_resolve(adapter, target, session_name=None):
        return SimpleNamespace(pane_id="%4", pane_role="mechanicus:3")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "resolve_pane", fake_resolve)
        out = control.handle_event("pane-died", pane="mechanicus:3")

    assert out["action"] == "ignored"
    assert "not_must_fill" in out["reason"]


class RouterAdapter(FakeAdapter):
    """FakeAdapter that answers the teardown router's tmux probes + records effects."""

    def __init__(self, *, window_name: str, pane_dead: bool = True, options=None) -> None:
        super().__init__(options or {})
        self.window_name = window_name
        self._pane_dead = pane_dead
        self.exists = True
        self.cleared: list[str] = []
        self.calls: list[tuple] = []

    def clear_runtime_state(self, target: str) -> None:
        self.cleared.append(target)

    def run(self, *args, allow_failure: bool = False) -> str:
        self.calls.append(tuple(args))
        if args and args[-1] == "#{window_name}":
            return self.window_name
        if args and args[-1] == "#{pane_dead}":
            return "1" if self._pane_dead else "0"
        if args and args[-1] == "#{pane_id}":
            return args[2] if self.exists else ""
        if args[:2] == ("kill-pane", "-t"):
            self.exists = False
            return ""
        if args[:1] == ("respawn-pane",):
            self._pane_dead = False
            return ""
        return ""

    @property
    def killed(self) -> bool:
        return any(c[:1] == ("kill-pane",) for c in self.calls)

    @property
    def respawned(self) -> bool:
        return any(c[:1] == ("respawn-pane",) for c in self.calls)


def test_handle_event_palace_slot_is_cleared_in_place_not_culled() -> None:
    # A completed one-off worker in a pre-allocated palace slot: the slot is
    # cleared IN PLACE and revived — PRESERVED, NOT culled (the morning over-reap).
    adapter = RouterAdapter(
        window_name="palace", pane_dead=True, options={"@PANE_ID": "palace:N", "@PANE_TYPE": ""}
    )
    control = TmuxControlPlane(adapter=adapter)

    def fake_resolve(a, target, session_name=None):
        return SimpleNamespace(pane_id="%7", pane_role="palace:N")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "resolve_pane", fake_resolve)
        out = control.handle_event("pane-died", pane="palace:N", session="main")

    assert out["ok"] is True
    assert out["action"] == "cleared_in_place"
    assert out["pane_class"] == "slot"
    assert adapter.killed is False  # slot preserved
    assert adapter.respawned is True  # revived in place -> back to freelist
    assert adapter.cleared == ["%7"]


def test_handle_event_dynamic_worker_is_culled() -> None:
    adapter = RouterAdapter(
        window_name="mechanicus",
        pane_dead=True,
        options={"@PANE_ID": "mechanicus:7", "@PANE_TYPE": ""},
    )
    control = TmuxControlPlane(adapter=adapter)

    def fake_resolve(a, target, session_name=None):
        return SimpleNamespace(pane_id="%9", pane_role="mechanicus:7")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service, "resolve_pane", fake_resolve)
        out = control.handle_event("pane-died", pane="mechanicus:7")

    assert out["action"] == "culled"
    assert out["pane_class"] == "worker"
    assert adapter.killed is True


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
