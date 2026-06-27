from __future__ import annotations

import json
import pathlib
import sys
import threading
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import daemon


class SingletonAdapter:
    def __init__(self) -> None:
        self.sends: list[tuple[str, ...]] = []

    def list_sessions(self) -> list:
        return []

    def _resolve_pane_target_arg(self, pane: str) -> str:
        return "%3"

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[0] == "display-message":
            return "%3\t1\t\tlegion:custodes\tlegion\t999"
        if args[0] == "send-keys":
            self.sends.append(tuple(args))
            return ""
        return ""

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.sends.append(("send-keys", "-t", target, *keys))

    def show_pane_option(self, pane_id: str, option: str) -> str:
        return ""


class EmptyWorkerAdapter(SingletonAdapter):
    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[0] == "display-message":
            return "%9\t1\t\tmechanicus:1\tmechanicus\t1000"
        return super().run(*args, allow_failure=allow_failure)


def _serve(adapter):
    server = daemon.TmuxctldServer(
        ("127.0.0.1", 0), adapter_factory=lambda: adapter, version="t", sha="t"
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    assert server.ready.wait(timeout=5)
    return server


def _post(server, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_daemon_send_text_refuses_dispatch_clear_into_singleton(monkeypatch):
    monkeypatch.setattr(daemon.typing_guard_state, "hold", lambda *a, **k: False)
    monkeypatch.setattr(daemon.typing_guard_state, "release", lambda *a, **k: None)
    monkeypatch.setattr(daemon.send_gate, "evaluate", lambda *a, **k: None)
    adapter = SingletonAdapter()
    server = _serve(adapter)
    try:
        payload = _post(server, "/send-text", {"pane": "legion:custodes", "text": "clear"})
    finally:
        server.shutdown()

    assert payload["ok"] is False
    assert "protected singleton" in payload["error"]["message"]
    assert adapter.sends == []


def test_daemon_send_text_allows_dispatch_clear_into_empty_worker(monkeypatch):
    monkeypatch.setattr(daemon.typing_guard_state, "hold", lambda *a, **k: False)
    monkeypatch.setattr(daemon.typing_guard_state, "release", lambda *a, **k: None)
    monkeypatch.setattr(daemon.send_gate, "evaluate", lambda *a, **k: None)
    adapter = EmptyWorkerAdapter()
    server = _serve(adapter)
    try:
        payload = _post(
            server,
            "/send-text",
            {"pane": "mechanicus:1", "text": "clear", "verify": False},
        )
    finally:
        server.shutdown()

    assert payload["ok"] is True
    assert adapter.sends
