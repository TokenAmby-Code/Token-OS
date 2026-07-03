"""tmuxctld human-lock inviolability at the send-path chokepoint.

Regression for the typing-guard-hold-pierce P0: an enforce-action that sets a
process-global ``TMUX_SEND_GATE_ALLOW`` sanctioned override (e.g. a
deferred-timeout Custodes nag, main.py ``custodes_enforcement_deferred_timeout``)
pierced the Emperor's live keystroke lock. The send gate only yields a sanctioned
override back to a human lock for the daemon's OWN thread-local transaction reasons
(``tmuxctld-send-holder`` / ``tmuxctl-submit-transaction`` / direct-user append);
ANY other override reason — including every enforce-action env override — sailed
straight through the typing guard and clobbered active typing.

These tests pin the daemon chokepoint contract with NO live tmux: the pane's
human ON/PENDING lock is mocked via the send_gate option primitives, and the
fake adapter records whether a byte-bearing send was issued. A live human lock
must queue the send (``ok:true`` / ``status:queued``) and the adapter must NOT be
called until the typing guard drops, regardless of any ambient ``TMUX_SEND_GATE_ALLOW`` override.
"""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import time
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import daemon, send_gate, typing_guard_state


@pytest.fixture(autouse=True)
def _isolated_deferred_queue(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    monkeypatch.setenv("TMUXCTLD_DEFERRED_SENDS_PATH", str(tmp_path / "deferred-sends.json"))
    monkeypatch.setattr(daemon, "_DEFERRED_SEND_QUEUE", daemon.DeferredSendQueue())
    monkeypatch.setattr(daemon, "_schedule_deferred_drain", lambda _pane: None)


class _RecordingAdapter:
    """tmux reachable; records any byte-bearing send so a pierce is observable.

    ``send_text_then_submit`` simulates the real adapter delivering bytes to the
    pane. If the daemon ever calls it while a human lock is live, that IS the
    pierce the test must catch.
    """

    sends: list = []

    def __init__(self) -> None:
        self.last_send_gate_result = None

    def list_sessions(self) -> list:
        return []

    def _resolve_pane_target_arg(self, pane: str) -> str:
        return pane

    def send_text_then_submit(
        self, target: str, text: str, *, clear_prompt: bool = False, **_kw
    ) -> None:
        type(self).sends.append((target, text))

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[:1] == ("send-keys",):
            type(self).sends.append(args)
        return ""

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        type(self).sends.append((target, *keys))


class _ResolveExplodesAdapter(_RecordingAdapter):
    """tmux reachable, but pane resolution genuinely fails (not AttributeError).

    A canonical id that cannot be resolved must NOT fall back to the unresolved
    id and gamble a pierce — the daemon fails closed.
    """

    def _resolve_pane_target_arg(self, pane: str) -> str:
        raise RuntimeError("resolver blew up")


def _serve(adapter_factory):
    server = daemon.TmuxctldServer(
        ("127.0.0.1", 0), adapter_factory=adapter_factory, version="t", sha="t"
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    assert server.ready.wait(timeout=5), "server thread never signalled ready"
    return server


def _post(server, path: str, body):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _mock_human_lock(monkeypatch: pytest.MonkeyPatch, *, pending: bool = False) -> None:
    """Simulate a live human keystroke (or pending) lock with NO live tmux.

    Patches the send_gate option primitives so both the gate predicate and the
    daemon's human-lock check see a future deadline for the pane, and neutralizes
    quiet-hours so typing-guard is the only active signal.
    """
    future = int(time.time()) + 300
    lock = None if pending else future
    pend = future if pending else None
    monkeypatch.setattr(send_gate, "_pane_lock_until", lambda target: lock)
    monkeypatch.setattr(send_gate, "_pane_pending_until", lambda target: pend)
    monkeypatch.setattr(send_gate, "_pane_agent_until", lambda target: None)
    monkeypatch.setattr(send_gate, "_pane_human_locked", lambda target: True)
    monkeypatch.setattr(send_gate, "quiet_hours_active", lambda **_kw: (False, {}))
    # The daemon must never reach (or succeed at) acquiring its own AGENT hold over
    # a human-locked pane; with no live tmux, force the realistic denied result.
    monkeypatch.setattr(typing_guard_state, "hold", lambda *a, **k: False)


def test_enforce_override_cannot_pierce_human_keystroke_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingAdapter.sends.clear()
    _mock_human_lock(monkeypatch)
    # The enforce-action's process-global sanctioned override (NOT one of the
    # daemon's thread-local holder reasons).
    monkeypatch.setenv("TMUX_SEND_GATE_ALLOW", "custodes_enforcement_deferred_timeout")

    server = _serve(_RecordingAdapter)
    try:
        status, payload = _post(
            server,
            "/send-text",
            {"pane": "%9", "text": "OOB enforce nag", "submit": True, "verify": False},
        )
    finally:
        server.shutdown()

    assert status == 200
    assert payload["ok"] is True, payload
    assert payload["result"]["status"] == "queued"
    assert payload["result"]["reason"] == "typing_guard"
    assert _RecordingAdapter.sends == [], "bytes reached the pane over a live human lock"


def test_enforce_override_cannot_pierce_human_pending_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingAdapter.sends.clear()
    _mock_human_lock(monkeypatch, pending=True)
    monkeypatch.setenv("TMUX_SEND_GATE_ALLOW", "custodes_enforcement_deferred_timeout")

    server = _serve(_RecordingAdapter)
    try:
        status, payload = _post(
            server,
            "/send-text",
            {"pane": "%9", "text": "OOB enforce nag", "submit": True, "verify": False},
        )
    finally:
        server.shutdown()

    assert status == 200
    assert payload["ok"] is True, payload
    assert payload["result"]["status"] == "queued"
    assert payload["result"]["reason"] == "typing_guard"
    assert _RecordingAdapter.sends == []


def test_enforce_override_cannot_pierce_human_lock_via_send_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The /tmux/send-keys handler honors the same inviolable human-lock guard."""
    _RecordingAdapter.sends.clear()
    _mock_human_lock(monkeypatch)
    monkeypatch.setenv("TMUX_SEND_GATE_ALLOW", "custodes_enforcement_deferred_timeout")

    server = _serve(_RecordingAdapter)
    try:
        status, payload = _post(
            server,
            "/tmux/send-keys",
            {"pane": "%9", "command": "C-c"},
        )
    finally:
        server.shutdown()

    assert status == 200
    assert payload["ok"] is True, payload
    assert payload["result"]["status"] == "queued"
    assert payload["result"]["reason"] == "typing_guard"
    assert _RecordingAdapter.sends == [], "keys reached the pane over a live human lock"


def test_pane_resolution_failure_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine resolver failure must gate the send, never fall through and pierce.

    No human lock is mocked here: the point is that an UNRESOLVED canonical id
    cannot be cleared, so the daemon refuses rather than keying the lock read on a
    non-physical target tmux would not understand.
    """
    _RecordingAdapter.sends.clear()
    # Lock primitives are irrelevant — resolution fails before the lock read.
    monkeypatch.setattr(send_gate, "quiet_hours_active", lambda **_kw: (False, {}))
    monkeypatch.setattr(typing_guard_state, "hold", lambda *a, **k: False)
    monkeypatch.setenv("TMUX_SEND_GATE_ALLOW", "custodes_enforcement_deferred_timeout")

    server = _serve(_ResolveExplodesAdapter)
    try:
        status, payload = _post(
            server,
            "/send-text",
            {"pane": "council:custodes", "text": "nag", "submit": True, "verify": False},
        )
    finally:
        server.shutdown()

    assert status == 200
    assert payload["ok"] is False, f"unresolved pane was not failed closed: {payload}"
    assert payload["error"]["code"] == "gated"
    assert payload["error"]["detail"].get("gate") == "pane_unresolved"
    assert _RecordingAdapter.sends == []


def test_unlocked_pane_still_sends_under_enforce_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guard must not over-block: an OFF pane still delivers under the override."""
    _RecordingAdapter.sends.clear()
    monkeypatch.setattr(send_gate, "_pane_lock_until", lambda target: None)
    monkeypatch.setattr(send_gate, "_pane_pending_until", lambda target: None)
    monkeypatch.setattr(send_gate, "_pane_agent_until", lambda target: None)
    monkeypatch.setattr(send_gate, "_pane_human_locked", lambda target: False)
    monkeypatch.setattr(send_gate, "quiet_hours_active", lambda **_kw: (False, {}))
    monkeypatch.setattr(typing_guard_state, "hold", lambda *a, **k: False)
    monkeypatch.setenv("TMUX_SEND_GATE_ALLOW", "custodes_enforcement_deferred_timeout")

    server = _serve(_RecordingAdapter)
    try:
        status, payload = _post(
            server,
            "/send-text",
            {"pane": "%9", "text": "nag", "submit": True, "verify": False},
        )
    finally:
        server.shutdown()

    assert status == 200
    assert payload["ok"] is True, payload
    assert _RecordingAdapter.sends == [("%9", "nag")]
