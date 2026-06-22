from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import talk  # noqa: E402


async def test_lookup_instance_for_pane_returns_ps_live_pseudo_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pane = "%900402"

    async def no_instance_id(pane_id: str) -> None:
        assert pane_id == pane
        return None

    async def codex_agent(pane_id: str) -> str:
        assert pane_id == pane
        return "codex"

    async def fake_tmux_list_panes() -> list[dict[str, str]]:
        return [
            {
                "pane_id": pane,
                "position_id": "stack:worker-1",
                "session": "fake",
                "window_index": "9",
                "window_name": "stack",
            }
        ]

    monkeypatch.setattr(talk, "instance_id_for_pane", no_instance_id)
    monkeypatch.setattr(talk, "_resolve_agent_for_pane", codex_agent)
    monkeypatch.setattr(talk, "_tmux_list_panes", fake_tmux_list_panes)

    result = await talk.lookup_instance_for_pane(pane)

    assert result is not None
    assert result["id"] is None
    assert result["engine"] == "codex"
    assert result["tmux_pane"] == pane
    assert result["pane_label"] == "stack:worker-1"
    assert result["rowless_live"] is True
