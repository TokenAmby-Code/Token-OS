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
    pane_id = "%3"
    pane_role = "legion:custodes"
    window_name = "legion"

    def __init__(self) -> None:
        self.sends: list[tuple[str, ...]] = []

    def list_sessions(self) -> list:
        return []

    def current_session_name(self) -> str:
        return "main"

    def list_windows(self, session_name: str) -> list[dict[str, str]]:
        return [{"window_index": "1"}]

    def list_panes(self, target: str) -> list[dict[str, str]]:
        return [
            {
                "pane_id": self.pane_id,
                "session_name": "main",
                "window_index": "1",
                "window_name": self.window_name,
                "pane_index": "0",
                "width": "80",
                "height": "24",
                "current_command": "zsh",
                "tty": "/dev/ttys000",
                "active": "1",
            }
        ]

    def show_window_option(self, target: str, option: str) -> str:
        return ""

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[0] == "display-message":
            return f"{self.pane_id}\t1\t\t{self.pane_role}\t{self.window_name}\t999"
        if args[0] == "send-keys":
            self.sends.append(tuple(args))
            return ""
        return ""

    def send_keys(self, target: str, *keys: str, allow_failure: bool = False) -> None:
        self.sends.append(("send-keys", "-t", target, *keys))

    def show_pane_option(self, pane_id: str, option: str) -> str:
        if option == "@PANE_ID":
            return self.pane_role
        return ""


class EmptyWorkerAdapter(SingletonAdapter):
    pane_id = "%9"
    pane_role = "mechanicus:1"
    window_name = "mechanicus"

    def run(self, *args: str, allow_failure: bool = False) -> str:
        if args[0] == "display-message":
            return f"{self.pane_id}\t1\t\t{self.pane_role}\t{self.window_name}\t1000"
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
