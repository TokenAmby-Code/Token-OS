from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT.parent / "cli-tools" / "lib"))

import shared  # noqa: E402


class FakeAdapter:
    def __init__(self, *, voice_locked: bool = False) -> None:
        self.voice_locked = voice_locked
        self.calls: list[tuple[str, ...]] = []

    def run(self, *args, allow_failure: bool = False) -> str:
        self.calls.append(args)
        if args[:1] == ("show-options",):
            return "1" if self.voice_locked else ""
        return ""


def test_clear_pane_tint_unsets_style_options(monkeypatch):
    adapter = FakeAdapter(voice_locked=True)
    monkeypatch.setattr("tmuxctl.tmux_adapter.TmuxAdapter", lambda: adapter)

    shared.clear_pane_tint("%22")

    assert ("set-option", "-pu", "-t", "%22", "window-style") in adapter.calls
    assert ("set-option", "-pu", "-t", "%22", "window-active-style") in adapter.calls
    assert not [call for call in adapter.calls if call[:1] == ("select-pane",)]


def test_apply_pane_tint_still_respects_voice_lock(monkeypatch):
    adapter = FakeAdapter(voice_locked=True)
    monkeypatch.setattr("tmuxctl.tmux_adapter.TmuxAdapter", lambda: adapter)

    shared.apply_pane_tint("%22", "#300808")

    assert not [call for call in adapter.calls if call[:1] == ("select-pane",)]
    assert not [call for call in adapter.calls if call[:1] == ("set-option",)]
