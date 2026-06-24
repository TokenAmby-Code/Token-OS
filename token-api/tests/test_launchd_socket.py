"""Tests for the launchd socket-activation helper (token-api/launchd_socket.py).

The helper must degrade to None — so main.py falls back to a normal host/port
bind — whenever there is no launchd-activated socket (non-macOS, dev runs,
`token-restart --from` local runs, the WSL satellite).
"""

from __future__ import annotations

import launchd_socket


def test_returns_none_off_darwin(monkeypatch) -> None:
    monkeypatch.setattr(launchd_socket.sys, "platform", "linux")
    assert launchd_socket.activated_fd("Listeners") is None


def test_returns_none_outside_launchd() -> None:
    # Running pytest is not a launchd job with a Sockets entry, so on macOS the
    # real launch_activate_socket returns a nonzero rc and we fall back. On Linux
    # the platform guard already returns None. Either way: None (host/port bind).
    assert launchd_socket.activated_fd("Listeners") is None


def test_returns_none_when_libsystem_symbol_missing(monkeypatch) -> None:
    monkeypatch.setattr(launchd_socket.sys, "platform", "darwin")

    class _NoSymbol:
        def __getattr__(self, name):  # launch_activate_socket lookup fails
            raise AttributeError(name)

    monkeypatch.setattr(launchd_socket.ctypes, "CDLL", lambda *a, **k: _NoSymbol())
    assert launchd_socket.activated_fd("Listeners") is None
