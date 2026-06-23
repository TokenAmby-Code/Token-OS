"""Hook-side of the delivery-proof fix (Problem C).

``_direct_pane_write`` used to discard the adapter result and hardcode
``{"status": "sent"}`` (and fall back to a raw ``tmux send-keys`` that called
rc==0 "sent"), so the stop-hook delivery path reported "sent" while the
universal gate had suppressed the write — "fired but nothing arrived".

It now routes through main.py's gate-aware ``_tmux_send_payload_then_submit``
and reports the truth:

  * gated      -> never "sent"; the durable pane_write_queue row stays
                  ``pending`` so the periodic worker re-drains it.
  * unverified -> bytes issued but unproven; never "sent" (the proof belt
                  upgrades it asynchronously). The queue row is terminal
                  ``sent`` to avoid a double send.
  * submitted  -> verified delivery -> "sent".
  * error      -> "failed".
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from typing import Any

# ── unit: _direct_pane_write maps the primitive result to delivery truth ──────


async def test_direct_pane_write_gated_is_never_sent(app_env: Any, monkeypatch: Any) -> None:
    hooks = sys.modules["routes.hooks"]

    async def _gated(pane, payload):
        return {
            "returncode": 1,
            "gated": True,
            "gate_reason": "typing_guard",
            "verification_status": "gated",
        }

    monkeypatch.setattr(hooks, "_tmux_send_payload_then_submit", _gated)

    result = await hooks._direct_pane_write("%9", "hello")

    assert result["status"] == "gated"
    assert result["status"] != "sent"
    assert result["gate_reason"] == "typing_guard"


async def test_direct_pane_write_bytes_issued_unproven_is_unverified(
    app_env: Any, monkeypatch: Any
) -> None:
    hooks = sys.modules["routes.hooks"]

    async def _unverified(pane, payload):
        return {"returncode": 0, "gated": False, "verification_status": "unverified"}

    monkeypatch.setattr(hooks, "_tmux_send_payload_then_submit", _unverified)

    result = await hooks._direct_pane_write("%9", "hello")

    assert result["status"] == "unverified", "bytes issued != proven delivery"
    assert result["status"] != "sent"


async def test_direct_pane_write_verified_submission_is_sent(
    app_env: Any, monkeypatch: Any
) -> None:
    hooks = sys.modules["routes.hooks"]

    async def _submitted(pane, payload):
        return {
            "returncode": 0,
            "gated": False,
            "verification_status": "submitted",
            "verified_by": "composer_cleared",
        }

    monkeypatch.setattr(hooks, "_tmux_send_payload_then_submit", _submitted)

    result = await hooks._direct_pane_write("%9", "hello")

    assert result["status"] == "sent"


async def test_direct_pane_write_send_error_is_failed(app_env: Any, monkeypatch: Any) -> None:
    hooks = sys.modules["routes.hooks"]

    async def _err(pane, payload):
        return {"returncode": 1, "gated": False, "stderr": "no live instance"}

    monkeypatch.setattr(hooks, "_tmux_send_payload_then_submit", _err)

    result = await hooks._direct_pane_write("%9", "hello")

    assert result["status"] == "failed"
    assert result["error"] == "no live instance"


async def test_direct_pane_write_uninitialized_primitive_is_failed_not_sent(
    app_env: Any, monkeypatch: Any
) -> None:
    """If main.py never wired the primitive, fail loudly — never fake a send."""
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "_tmux_send_payload_then_submit", None)

    result = await hooks._direct_pane_write("%9", "hello")

    assert result["status"] == "failed"
    assert result["status"] != "sent"


# ── integration: stop-hook delivery persists the truth to the queue ───────────


def _insert_instance(db_path, instance_id, pane=None, parent=None, status="idle"):
    # Pane geometry is no longer stored on the instance row; it is resolved live
    # from the @INSTANCE_ID stamp. The ``pane`` kwarg is retained for caller
    # readability but no longer persisted. Stop-hook delivery resolves the pane
    # from the subscription's ``subscriber_pane`` column, not from here.
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO instances
           (id, name, working_dir, origin_type, device_id, status,
            commander_type, commander_id)
           VALUES (?, ?, ?, 'local', 'Mac-Mini', ?, 'emperor', NULL)""",
        (instance_id, instance_id, "/tmp", status),
    )
    conn.commit()
    conn.close()


def _subscribe(db_path, *, target, target_pane, subscriber, subscriber_pane):
    conn = sqlite3.connect(db_path)
    conn.execute(
        """INSERT INTO stop_hook_subscriptions
           (target_instance_id, target_pane, subscriber_instance_id, subscriber_pane,
            event, delivery, status)
           VALUES (?, ?, ?, ?, 'stop', 'prompt', 'active')""",
        (target, target_pane, subscriber, subscriber_pane),
    )
    conn.commit()
    conn.close()


_TAIL = (
    '{"type":"assistant","message":{"role":"assistant",'
    '"content":[{"type":"text","text":"STOP_OK"}]}}'
)


def _row(db_path, query, params=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(query, params).fetchone()
    conn.close()
    return dict(row) if row else None


def _drive_stop_delivery(app_env, monkeypatch, send_fake):
    """Insert parent←child subscription, fire child Stop, return the result."""
    hooks = sys.modules["routes.hooks"]
    monkeypatch.setattr(hooks, "_tmux_send_payload_then_submit", send_fake)

    _insert_instance(app_env.db_path, "parent-c", pane="%20")
    _insert_instance(app_env.db_path, "child-c", pane="%21", parent="parent-c")
    _subscribe(
        app_env.db_path,
        target="child-c",
        target_pane="%21",
        subscriber="parent-c",
        subscriber_pane="%20",
    )

    async def run():
        return await hooks.handle_stop({"session_id": "child-c", "transcript_tail": _TAIL})

    return asyncio.run(run())


def test_gated_delivery_stays_pending_for_requeue(app_env, monkeypatch):
    """A gated stop-hook send is never "sent"; the queue row stays pending."""

    async def _gated(pane, payload):
        return {
            "returncode": 1,
            "gated": True,
            "gate_reason": "typing_guard",
            "verification_status": "gated",
        }

    result = _drive_stop_delivery(app_env, monkeypatch, _gated)
    sub = result["stop_subscriptions"][0]

    assert sub["status"] == "gated"
    assert sub["status"] != "sent"
    # Durable row stays pending so the periodic worker re-drains it (requeue).
    queue_row = _row(
        app_env.db_path, "SELECT status FROM pane_write_queue WHERE id = ?", (sub["queue_id"],)
    )
    assert queue_row["status"] == "pending"
    delivery = _row(
        app_env.db_path,
        "SELECT status FROM stop_hook_deliveries WHERE id = ?",
        (sub["delivery_id"],),
    )
    assert delivery["status"] == "gated"


def test_unverified_delivery_is_terminal_but_not_sent(app_env, monkeypatch):
    """Bytes issued without proof: recorded 'unverified', queue terminal (no double-send)."""

    async def _unverified(pane, payload):
        return {"returncode": 0, "gated": False, "verification_status": "unverified"}

    result = _drive_stop_delivery(app_env, monkeypatch, _unverified)
    sub = result["stop_subscriptions"][0]

    assert sub["status"] == "unverified"
    assert sub["status"] != "sent"
    # Queue row is terminal 'sent' (bytes left the building) so the periodic
    # worker does NOT re-send; the proof belt owns the async upgrade.
    queue_row = _row(
        app_env.db_path, "SELECT status FROM pane_write_queue WHERE id = ?", (sub["queue_id"],)
    )
    assert queue_row["status"] == "sent"
    delivery = _row(
        app_env.db_path,
        "SELECT status FROM stop_hook_deliveries WHERE id = ?",
        (sub["delivery_id"],),
    )
    assert delivery["status"] == "unverified"


def test_verified_delivery_is_sent(app_env, monkeypatch):
    """A confirmed submission (composer cleared) is reported and persisted as sent."""

    async def _submitted(pane, payload):
        return {
            "returncode": 0,
            "gated": False,
            "verification_status": "submitted",
            "verified_by": "composer_cleared",
        }

    result = _drive_stop_delivery(app_env, monkeypatch, _submitted)
    sub = result["stop_subscriptions"][0]

    assert sub["status"] == "sent"
    queue_row = _row(
        app_env.db_path, "SELECT status FROM pane_write_queue WHERE id = ?", (sub["queue_id"],)
    )
    assert queue_row["status"] == "sent"
    delivery = _row(
        app_env.db_path,
        "SELECT status FROM stop_hook_deliveries WHERE id = ?",
        (sub["delivery_id"],),
    )
    assert delivery["status"] == "sent"
