"""Event-level atomic pause for pane prompt sends.

The queued unit is the whole send event: text, submit intent, delivery options,
and attached effects.  A held/gated event must not leave composer bytes or run
side effects until the same payload is replayed after the guard drops.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest


async def _resolve_from_queue(app_env, instance_id: str):
    with sqlite3.connect(app_env.db_path) as conn:
        row = conn.execute(
            "SELECT tmux_pane FROM pane_write_queue "
            "WHERE instance_id = ? ORDER BY created_at DESC LIMIT 1",
            (instance_id,),
        ).fetchone()
    return (row[0] if row else None, None)


async def _no_pending(_pane: str) -> bool:
    return False


def _queue_row(db_path: Any, queue_id: str) -> tuple[str, str, str | None]:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, payload, event_payload_json FROM pane_write_queue WHERE id = ?",
            (queue_id,),
        ).fetchone()
    assert row is not None
    return row


@pytest.mark.asyncio
async def test_gated_event_is_whole_payload_no_text_or_effects(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = app_env.main
    attempts: list[tuple[str, str, bool]] = []
    effects: list[tuple[str | None, str | None, str]] = []

    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _no_pending)
    monkeypatch.setattr(
        main.shared, "resolve_instance_pane", lambda iid: _resolve_from_queue(app_env, iid)
    )

    async def _gated(pane: str, payload: str, *, clear_prompt: bool = False, **_kwargs):
        attempts.append((pane, payload, clear_prompt))
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": "",
            "gated": True,
            "gate_reason": "typing_guard",
            "gate": {"reason": "typing_guard", "suppressed": True},
            "verification_status": "gated",
            "verified_by": None,
        }

    async def _flag(instance_id: str | None = None, *, tmux_pane: str | None = None, actor: str):
        effects.append((instance_id, tmux_pane, actor))

    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _gated)
    monkeypatch.setattr(main, "_flag_hook_driven", _flag)

    queued = await main.enqueue_pane_write(
        instance_id="fg-atomic",
        tmux_pane="%9",
        source="brief",
        purpose="brief_send",
        payload="lossless event",
        hook_driven=True,
    )
    results = await main.process_pane_write_queue_once(queued["id"])

    assert results[0]["status"] == main.PANE_WRITE_PENDING
    assert attempts == [("%9", "lossless event", True)]
    assert effects == [], "held event effects must not run before release"
    status, payload, event_json = _queue_row(app_env.db_path, queued["id"])
    assert status == "pending"
    assert payload == "lossless event"
    event = json.loads(event_json or "{}")
    assert event["text"] == "lossless event"
    assert event["submit"] is True
    assert event["effects"]["hook_driven"] is True


@pytest.mark.asyncio
async def test_released_event_replays_like_fresh_arrival_with_submit_and_effects(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = app_env.main
    delivered: list[tuple[str, str, bool]] = []
    effects: list[tuple[str | None, str | None, str]] = []
    gate_open = False

    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _no_pending)
    monkeypatch.setattr(
        main.shared, "resolve_instance_pane", lambda iid: _resolve_from_queue(app_env, iid)
    )

    async def _send(pane: str, payload: str, *, clear_prompt: bool = False, **_kwargs):
        if not gate_open:
            return {
                "returncode": 1,
                "stdout": "",
                "stderr": "",
                "gated": True,
                "gate_reason": "typing_guard",
                "gate": {"reason": "typing_guard", "suppressed": True},
                "verification_status": "gated",
                "verified_by": None,
            }
        delivered.append((pane, payload, clear_prompt))
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "gated": False,
            "verification_status": "submitted",
            "verified_by": "UserPromptSubmit",
        }

    async def _flag(instance_id: str | None = None, *, tmux_pane: str | None = None, actor: str):
        effects.append((instance_id, tmux_pane, actor))

    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _send)
    monkeypatch.setattr(main, "_flag_hook_driven", _flag)

    queued = await main.enqueue_pane_write(
        instance_id="fg-atomic",
        tmux_pane="%9",
        source="brief",
        purpose="brief_send",
        payload="fresh-equivalent",
        hook_driven=True,
    )
    first = await main.process_pane_write_queue_once(queued["id"])
    assert first[0]["status"] == main.PANE_WRITE_PENDING
    assert delivered == []
    assert effects == []

    gate_open = True
    replay = await main.process_pane_write_queue_once(queued["id"])

    assert replay[0]["status"] == main.PANE_WRITE_SENT
    assert replay[0]["verification_status"] == "submitted"
    assert delivered == [("%9", "fresh-equivalent", True)]
    assert effects == [("fg-atomic", "%9", "release:brief")]
    assert _queue_row(app_env.db_path, queued["id"])[0] == "sent"


@pytest.mark.asyncio
async def test_unverified_submit_is_not_delivered_false_positive(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = app_env.main
    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _no_pending)
    monkeypatch.setattr(
        main.shared, "resolve_instance_pane", lambda iid: _resolve_from_queue(app_env, iid)
    )

    async def _unverified(pane: str, payload: str, *, clear_prompt: bool = False, **_kwargs):
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "gated": False,
            "verification_status": "unverified",
            "verified_by": None,
        }

    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _unverified)

    queued = await main.enqueue_pane_write(
        instance_id="fg-atomic",
        tmux_pane="%9",
        source="brief",
        purpose="brief_send",
        payload="text landed maybe enter did not",
    )
    result = (await main.process_pane_write_queue_once(queued["id"]))[0]

    assert result["status"] == main.PANE_WRITE_UNVERIFIED
    assert result["verification_status"] == "unverified"
    assert _queue_row(app_env.db_path, queued["id"])[0] == "unverified"


@pytest.mark.asyncio
async def test_hook_effect_failure_does_not_reclassify_sent_event(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = app_env.main
    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _no_pending)
    monkeypatch.setattr(
        main.shared, "resolve_instance_pane", lambda iid: _resolve_from_queue(app_env, iid)
    )

    async def _submitted(pane: str, payload: str, *, clear_prompt: bool = False, **_kwargs):
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "gated": False,
            "verification_status": "submitted",
            "verified_by": "UserPromptSubmit",
        }

    async def _bad_flag(*_args, **_kwargs):
        raise RuntimeError("flag db busy")

    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _submitted)
    monkeypatch.setattr(main, "_flag_hook_driven", _bad_flag)

    queued = await main.enqueue_pane_write(
        instance_id="fg-atomic",
        tmux_pane="%9",
        source="brief",
        purpose="brief_send",
        payload="submitted despite effect telemetry failure",
        hook_driven=True,
    )
    result = (await main.process_pane_write_queue_once(queued["id"]))[0]

    assert result["status"] == main.PANE_WRITE_SENT
    assert result["hook_effect_error"] == "flag db busy"
    assert _queue_row(app_env.db_path, queued["id"])[0] == "sent"


@pytest.mark.asyncio
async def test_operation_id_reuse_with_flipped_hook_driven_is_rejected(
    app_env: Any,
) -> None:
    """The deferred hook_driven effect is part of an operation's identity.

    A reuse of the same operation_id + target + payload but a different
    hook_driven value must not silently release the first row's stored effect;
    it is a different operation and is rejected like a target/payload mismatch.
    """
    main = app_env.main

    first = await main.enqueue_pane_write(
        instance_id="fg-atomic",
        tmux_pane="%9",
        source="brief",
        purpose="brief_send",
        payload="same payload",
        hook_driven=True,
        operation_id="op-flip",
    )
    assert first["id"] == "op-flip"

    with pytest.raises(ValueError, match="different target/payload/effects"):
        await main.enqueue_pane_write(
            instance_id="fg-atomic",
            tmux_pane="%9",
            source="brief",
            purpose="brief_send",
            payload="same payload",
            hook_driven=False,
            operation_id="op-flip",
        )

    # An identical replay (same hook_driven) still dedupes onto the original row.
    replay = await main.enqueue_pane_write(
        instance_id="fg-atomic",
        tmux_pane="%9",
        source="brief",
        purpose="brief_send",
        payload="same payload",
        hook_driven=True,
        operation_id="op-flip",
    )
    assert replay["id"] == "op-flip"
