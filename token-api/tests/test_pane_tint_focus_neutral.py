from __future__ import annotations

from typing import Any

import pytest

import shared


def test_apply_pane_tint_routes_through_focus_neutral_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []

    class FakeAdapter:
        def run(self, *args: str, allow_failure: bool = False) -> str:
            calls.append(("run", args))
            if args[:3] == ("show-options", "-pqv", "-t"):
                return ""
            raise AssertionError(f"unexpected tmux run: {args!r}")

        def set_pane_tint(self, pane: str, bg: str) -> None:
            calls.append(("set_pane_tint", pane, bg))

    import tmuxctl.tmux_adapter as tmux_adapter

    monkeypatch.setattr(tmux_adapter, "TmuxAdapter", FakeAdapter)

    shared.apply_pane_tint("%42", "#300808", source="test")

    assert calls == [
        (
            "run",
            ("show-options", "-pqv", "-t", "%42", "@DISCORD_VOICE_LOCK"),
        ),
        ("set_pane_tint", "%42", "#300808"),
    ]
