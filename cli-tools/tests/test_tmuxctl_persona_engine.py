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
