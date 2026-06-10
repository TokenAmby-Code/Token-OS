"""Tests for the headless Mac Deskflow client supervisor's state machine.

Loads ``Shell/deskflow-client-supervisor.py`` by path (it is a standalone
script, not a package module) and drives the pure ``Supervisor`` class with
the exact stdout strings deskflow-core emits — never a paraphrase. The
markers are IPC-level lines the Deskflow GUI itself parses (marked
must-not-change upstream), printed regardless of ``log/level``.
"""

import importlib.util
from pathlib import Path
from typing import Any

# Real deskflow-core stdout lines, with the timestamp + level prefix as emitted.
CONNECTED_LINE = '[2026-06-10T09:15:02] IPC: connected to server "TokenPC"'
DISCONNECTED_LINE = "[2026-06-10T09:43:11] IPC: disconnected from server"
NOISE_LINE = '[2026-06-10T09:15:01] NOTE: connecting to "TokenPC": 100.101.102.103:24800'


def load_supervisor_module() -> Any:
    module_path = Path(__file__).resolve().parents[2] / "Shell" / "deskflow-client-supervisor.py"
    spec = importlib.util.spec_from_file_location(
        "deskflow_client_supervisor_for_tests", module_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_supervisor(module: Any, start_time: float = 100.0) -> Any:
    return module.Supervisor(connect_window=20.0, reconnect_window=15.0, start_time=start_time)


class TestConnectWindow:
    def test_armed_from_birth(self):
        module = load_supervisor_module()
        sup = make_supervisor(module, start_time=100.0)
        assert sup.deadline == 120.0
        assert not sup.expired(119.9)
        assert sup.expired(120.0)
        assert sup.window_name == "connect"

    def test_connect_clears_deadline(self):
        module = load_supervisor_module()
        sup = make_supervisor(module, start_time=100.0)
        sup.handle_line(CONNECTED_LINE, 105.0)
        assert sup.deadline is None
        assert sup.connected
        # Connected: block indefinitely, never expire.
        assert sup.timeout(105.0) is None
        assert not sup.expired(10_000.0)

    def test_noise_lines_do_not_disarm(self):
        module = load_supervisor_module()
        sup = make_supervisor(module, start_time=100.0)
        sup.handle_line(NOISE_LINE, 105.0)
        assert sup.deadline == 120.0
        assert sup.expired(120.0)


class TestReconnectWindow:
    def test_disconnect_arms_reconnect_window(self):
        module = load_supervisor_module()
        sup = make_supervisor(module, start_time=100.0)
        sup.handle_line(CONNECTED_LINE, 105.0)
        sup.handle_line(DISCONNECTED_LINE, 200.0)
        assert sup.deadline == 215.0
        assert not sup.connected
        assert sup.window_name == "reconnect"

    def test_reconnect_within_window_clears(self):
        module = load_supervisor_module()
        sup = make_supervisor(module, start_time=100.0)
        sup.handle_line(CONNECTED_LINE, 105.0)
        sup.handle_line(DISCONNECTED_LINE, 200.0)
        sup.handle_line(CONNECTED_LINE, 210.0)
        assert sup.deadline is None
        assert sup.connected
        assert not sup.expired(10_000.0)

    def test_reconnect_window_expiry_kills(self):
        module = load_supervisor_module()
        sup = make_supervisor(module, start_time=100.0)
        sup.handle_line(CONNECTED_LINE, 105.0)
        sup.handle_line(DISCONNECTED_LINE, 200.0)
        assert not sup.expired(214.9)
        assert sup.expired(215.0)

    def test_repeated_drops_rearm_from_latest(self):
        module = load_supervisor_module()
        sup = make_supervisor(module, start_time=100.0)
        sup.handle_line(CONNECTED_LINE, 105.0)
        sup.handle_line(DISCONNECTED_LINE, 200.0)
        sup.handle_line(CONNECTED_LINE, 205.0)
        sup.handle_line(DISCONNECTED_LINE, 300.0)
        assert sup.deadline == 315.0


class TestMarkerDisambiguation:
    def test_disconnected_line_is_not_a_connect(self):
        # "disconnected from server" must never read as a connect — substring
        # matching is only safe because the connect marker is "connected to
        # server", which the disconnect line does not contain.
        module = load_supervisor_module()
        sup = make_supervisor(module, start_time=100.0)
        sup.handle_line(DISCONNECTED_LINE, 105.0)
        assert not sup.connected
        assert sup.deadline == 120.0  # reconnect window from now

    def test_timeout_tracks_deadline(self):
        module = load_supervisor_module()
        sup = make_supervisor(module, start_time=100.0)
        assert sup.timeout(110.0) == 10.0
        assert sup.timeout(125.0) == 0.0  # past deadline clamps, never negative
