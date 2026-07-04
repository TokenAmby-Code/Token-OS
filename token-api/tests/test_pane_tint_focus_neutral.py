from __future__ import annotations

import pytest

import shared


def test_apply_pane_tint_routes_through_focus_neutral_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(args: tuple[str, ...], **_kwargs):
        calls.append(tuple(args))
        return {"stdout": ""}

    monkeypatch.setattr(shared, "_tmuxctld_run_tmux", fake_run)
    shared.apply_pane_tint("%42", "#300808", source="test")

    assert calls == [
        ("set-option", "-p", "-t", "%42", "window-style", "bg=#300808"),
        ("set-option", "-p", "-t", "%42", "window-active-style", "bg=#300808"),
    ]


def test_apply_pane_tint_ignores_stale_discord_voice_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(args: tuple[str, ...], **_kwargs):
        calls.append(tuple(args))
        if tuple(args) == ("show-options", "-pqv", "-t", "%42", "@DISCORD_VOICE_LOCK"):
            return {"stdout": "1"}
        return {"stdout": ""}

    monkeypatch.setattr(shared, "_tmuxctld_run_tmux", fake_run)
    shared.apply_pane_tint("%42", "#302800", source="test")

    assert ("show-options", "-pqv", "-t", "%42", "@DISCORD_VOICE_LOCK") not in calls
    assert ("set-option", "-p", "-t", "%42", "window-style", "bg=#302800") in calls
