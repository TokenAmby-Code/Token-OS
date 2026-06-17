from __future__ import annotations

import pytest

import talk


def test_publicize_pane_payload_preserves_none_optional_pane_fields() -> None:
    payload = {
        "pane_id": "%7",
        "returned_by_pane": None,
        "nested": {"target_pane": "%404", "message": "reply from %7"},
    }

    assert talk.publicize_pane_payload(payload, {"%7": "palace:E"}) == {
        "pane_id": "palace:E",
        "returned_by_pane": None,
        "nested": {"target_pane": "unresolved", "message": "reply from palace:E"},
    }


@pytest.mark.asyncio
async def test_public_pane_map_accepts_only_canonical_position_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_tmux_list_panes() -> list[dict[str, str]]:
        return [
            {"pane_id": "%7", "position_id": "palace:E"},
            {"pane_id": "%8", "position_id": "(null)"},
            {"pane_id": "%9", "position_id": "palace:%9"},
            {"pane_id": "palace:N", "position_id": "palace:N"},
        ]

    monkeypatch.setattr(talk, "_tmux_list_panes", fake_tmux_list_panes)

    assert await talk._public_pane_map() == {"%7": "palace:E"}
