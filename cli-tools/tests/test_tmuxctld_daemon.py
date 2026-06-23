"""tmuxctld smoke tests: start the real ThreadingHTTPServer in-process against a
stub adapter and hit it over loopback (the in-process pattern from
``test_instance_name_cli.py``). Asserts the ``/health`` shape, the envelope, a
representative endpoint, and 404 on an unknown route."""

from __future__ import annotations

import json
import pathlib
import sys
import threading
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import daemon


class StubAdapter:
    """Minimal adapter: tmux reachable, every scan returns empty (fail-closed)."""

    def list_sessions(self):
        return []

    def run(self, *args, allow_failure=False):
        return ""

    def show_pane_option(self, pane_id, option):
        return self.run("show-options", "-pv", "-t", pane_id, option, allow_failure=True).strip()


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


def _get(server, path):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post(server, path, body):
    url = f"http://127.0.0.1:{server.server_address[1]}{path}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_server_signals_ready_event():
    # Finding #3: the server thread sets a threading.Event once it is actually
    # listening; _serve gates on it, so by the time it returns the event is set.
    server, _ = _serve(StubAdapter)
    try:
        assert server.ready.is_set()
    finally:
        server.shutdown()


def test_health_shape():
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


def test_resolve_instance_fail_closed_envelope():
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

    def list_sessions(self):
        return []

    def run(self, *args, allow_failure=False):
        if args[:2] == ("list-panes", "-a"):
            return "%42\tmy-uuid\tmechanicus:1"
        return ""


def test_resolve_instance_returns_canonical_role_never_physical():
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

    def list_sessions(self):
        return []

    def run(self, *args, allow_failure=False):
        if args[:2] == ("display-message", "-p"):
            return "%7"
        if args[0] == "show-options" and args[-1] == "@INSTANCE_ID":
            return "stamped-uuid"
        return ""

    def show_pane_option(self, pane_id, option):
        return self.run("show-options", "-pv", "-t", pane_id, option, allow_failure=True).strip()


def test_instance_id_for_pane_reads_stamp():
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


def test_instance_id_for_pane_fail_closed_when_unstamped():
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


def test_translate_ids_unresolved_passthrough():
    server, _ = _serve(StubAdapter)
    try:
        status, payload = _post(server, "/translate-ids", {"text": "pane %9 here"})
        assert status == 200
        assert payload["ok"] is True
        # No live mapping -> raw id is replaced with the fail-closed sentinel.
        assert payload["result"] == "pane unresolved here"
    finally:
        server.shutdown()


def test_unknown_route_is_404_envelope():
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


def test_bad_json_body_is_400():
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


def test_heartbeat_written_only_when_reachable(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Reachable -> file written atomically.
    assert daemon.write_heartbeat(7778, reachable=True, home=str(tmp_path)) is True
    path = daemon.heartbeat_path(str(tmp_path))
    assert path.exists()
    payload = json.loads(path.read_text())
    assert payload["tmux_reachable"] is True
    assert payload["port"] == 7778

    # Unreachable -> deliberately NOT written (watchdog distinguishes alive-but-
    # tmux-dead from process-dead by heartbeat freshness).
    path.unlink()
    assert daemon.write_heartbeat(7778, reachable=False, home=str(tmp_path)) is False
    assert not path.exists()
