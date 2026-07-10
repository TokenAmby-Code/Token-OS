from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from tmuxctl import persona_engine


class FakeAdapter:
    def __init__(self, engine: str = "") -> None:
        self.engine = engine
        self.options = {"@PANE_ID": "mechanicus:fabricator-general"}
        self.commands: list[tuple] = []

    def run(self, *args, allow_failure: bool = False):
        self.commands.append(tuple(args))
        if args[:5] == ("display-message", "-t", "%42", "-p", "#{pane_id}"):
            return "%42"
        if args[:3] == ("display-message", "-p", "#{pane_id}"):
            return "%42"
        if args and args[0] == "show-options":
            return ""
        return ""

    def show_pane_option(self, _pane_id: str, option: str) -> str:
        if option == "@TOKEN_API_ENGINE":
            return self.engine
        return self.options.get(option, "")


def test_rotate_persona_engine_toggles_fg_to_codex() -> None:
    adapter = FakeAdapter(engine="claude")
    resolved = SimpleNamespace(pane_id="%42", pane_role="mechanicus:fabricator-general")
    launched = []

    def fake_launch(_adapter, pane_id, spec, *, session=None):
        launched.append((pane_id, spec, session))
        return True, "launched"

    with (
        patch.object(persona_engine, "resolve_pane", return_value=resolved),
        patch.object(persona_engine, "launch_persona_seat", side_effect=fake_launch),
    ):
        result = persona_engine.rotate_persona_engine(adapter, "%42", toggle=True)

    assert result["ok"] is True
    assert result["pane_label"] == "mechanicus:fabricator-general"
    assert result["previous_engine"] == "claude"
    assert result["engine"] == "codex"
    assert launched[0][0] == "%42"
    assert launched[0][1].persona == "fabricator-general"
    assert launched[0][1].engine == "codex"
    assert ("set-option", "-pu", "-t", "%42", "window-style") in adapter.commands
    assert ("set-option", "-pu", "-t", "%42", "window-active-style") in adapter.commands


def test_rotate_persona_engine_refuses_non_persona_pane() -> None:
    adapter = FakeAdapter(engine="claude")
    resolved = SimpleNamespace(pane_id="%99", pane_role="mechanicus:1")

    with patch.object(persona_engine, "resolve_pane", return_value=resolved):
        try:
            persona_engine.rotate_persona_engine(adapter, "%99", toggle=True)
        except ValueError as exc:
            assert "not a protected persona seat" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected ValueError")


def test_rotate_persona_engine_requires_explicit_mode() -> None:
    adapter = FakeAdapter(engine="claude")
    resolved = SimpleNamespace(pane_id="%42", pane_role="mechanicus:fabricator-general")

    with patch.object(persona_engine, "resolve_pane", return_value=resolved):
        try:
            persona_engine.rotate_persona_engine(adapter, "%42")
        except ValueError as exc:
            assert "must pass --engine or --toggle" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("expected ValueError")


def test_cli_persona_engine_posts_to_daemon_with_captured_current_pane() -> None:
    from unittest.mock import MagicMock

    from tmuxctl import cli

    control = SimpleNamespace(adapter=MagicMock())
    control.adapter.run.return_value = "%119"
    posted = []

    def fake_post(path, payload, *, timeout=30.0):
        posted.append((path, payload, timeout))
        return {
            "ok": True,
            "pane": "%119",
            "pane_label": "mechanicus:fabricator-general",
            "engine": "codex",
        }

    with (
        patch.object(cli, "TmuxControlPlane", return_value=control),
        patch.object(cli, "_post_tmuxctld", side_effect=fake_post),
    ):
        rc = cli.main(["persona-engine", "--pane", "current", "--toggle"])

    assert rc == 0
    control.adapter.run.assert_called_once_with("display-message", "-p", "#{pane_id}")
    assert posted == [
        (
            "/persona-engine",
            {"pane": "%119", "engine": "", "toggle": True, "session": ""},
            30.0,
        )
    ]


def test_daemon_persona_engine_route_rotates_only_requested_pane() -> None:
    import json
    import threading
    import urllib.request

    from tmuxctl import daemon

    adapter = FakeAdapter(engine="claude")
    server = daemon.TmuxctldServer(
        ("127.0.0.1", 0), adapter_factory=lambda: adapter, version="t", sha="t"
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    assert server.ready.wait(timeout=5)
    resolved = SimpleNamespace(pane_id="%119", pane_role="mechanicus:fabricator-general")
    launched = []

    def fake_launch(_adapter, pane_id, spec, *, session=None):
        launched.append((pane_id, spec.persona, spec.engine, session))
        return True, "launched"

    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}/persona-engine",
        data=json.dumps({"pane": "%119", "toggle": True, "session": "main"}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with (
            patch.object(persona_engine, "resolve_pane", return_value=resolved) as resolve,
            patch.object(persona_engine, "launch_persona_seat", side_effect=fake_launch),
            urllib.request.urlopen(req, timeout=5) as resp,
        ):
            payload = json.loads(resp.read().decode("utf-8"))
    finally:
        server.shutdown()

    assert payload["ok"] is True
    assert payload["result"]["pane"] == "%119"
    assert payload["result"]["pane_label"] == "mechanicus:fabricator-general"
    resolve.assert_called_once_with(adapter, "%119", session_name="main")
    assert launched == [("%119", "fabricator-general", "codex", "main")]
