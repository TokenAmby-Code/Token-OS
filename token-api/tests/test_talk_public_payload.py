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


@pytest.mark.asyncio
async def test_slash_copy_accepts_public_stop_pane_for_raw_registered_talk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop-hook reply return must bridge public pane ids to raw SEND keys."""
    talk._TALKS.clear()
    talk._PAIR_INDEX.clear()
    talk._TARGET_INDEX.clear()

    async def fake_resolve_pane(identifier: str) -> str | None:
        return {"reservists:token-os": "%36", "%36": "%36"}.get(identifier)

    async def fake_slash_copy_target(record: dict, *, transcript_path: str | None = None) -> str:
        return "ROUNDTRIP_STOPHOOK_OK"

    monkeypatch.setattr(talk, "resolve_pane", fake_resolve_pane)
    monkeypatch.setattr(talk, "slash_copy_target", fake_slash_copy_target)

    try:
        record = await talk.register_talk(
            caller_pane="%46",
            target_pane="%36",
            payload="probe",
            target_instance={"id": "target-instance", "working_dir": "/tmp", "engine": "claude"},
        )

        resolved = await talk.fire_slash_copy_for_pane("reservists:token-os")

        assert [item["talk_id"] for item in resolved] == [record["talk_id"]]
        assert resolved[0]["status"] == talk.TALK_RETURNED
        assert resolved[0]["result_kind"] == "slash_copy"
        assert resolved[0]["result_text"] == "ROUNDTRIP_STOPHOOK_OK"
        assert resolved[0]["returned_by_pane"] == "%36"
        assert talk._TARGET_INDEX["%36"] == []
    finally:
        talk._TALKS.clear()
        talk._PAIR_INDEX.clear()
        talk._TARGET_INDEX.clear()
