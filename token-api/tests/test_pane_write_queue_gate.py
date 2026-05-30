"""Queue-side of the send-gate delivery-proof fix.

Pins the corrected contract for automated pane writes:

  * ``_tmux_send_payload_then_submit`` translates a ``TmuxSendGated`` into a
    structured ``gated`` result (NOT ``sent``); a successful byte-issue is
    reported ``unverified`` (delivery not yet proven), never a default ``sent``.
  * ``process_pane_write_queue_once`` keeps a gated item ``pending`` so the
    periodic worker re-drains it — the typing guard queues, it does not bounce.
  * Once the gate clears, the very same pending item flushes to ``sent``.
"""

from __future__ import annotations

import sqlite3
from typing import Any


async def _no_pending_input(_pane: str) -> bool:
    return False


def _fetch_status(db_path: Any, queue_id: str) -> str | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM pane_write_queue WHERE id = ?", (queue_id,)
        ).fetchone()
    return row[0] if row else None


# ---- translation layer: TmuxSendGated -> structured gated result ------------


async def test_send_payload_translates_gate_to_gated_result(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    import tmuxctl.tmux_adapter as ta

    def _raise_gated(self, target, text, **kwargs):
        raise ta.TmuxSendGated({"reason": "typing_guard", "suppressed": True})

    monkeypatch.setattr(ta.TmuxAdapter, "send_text_then_submit", _raise_gated)

    result = await main._tmux_send_payload_then_submit("%9", "hello FG")

    assert result["gated"] is True
    assert result["gate_reason"] == "typing_guard"
    assert result["verification_status"] == "gated"
    assert result["verified_by"] is None
    assert result["returncode"] != 0


async def test_send_payload_success_is_unverified_not_sent(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    import tmuxctl.tmux_adapter as ta

    def _ok(self, target, text, **kwargs):
        return None

    monkeypatch.setattr(ta.TmuxAdapter, "send_text_then_submit", _ok)

    result = await main._tmux_send_payload_then_submit("%9", "hello FG")

    assert result["returncode"] == 0
    assert result["verification_status"] == "unverified", "bytes issued != proven delivery"
    assert result["verified_by"] is None
    assert not result.get("gated")


# ---- queue handling: gated stays pending, success goes sent -----------------


async def test_gated_send_keeps_item_pending(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _no_pending_input)

    async def _gated(pane, payload, *, clear_prompt=False):
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": "",
            "gated": True,
            "gate_reason": "typing_guard",
            "gate": {"reason": "typing_guard"},
            "verification_status": "gated",
            "verified_by": None,
        }

    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _gated)

    queued = await main.enqueue_pane_write(
        instance_id="fg-1",
        tmux_pane="%9",
        source="brief",
        purpose="dispatch",
        payload="brief for FG",
    )
    results = await main.process_pane_write_queue_once(queued["id"])

    assert len(results) == 1
    assert results[0]["status"] == main.PANE_WRITE_PENDING
    assert results[0]["reason"].startswith("send_gated")
    # The DB row stays pending so the periodic worker retries it (no bounce).
    assert _fetch_status(app_env.db_path, queued["id"]) == "pending"


async def test_gated_then_cleared_gate_flushes_to_sent(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _no_pending_input)

    async def _gated(pane, payload, *, clear_prompt=False):
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": "",
            "gated": True,
            "gate_reason": "typing_guard",
            "gate": {"reason": "typing_guard"},
            "verification_status": "gated",
            "verified_by": None,
        }

    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _gated)

    queued = await main.enqueue_pane_write(
        instance_id="fg-1",
        tmux_pane="%9",
        source="brief",
        purpose="dispatch",
        payload="brief for FG",
    )
    await main.process_pane_write_queue_once(queued["id"])
    assert _fetch_status(app_env.db_path, queued["id"]) == "pending"

    # Guard clears: the same still-pending item now delivers on the next drain.
    async def _ok(pane, payload, *, clear_prompt=False):
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "gated": False,
            "verification_status": "unverified",
            "verified_by": None,
        }

    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _ok)
    results = await main.process_pane_write_queue_once(queued["id"])

    assert len(results) == 1
    assert results[0]["status"] == main.PANE_WRITE_SENT
    assert results[0]["verification_status"] == "unverified"
    assert _fetch_status(app_env.db_path, queued["id"]) == "sent"
