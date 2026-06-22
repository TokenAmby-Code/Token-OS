"""Rowless attended-pane sends must queue instead of dropping gated payloads."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest


async def _no_registry_pane(_instance_id: str):
    return (None, None)


def _pending_queue_row(db_path: Any) -> tuple[str, str, str] | None:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            """
            SELECT id, status, payload
            FROM pane_write_queue
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()


@pytest.mark.asyncio
async def test_rowless_attended_pending_input_gated_send_enqueues_without_writing(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = app_env.main
    writes: list[tuple[str, str, bool]] = []

    async def _resolve_pane(pane: str | None) -> str | None:
        return "%44" if pane == "%44" else None

    async def _live_agent(_pane: str | None) -> str | None:
        return "codex"

    async def _gated(pane: str, payload: str, *, clear_prompt: bool = False, **_kwargs):
        # This is the contract of the lower tmux boundary when the typing guard
        # is active: no bytes reached tmux. Count attempts separately from writes.
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": "",
            "operation": "tmuxctl.send_text_then_submit",
            "gated": True,
            "gate_reason": "typing_guard",
            "gate": {"reason": "typing_guard", "suppressed": True},
            "verification_status": "gated",
            "verified_by": None,
            "pane": pane,
            "instance_id": None,
        }

    monkeypatch.setattr(main.shared, "resolve_tmux_pane_id", _resolve_pane)
    monkeypatch.setattr(main.shared, "resolve_instance_pane", _no_registry_pane)
    monkeypatch.setattr(main, "_pane_live_agent_engine", _live_agent)
    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _gated)

    result = await main._direct_tmux_pane_delivery(
        "%44",
        "attended brief must queue",
        source="brief",
        purpose="brief_send",
        clear_prompt=True,
    )

    assert result["status"] == main.PANE_WRITE_PENDING
    assert result["reason"].startswith("send_gated:typing_guard")
    row = _pending_queue_row(app_env.db_path)
    assert row is not None
    queue_id, status, payload = row
    assert result["queue_id"] == queue_id
    assert status == main.PANE_WRITE_PENDING
    assert payload == "attended brief must queue"
    assert writes == [], "gated direct sends must not write through to tmux"


@pytest.mark.asyncio
async def test_rowless_attended_queue_drains_after_draft_clears(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = app_env.main
    delivered: list[tuple[str, str, bool]] = []
    gate_open = False

    async def _resolve_pane(pane: str | None) -> str | None:
        return "%44" if pane == "%44" else None

    async def _live_agent(_pane: str | None) -> str | None:
        return "codex"

    async def _send(pane: str, payload: str, *, clear_prompt: bool = False, **_kwargs):
        if not gate_open:
            return {
                "returncode": 1,
                "stdout": "",
                "stderr": "",
                "operation": "tmuxctl.send_text_then_submit",
                "gated": True,
                "gate_reason": "typing_guard",
                "gate": {"reason": "typing_guard", "suppressed": True},
                "verification_status": "gated",
                "verified_by": None,
                "pane": pane,
                "instance_id": None,
            }
        delivered.append((pane, payload, clear_prompt))
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "operation": "tmuxctl.send_text_then_submit",
            "gated": False,
            "verification_status": "submitted",
            "verified_by": "composer_cleared",
            "pane": pane,
            "instance_id": None,
        }

    monkeypatch.setattr(main.shared, "resolve_tmux_pane_id", _resolve_pane)
    monkeypatch.setattr(main.shared, "resolve_instance_pane", _no_registry_pane)
    monkeypatch.setattr(main, "_pane_live_agent_engine", _live_agent)
    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _send)

    pending = await main._direct_tmux_pane_delivery(
        "%44",
        "deliver after clear",
        source="brief",
        purpose="brief_send",
        clear_prompt=True,
    )

    assert pending["status"] == main.PANE_WRITE_PENDING
    assert delivered == []

    gate_open = True
    drained = await main.process_pane_write_queue_once(pending["queue_id"])

    assert len(drained) == 1
    assert drained[0]["status"] == main.PANE_WRITE_SENT
    assert delivered == [("%44", "deliver after clear", True)]
