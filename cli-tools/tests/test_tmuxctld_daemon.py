"""tmuxctld smoke tests: start the real ThreadingHTTPServer in-process against a
stub adapter and hit it over loopback (the in-process pattern from
``test_instance_name_cli.py``). Asserts the ``/health`` shape, the envelope, a
representative endpoint, and 404 on an unknown route."""

from __future__ import annotations

import json
import pathlib
import socket
import sys
import threading
import urllib.error
import urllib.request

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import daemon


@pytest.fixture(autouse=True)
def _no_live_tmux_guard(monkeypatch):
    """No daemon test may touch a live tmux server (hook-tests-no-live-tmux).

    ``_h_send_text`` now acquires/releases the typing-guard AGENT hold, which
    shells real tmux. Stub it module-wide so the default is "hold DENIED"
    (held=False) — the no-live-tmux outcome — keeping every existing send-path
    assertion unchanged. Tests exercising the hold explicitly re-patch these.
    """
    monkeypatch.setattr(daemon.typing_guard_state, "hold", lambda *a, **k: False)
    monkeypatch.setattr(daemon.typing_guard_state, "release", lambda *a, **k: None)
    monkeypatch.setattr(daemon.send_gate, "evaluate", lambda *a, **k: None)


class StubAdapter:
    """Minimal adapter: tmux reachable, every scan returns empty (fail-closed)."""

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self.run("show-options", "-pv", "-t", pane_id, option, allow_failure=True).strip()


class RecordingVoiceAdapter(StubAdapter):
    def __init__(self) -> None:
        self.calls = []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.calls.append(args)
        return ""

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.calls.append(("send-keys-helper", target, *keys))


def _serve(adapter_factory):
    server = daemon.TmuxctldServer(
        ("127.0.0.1", 0),
        adapter_factory=adapter_factory,
        version="9.9.9",
        sha="deadbee",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    # Gate on the real ready event (set once the accept loop owns the socket) —
    # no sleep-based race. The timeout is only a deadlock backstop, not the gate.
    assert server.ready.wait(timeout=5), "server thread never signalled ready"
    return server, thread


def _get(server, path: str):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post(server, path: str, body):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post_timeout(server, path: str, body, *, timeout: float = 5):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_server_signals_ready_event() -> None:
    # Finding #3: the server thread sets a threading.Event once it is actually
    # listening; _serve gates on it, so by the time it returns the event is set.
    server, _ = _serve(StubAdapter)
    try:
        assert server.ready.is_set()
    finally:
        server.shutdown()


def test_health_shape() -> None:
    server, _ = _serve(StubAdapter)
    try:
        status, payload = _get(server, "/health")
        assert status == 200
        # /health is the one un-enveloped surface.
        assert payload["ok"] is True
        assert payload["tmux_reachable"] is True
        assert payload["version"] == "9.9.9"
        assert payload["sha"] == "deadbee"
        assert "port" in payload
    finally:
        server.shutdown()


def test_resolve_instance_fail_closed_envelope() -> None:
    server, _ = _serve(StubAdapter)
    try:
        status, payload = _get(server, "/tmux/resolve-instance?instance_id=does-not-exist")
        assert status == 200
        assert payload["ok"] is True
        result = payload["result"]
        # Canonical-only, fail-closed: no live pane -> found:false, no id, no 500.
        assert result["instance_id"] == "does-not-exist"
        assert result["found"] is False
        assert result["pane_id"] == ""
        assert result["pane_role"] == ""
    finally:
        server.shutdown()


class FoundInstanceAdapter:
    """tmux reachable; one live pane carries @INSTANCE_ID=my-uuid @PANE_ID=mechanicus:1."""

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[:2] == ("list-panes", "-a"):
            return "%42\tmy-uuid\tmechanicus:1"
        return ""


def test_resolve_instance_returns_canonical_role_never_physical() -> None:
    server, _ = _serve(FoundInstanceAdapter)
    try:
        _, payload = _get(server, "/tmux/resolve-instance?instance_id=my-uuid")
        result = payload["result"]
        assert result["found"] is True
        # The canonical {page}:{id} role — NOT the raw physical %42.
        assert result["pane_id"] == "mechanicus:1"
        assert result["pane_role"] == "mechanicus:1"
        assert "%42" not in json.dumps(payload)
    finally:
        server.shutdown()


class StampedPaneAdapter:
    """current pane resolves to %7 and carries @INSTANCE_ID=stamped-uuid."""

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[:2] == ("display-message", "-p"):
            return "%7"
        if args[0] == "show-options" and args[-1] == "@INSTANCE_ID":
            return "stamped-uuid"
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self.run("show-options", "-pv", "-t", pane_id, option, allow_failure=True).strip()


def test_instance_id_for_pane_reads_stamp() -> None:
    server, _ = _serve(StampedPaneAdapter)
    try:
        status, payload = _get(server, "/tmux/instance-id-for-pane?pane=current")
        assert status == 200
        result = payload["result"]
        assert result["found"] is True
        assert result["instance_id"] == "stamped-uuid"
        assert result["pane"] == "%7"
    finally:
        server.shutdown()


def test_instance_id_for_pane_fail_closed_when_unstamped() -> None:
    server, _ = _serve(StubAdapter)
    try:
        status, payload = _get(server, "/tmux/instance-id-for-pane?pane=current")
        assert status == 200
        result = payload["result"]
        # No @INSTANCE_ID stamp -> fail-closed: found:false, empty instance_id.
        assert result["found"] is False
        assert result["instance_id"] == ""
    finally:
        server.shutdown()


class WrapperEndAdapter:
    def __init__(self) -> None:
        self.wrapper_owner = "wrap-1"
        self.instance_id = "inst-1"
        self.cleared: list[str] = []
        self.calls: list[tuple[str, ...]] = []

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.calls.append(tuple(args))
        if args[:3] == ("display-message", "-t", "%42"):
            return "%42" if self.instance_id or self.wrapper_owner else ""
        if args[:3] == ("list-panes", "-a", "-F"):
            return f"%42__TMUXCTLD_WRAPPEREND_FIELD__{self.wrapper_owner}"
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        if option == "@TOKEN_API_WRAPPER_LAUNCH_ID":
            return self.wrapper_owner
        if option == "@INSTANCE_ID":
            return self.instance_id
        return ""

    def clear_runtime_state(self, target: str) -> None:
        self.cleared.append(target)
        self.wrapper_owner = ""
        self.instance_id = ""


def test_wrapperend_clears_owned_runtime_state_immediately_and_idempotently() -> None:
    rec = WrapperEndAdapter()
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(
            server,
            "/hooks/wrapperend",
            {"wrapper_launch_id": "wrap-1", "tmux_pane": "%42", "exit_code": 0},
        )
        assert payload["ok"] is True
        assert payload["result"]["status"] == "cleared"
        assert rec.cleared == ["%42"]

        _, duplicate = _post(
            server,
            "/hooks/wrapperend",
            {"wrapper_launch_id": "wrap-1", "tmux_pane": "%42", "exit_code": 0},
        )
        assert duplicate["ok"] is True
        assert duplicate["result"]["status"] in {"already_cleared", "already_missing"}
        assert rec.cleared == ["%42"]
    finally:
        server.shutdown()


def test_wrapperend_resolves_pane_by_wrapper_id_when_payload_pane_missing() -> None:
    rec = WrapperEndAdapter()
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(server, "/hooks/wrapperend", {"wrapper_launch_id": "wrap-1"})
        assert payload["ok"] is True
        assert payload["result"]["status"] == "cleared"
        assert rec.cleared == ["%42"]
    finally:
        server.shutdown()


def test_wrapperend_rejects_unowned_mismatched_pane_without_clearing() -> None:
    rec = WrapperEndAdapter()
    rec.wrapper_owner = "other-wrap"
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(
            server,
            "/hooks/wrapperend",
            {"wrapper_launch_id": "wrap-1", "tmux_pane": "%42"},
        )
        assert payload["ok"] is False
        assert payload["error"]["code"] == "ValueError"
        assert "owned by another wrapper" in payload["error"]["message"]
        assert rec.cleared == []
    finally:
        server.shutdown()


class RecordingFocusAdapter:
    """Resolves focus-uuid -> palace:1 and records every run() call."""

    def __init__(self) -> None:
        self.calls = []

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        self.calls.append(args)
        if args[:2] == ("list-panes", "-a"):
            return "%24\tfocus-uuid\tpalace:1"
        if args and args[0] == "display-message" and "#{session_name}:#{window_index}" in args:
            return "main:3"
        return ""

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return self.run("show-options", "-pv", "-t", pane_id, option, allow_failure=True).strip()


def test_instance_focus_honors_explicit_client() -> None:
    rec = RecordingFocusAdapter()
    server, _ = _serve(lambda: rec)
    try:
        status, payload = _post(
            server, "/instance/focus", {"instance_id": "focus-uuid", "client": "viewer-1"}
        )
        assert status == 200
        assert payload["result"]["found"] is True
        # The explicit client is pointed at the pane's window before select-pane.
        assert ("switch-client", "-c", "viewer-1", "-t", "main:3") in rec.calls
    finally:
        server.shutdown()


def test_translate_ids_unresolved_passthrough() -> None:
    server, _ = _serve(StubAdapter)
    try:
        status, payload = _post(server, "/translate-ids", {"text": "pane %9 here"})
        assert status == 200
        assert payload["ok"] is True
        # No live mapping -> raw id is replaced with the fail-closed sentinel.
        assert payload["result"] == "pane unresolved here"
    finally:
        server.shutdown()


def test_unknown_route_is_404_envelope() -> None:
    server, _ = _serve(StubAdapter)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/nope"
        try:
            urllib.request.urlopen(url, timeout=5)
            raise AssertionError("expected HTTP 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
            body = json.loads(exc.read().decode("utf-8"))
            assert body["ok"] is False
            assert body["error"]["code"] == "not_found"
    finally:
        server.shutdown()


def test_bad_json_body_is_400() -> None:
    server, _ = _serve(StubAdapter)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/send-text"
        req = urllib.request.Request(
            url, data=b"{not json", headers={"Content-Type": "application/json"}
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected HTTP 400")
        except urllib.error.HTTPError as exc:
            assert exc.code == 400
            body = json.loads(exc.read().decode("utf-8"))
            assert body["error"]["code"] == "bad_request"
    finally:
        server.shutdown()


class SendAckAdapter:
    """Pane carries an instance stamp; send-keys calls are recorded."""

    calls: list[tuple[str, ...]] = []
    # Set once the literal `send-keys -l` injection is recorded, so the test can
    # post its ack deterministically AFTER the send started (no sleep race).
    literal_sent: threading.Event | None = None

    def __init__(self) -> None:
        self.last_send_gate_result = None

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        type(self).calls.append(tuple(args))
        if args[:1] == ("send-keys",) and "-l" in args and type(self).literal_sent is not None:
            type(self).literal_sent.set()
        return ""

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.run("send-keys", "-t", target, *keys, allow_failure=allow_failure)

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return "inst-ack" if option == "@INSTANCE_ID" else ""


def test_send_text_waits_for_user_prompt_submit_ack() -> None:
    SendAckAdapter.calls = []
    SendAckAdapter.literal_sent = threading.Event()
    server, _ = _serve(SendAckAdapter)
    try:
        result_box: dict = {}

        def send() -> None:
            result_box["status"], result_box["payload"] = _post_timeout(
                server,
                "/send-text",
                {
                    "pane": "%42",
                    "text": "do the thing",
                    "verify": True,
                    "verify_timeout": 2,
                    "submit_settle_seconds": 0.01,
                },
                timeout=5,
            )

        thread = threading.Thread(target=send)
        thread.start()
        # Wait for the literal send to be recorded before posting the ack, so the
        # sniffer's `since` window is already open and can never drop it as stale.
        assert SendAckAdapter.literal_sent is not None
        assert SendAckAdapter.literal_sent.wait(timeout=2)
        _, ack = _post(
            server,
            "/hooks/user-prompt-submit",
            {"session_id": "inst-ack"},
        )
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert ack["ok"] is True
        payload = result_box["payload"]
        assert payload["ok"] is True
        result = payload["result"]
        assert result["verification_status"] == "submitted"
        assert result["verified_by"] == "UserPromptSubmit"
        assert result["instance_id"] == "inst-ack"
        assert ("send-keys", "-t", "%42", "-l", "do the thing") in SendAckAdapter.calls
        assert ("send-keys", "-t", "%42", "C-m") in SendAckAdapter.calls
    finally:
        server.shutdown()


def test_send_text_reports_unverified_without_prompt_submit_ack() -> None:
    SendAckAdapter.calls = []
    server, _ = _serve(SendAckAdapter)
    try:
        status, payload = _post_timeout(
            server,
            "/send-text",
            {
                "pane": "%42",
                "text": "do the thing",
                "verify": True,
                "verify_timeout": 0.01,
                "submit_settle_seconds": 0,
            },
            timeout=5,
        )
        assert status == 200
        assert payload["ok"] is True
        result = payload["result"]
        assert result["verification_status"] == "unverified"
        assert result["verified_by"] is None
    finally:
        server.shutdown()


def test_serve_refuses_non_loopback_bind() -> None:
    # The daemon is unauthenticated and does powerful tmux ops — serve() must
    # fail closed (no bind) on any non-loopback host.
    assert daemon.serve("0.0.0.0", 0) == 2
    assert daemon.serve("10.0.0.5", 0) == 2


def test_malformed_content_length_is_bad_request() -> None:
    # A non-integer Content-Length must normalize to a 400 bad_request envelope,
    # never an unhandled 500. Crafted over a raw socket (urllib would rewrite the
    # header), with Connection: close so the server replies once and hangs up.
    server, _ = _serve(StubAdapter)
    try:
        host, port = server.server_address
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(
                b"POST /send-text HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\n"
                b"Content-Length: not-a-number\r\n"
                b"Connection: close\r\n"
                b"\r\n"
            )
            chunks = []
            while True:
                data = sock.recv(4096)
                if not data:
                    break
                chunks.append(data)
        raw = b"".join(chunks).decode("utf-8", "ignore")
        status_line, _, body = raw.partition("\r\n\r\n")
        assert "400" in status_line.splitlines()[0]
        assert json.loads(body)["error"]["code"] == "bad_request"
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# Agent-guard hold (B1) + loud swallowed-submit recovery (B2)
# ---------------------------------------------------------------------------


class StuckDraftAdapter:
    """send-keys recorded; capture-pane returns a stuck draft (Enter swallowed).

    The composer still holds the payload head AND ends in a trailing newline —
    the white-whale signature ``_detect_swallowed_submit`` matches. The sniffer
    is never fed an ack, so the verify loop exhausts its retries.
    """

    calls: list[tuple[str, ...]] = []

    def __init__(self) -> None:
        self.last_send_gate_result = None

    def list_sessions(self) -> list:
        return []

    def run(self, *args: str, allow_failure: bool = False) -> str:
        type(self).calls.append(tuple(args))
        return ""

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.run("send-keys", "-t", target, *keys, allow_failure=allow_failure)

    def capture_pane(self, pane_id: str, *, lines: int = 10) -> str:
        return "do the thing\n"

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return "inst-stuck" if option == "@INSTANCE_ID" else ""


def test_agent_guard_hold_acquired_and_released_even_when_verify_times_out(monkeypatch) -> None:
    # The hold is taken before the send and released in `finally` even when no
    # ack ever arrives (verify times out) — the guard must never leak green.
    SendAckAdapter.calls = []
    events: list[str] = []
    monkeypatch.setattr(
        daemon.typing_guard_state, "hold", lambda *a, **k: events.append("hold") or True
    )
    monkeypatch.setattr(
        daemon.typing_guard_state, "release", lambda *a, **k: events.append("release")
    )
    server, _ = _serve(SendAckAdapter)
    try:
        status, payload = _post_timeout(
            server,
            "/send-text",
            {
                "pane": "%42",
                "text": "do the thing",
                "verify": True,
                "verify_timeout": 0.01,
                "submit_settle_seconds": 0,
            },
            timeout=5,
        )
        assert status == 200
        result = payload["result"]
        assert result["guard_held"] is True
        assert result["verification_status"] == "unverified"
        assert events == ["hold", "release"], "hold acquired, then released in finally"
    finally:
        server.shutdown()


def test_agent_guard_hold_denied_does_not_release_and_send_still_routes(monkeypatch) -> None:
    # A live human lock denies the hold (held=False, the autouse default). The
    # daemon must NOT force/release — the send still routes (through the normal
    # gate, which would delay behind the human) and guard_held is False.
    SendAckAdapter.calls = []
    released: list[str] = []
    monkeypatch.setattr(
        daemon.typing_guard_state, "release", lambda *a, **k: released.append("release")
    )
    server, _ = _serve(SendAckAdapter)
    try:
        status, payload = _post_timeout(
            server,
            "/send-text",
            {
                "pane": "%42",
                "text": "do the thing",
                "verify": True,
                "verify_timeout": 0.01,
                "submit_settle_seconds": 0,
            },
            timeout=5,
        )
        assert status == 200
        result = payload["result"]
        assert result["guard_held"] is False
        assert released == [], "a denied hold must never call release"
        assert ("send-keys", "-t", "%42", "-l", "do the thing") in SendAckAdapter.calls
    finally:
        server.shutdown()


def test_send_text_returns_gated_without_waiting_or_writing_under_typing_guard(monkeypatch) -> None:
    SendAckAdapter.calls = []
    hold_calls: list[str] = []
    monkeypatch.setattr(
        daemon.send_gate,
        "evaluate",
        lambda *a, **k: {
            "suppressed": True,
            "policy": "delay",
            "reason": "typing_guard",
            "target": "%42",
        },
    )
    monkeypatch.setattr(
        daemon.typing_guard_state, "hold", lambda *a, **k: hold_calls.append("hold") or True
    )
    server, _ = _serve(SendAckAdapter)
    try:
        status, payload = _post_timeout(
            server,
            "/send-text",
            {
                "pane": "%42",
                "text": "do the thing",
                "verify": True,
                "verify_timeout": 5,
                "submit_settle_seconds": 0,
            },
            timeout=1,
        )
        assert status == 200
        assert payload["ok"] is False
        assert payload["error"]["code"] == "gated"
        assert payload["error"]["detail"]["reason"] == "typing_guard"
        assert payload["error"]["detail"]["deferred"] is True
        assert hold_calls == [], "do not acquire agent hold behind a live human guard"
        assert SendAckAdapter.calls == [], "zero bytes issued while human guard is active"
    finally:
        server.shutdown()


def test_insert_only_send_text_is_also_gated_under_typing_guard(monkeypatch) -> None:
    SendAckAdapter.calls = []
    monkeypatch.setattr(
        daemon.send_gate,
        "evaluate",
        lambda *a, **k: {
            "suppressed": True,
            "policy": "delay",
            "reason": "typing_guard",
            "target": "%42",
        },
    )
    server, _ = _serve(SendAckAdapter)
    try:
        status, payload = _post_timeout(
            server,
            "/send-text",
            {
                "pane": "%42",
                "text": "insert only",
                "submit": False,
                "verify": False,
                "submit_settle_seconds": 0,
            },
            timeout=1,
        )
        assert status == 200
        assert payload["ok"] is False
        assert payload["error"]["code"] == "gated"
        assert SendAckAdapter.calls == []
    finally:
        server.shutdown()


@pytest.mark.parametrize(
    "capture,payload,expected",
    [
        ("do the thing\n", "do the thing", True),  # draft present + trailing newline
        ("$ prompt> do the thing\n", "do the thing", True),  # head present inside composer
        ("do the thing", "do the thing", False),  # no trailing newline → submitted/clean line
        ("", "do the thing", False),  # empty composer → clean submit
        ("unrelated shell output\n", "do the thing", False),  # payload absent
        ("do the thing\n", "", False),  # empty payload never matches
    ],
)
def test_detect_swallowed_submit(capture: str, payload: str, expected: bool) -> None:
    assert daemon._detect_swallowed_submit(capture, payload) is expected


def test_swallowed_submit_fires_recovery_and_surfaces_loudly(monkeypatch) -> None:
    # The white-whale: bytes landed, Enter swallowed. The daemon must STILL fire
    # the recovery C-m (sink the stuck draft) AND surface the failure loudly —
    # never silently eat it.
    StuckDraftAdapter.calls = []
    notified: list[dict] = []
    monkeypatch.setattr(daemon.typing_guard_state, "hold", lambda *a, **k: True)
    monkeypatch.setattr(daemon.typing_guard_state, "release", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_notify_swallowed_submit", lambda **kw: notified.append(kw))
    server, _ = _serve(StuckDraftAdapter)
    try:
        status, payload = _post_timeout(
            server,
            "/send-text",
            {
                "pane": "%42",
                "text": "do the thing",
                "verify": True,
                "verify_timeout": 0.01,
                "ack_submit_retries": 1,
                "submit_settle_seconds": 0,
            },
            timeout=5,
        )
        assert status == 200
        result = payload["result"]
        assert result["swallowed_submit_detected"] is True
        assert result["recovery_attempts"] >= 1
        assert any(f["type"] == "swallowed_submit" for f in result["failures"])
        # Recovery C-m still fired to sink the stuck draft.
        assert ("send-keys", "-t", "%42", "C-m") in StuckDraftAdapter.calls
        # And the failure was surfaced on the notify path, not eaten.
        assert notified, "swallowed submit must be surfaced via notify"
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# Discord voice-session API
# ---------------------------------------------------------------------------


def test_voice_start_returns_opaque_id_and_public_role_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = RecordingVoiceAdapter()
    monkeypatch.setattr(daemon, "_voice_resolve_target", lambda _control, _bot: "palace:E")
    server, _ = _serve(lambda: rec)
    session_id = None
    try:
        status, payload = _post(
            server,
            "/voice/session/start",
            {
                "bot_name": "imperial_guard",
                "user_id": "operator",
                "channel_id": "cadia",
                "route_epoch": 7,
            },
        )
        assert status == 200
        assert payload["ok"] is True
        result = payload["result"]
        session_id = result["voice_session_id"]
        assert result["target_role"] == "palace:E"
        assert session_id
        assert "%" not in json.dumps(payload)
        assert "pane" not in json.dumps(payload).lower()
    finally:
        server.shutdown()
        if session_id:
            daemon.VOICE_SESSIONS.remove(session_id)


def test_voice_append_ship_scratch_clear_mutate_public_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = RecordingVoiceAdapter()
    monkeypatch.setattr(daemon, "_voice_resolve_target", lambda _control, _bot: "palace:E")
    server, _ = _serve(lambda: rec)
    created_session_ids: list[str] = []
    try:
        _, started = _post(
            server, "/voice/session/start", {"bot_name": "imperial_guard", "user_id": "u1"}
        )
        sid = started["result"]["voice_session_id"]
        created_session_ids.append(sid)
        _, appended = _post(
            server, "/voice/session/append", {"voice_session_id": sid, "text": "draft"}
        )
        assert appended["result"]["inserted"] is True
        assert ("send-keys", "-t", "palace:E", "-l", " ") in rec.calls
        assert ("send-keys", "-t", "palace:E", "-l", "draft") in rec.calls

        _, shipped = _post(
            server, "/voice/session/ship", {"voice_session_id": sid, "text": "final"}
        )
        assert shipped["result"]["shipped"] is True
        assert ("send-keys-helper", "palace:E", "Enter") in rec.calls

        _, started2 = _post(
            server, "/voice/session/start", {"bot_name": "imperial_guard", "user_id": "u1"}
        )
        sid2 = started2["result"]["voice_session_id"]
        created_session_ids.append(sid2)
        _, scratched = _post(server, "/voice/session/scratch", {"voice_session_id": sid2})
        assert scratched["result"]["scratched"] is True
        assert ("send-keys-helper", "palace:E", "C-c") in rec.calls

        _, started3 = _post(
            server, "/voice/session/start", {"bot_name": "imperial_guard", "user_id": "u1"}
        )
        sid3 = started3["result"]["voice_session_id"]
        created_session_ids.append(sid3)
        _, cleared = _post(server, "/voice/session/clear", {"voice_session_id": sid3})
        assert cleared["result"]["cleared"] == 1
    finally:
        server.shutdown()
        for session_id in created_session_ids:
            daemon.VOICE_SESSIONS.remove(session_id)


class ImperialGuardNoClientAdapter(StubAdapter):
    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[:2] == ("list-clients", "-F"):
            return ""
        return ""


def test_voice_imperial_guard_fails_closed_with_no_routable_client() -> None:
    server, _ = _serve(ImperialGuardNoClientAdapter)
    try:
        _, payload = _post(
            server, "/voice/session/start", {"bot_name": "imperial_guard", "user_id": "u1"}
        )
        assert payload["ok"] is False
        assert payload["error"]["code"] == "ValueError"
        assert "no routable" in payload["error"]["message"]
    finally:
        server.shutdown()


def test_voice_clear_by_bot_clears_stale_target_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rec = RecordingVoiceAdapter()
    monkeypatch.setattr(daemon, "_voice_resolve_target", lambda _control, _bot: "palace:E")
    server, _ = _serve(lambda: rec)
    try:
        _, payload = _post(server, "/voice/session/clear", {"bot_name": "imperial_guard"})
        assert payload["result"]["cleared"] == 0
        assert payload["result"]["cleared_options"] is True
        assert ("set-option", "-p", "-t", "palace:E", "@DISCORD_VOICE_LOCK", "0") in rec.calls
    finally:
        server.shutdown()


def test_startup_installs_tmux_lifecycle_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    class Proc:
        returncode = 0
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return Proc()

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)

    out = daemon.ensure_tmux_lifecycle_hooks()

    assert out["ok"] is True
    assert calls == [
        (
            ("tmux", "set-option", "-g", "remain-on-exit", "on"),
            {
                "capture_output": True,
                "text": True,
                "timeout": 5,
                "check": False,
            },
        ),
        (
            ("tmux", "set-hook", "-g", "pane-died[90]", daemon._PANE_DIED_HOOK),
            {
                "capture_output": True,
                "text": True,
                "timeout": 5,
                "check": False,
            },
        ),
    ]
    assert "tmux-pane-respawn #{pane_id}" in daemon._PANE_DIED_HOOK


def test_startup_lifecycle_hook_install_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args, **_kwargs):
        raise PermissionError("tmux denied")

    monkeypatch.setattr(daemon.subprocess, "run", fake_run)

    out = daemon.ensure_tmux_lifecycle_hooks()

    assert out["ok"] is False
    assert len(out["results"]) == 2
    assert all(result["returncode"] is None for result in out["results"])
