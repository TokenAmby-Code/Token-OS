"""Physical enforcement guard queue: split from comms and re-check on drop."""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _clean_enforcement_guard(app_env: Any, monkeypatch: pytest.MonkeyPatch):
    main = app_env.main
    main._phone_enforcement_guard_queue.clear()
    main._PHONE_ENFORCE_STATE.clear()
    main.PHONE_STATE.update(
        {
            "current_app": "youtube",
            "is_distracted": True,
            "app_opened_at": "2026-07-04T12:00:00",
        }
    )
    main.timer_engine._break_balance_ms = 0
    main.timer_engine._productivity_active = False
    monkeypatch.setattr(main, "is_quiet_hours", lambda *args, **kwargs: False)
    monkeypatch.setattr(main, "_dictation_active", lambda: False)

    async def _custodes_noop(*args, **kwargs):
        return {"intervention_dispatched": False, "reason": "test_noop"}

    monkeypatch.setattr(main, "handle_custodes_state_event", _custodes_noop)
    yield
    main._phone_enforcement_guard_queue.clear()
    main._PHONE_ENFORCE_STATE.clear()


async def test_phone_enforcement_is_held_while_any_typing_guard_active(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = app_env.main
    calls: dict[str, list] = {"enforce": [], "redirect": []}

    async def _enforce(req):  # pragma: no cover - assertion path
        calls["enforce"].append(req)
        return {"fired": True}

    monkeypatch.setattr(main, "_typing_guard_active", lambda: True)
    monkeypatch.setattr(main, "enforce", _enforce)
    monkeypatch.setattr(
        main, "_send_eject_to_phone", lambda method: calls["redirect"].append(method)
    )

    main.start_enforcement_cascade("youtube")

    assert len(main._phone_enforcement_guard_queue) == 1
    assert main._phone_enforcement_guard_queue[0]["guard_reason"] == "typing_guard"
    assert calls == {"enforce": [], "redirect": []}


async def test_phone_enforcement_voided_by_activity_during_hold_noops_on_drop(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = app_env.main
    calls: dict[str, list] = {"enforce": [], "redirect": []}

    async def _enforce(req):  # pragma: no cover - assertion path
        calls["enforce"].append(req)
        return {"fired": True}

    monkeypatch.setattr(main, "_typing_guard_active", lambda: True)
    monkeypatch.setattr(main, "enforce", _enforce)
    monkeypatch.setattr(
        main, "_send_eject_to_phone", lambda method: calls["redirect"].append(method)
    )

    main.start_enforcement_cascade("youtube")
    assert len(main._phone_enforcement_guard_queue) == 1

    # The Emperor's activity while the guard held voided the live condition.
    main.PHONE_STATE["is_distracted"] = False
    main.PHONE_STATE["current_app"] = None
    monkeypatch.setattr(main, "_typing_guard_active", lambda: False)

    results = await main._phone_enforcement_guard_queue_flush_once()

    assert results == [
        {
            "queue": "phone_enforcement",
            "fired": False,
            "reason": "voided",
            "stale_reason": "phone_distraction_cleared",
        }
    ]
    assert main._phone_enforcement_guard_queue == []
    assert calls == {"enforce": [], "redirect": []}


async def test_phone_enforcement_rechecks_productivity_on_guard_drop(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = app_env.main
    calls: dict[str, list] = {"enforce": [], "redirect": []}

    async def _enforce(req):  # pragma: no cover - assertion path
        calls["enforce"].append(req)
        return {"fired": True}

    monkeypatch.setattr(main, "_typing_guard_active", lambda: True)
    monkeypatch.setattr(main, "enforce", _enforce)
    monkeypatch.setattr(
        main, "_send_eject_to_phone", lambda method: calls["redirect"].append(method)
    )

    main.start_enforcement_cascade("youtube")
    assert len(main._phone_enforcement_guard_queue) == 1

    # The app is still foregrounded, but live work resumed while the guard held;
    # the original no-productivity enforcement condition is no longer true.
    main.timer_engine._productivity_active = True
    monkeypatch.setattr(main, "_typing_guard_active", lambda: False)

    results = await main._phone_enforcement_guard_queue_flush_once()

    assert results[0]["reason"] == "voided"
    assert results[0]["stale_reason"] == "productivity_active"
    assert main._phone_enforcement_guard_queue == []
    assert calls == {"enforce": [], "redirect": []}


async def test_phone_enforcement_still_true_fires_once_on_guard_drop(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = app_env.main
    calls: dict[str, list] = {"enforce": [], "redirect": []}

    async def _enforce(req):
        calls["enforce"].append(
            {
                "message": req.message,
                "intensity": req.intensity,
                "source": req.source,
                "notify": req.notify,
            }
        )
        return {"fired": True}

    monkeypatch.setattr(main, "_typing_guard_active", lambda: True)
    monkeypatch.setattr(main, "enforce", _enforce)
    monkeypatch.setattr(
        main, "_send_eject_to_phone", lambda method: calls["redirect"].append(method)
    )

    main.start_enforcement_cascade("youtube")
    main.start_enforcement_cascade("youtube")
    assert len(main._phone_enforcement_guard_queue) == 1

    monkeypatch.setattr(main, "_typing_guard_active", lambda: False)
    results = await main._phone_enforcement_guard_queue_flush_once()

    assert len(results) == 1
    assert results[0]["queue"] == "phone_enforcement"
    assert results[0]["fired"] is True
    assert results[0]["reason"] == "flushed_after_guard_drop"
    assert main._phone_enforcement_guard_queue == []
    assert calls["enforce"] == [
        {
            "message": "Close youtube",
            "intensity": 40,
            "source": "phone_distraction_youtube",
            "notify": False,
        }
    ]
    assert calls["redirect"] == ["redirect"]

    # A second drain after the same guard drop must not re-fire the transaction.
    again = await main._phone_enforcement_guard_queue_flush_once()
    assert again == []
    assert len(calls["enforce"]) == 1
    assert calls["redirect"] == ["redirect"]


async def test_phone_enforcement_requeues_if_guard_reappears_before_shock(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = app_env.main
    calls: dict[str, list] = {"enforce": [], "redirect": []}

    async def _enforce(req):
        calls["enforce"].append(req)
        return {"fired": False, "blocked_by": "typing_guard"}

    monkeypatch.setattr(main, "_typing_guard_active", lambda: True)
    monkeypatch.setattr(main, "enforce", _enforce)
    monkeypatch.setattr(
        main, "_send_eject_to_phone", lambda method: calls["redirect"].append(method)
    )

    main.start_enforcement_cascade("youtube")
    assert len(main._phone_enforcement_guard_queue) == 1

    # Guard is clear when the queue starts draining, but the physical chokepoint
    # sees a new guard before Pavlok fires. Redirect must not bypass it.
    monkeypatch.setattr(main, "_typing_guard_active", lambda: False)
    results = await main._phone_enforcement_guard_queue_flush_once()

    assert results[0]["fired"] is False
    assert results[0]["reason"] == "guard_reappeared"
    assert len(main._phone_enforcement_guard_queue) == 1
    assert main._phone_enforcement_guard_queue[0]["guard_reason"] == "typing_guard"
    assert len(calls["enforce"]) == 1
    assert calls["redirect"] == []


async def test_comms_queue_keeps_enqueue_and_drain_no_void_semantics(
    app_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A voided enforcement condition must not make brief/talk/comms vanish."""
    main = app_env.main
    sent: list[dict] = []

    async def _resolve(instance_id):
        return "%9", None

    async def _no_pending_input(_pane: str) -> bool:
        return False

    async def _send(pane, payload, *, clear_prompt=False, **kwargs):
        sent.append({"pane": pane, "payload": payload, "clear_prompt": clear_prompt})
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "gated": False,
            "verification_status": "submitted",
            "verified_by": "UserPromptSubmit",
        }

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve)
    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _no_pending_input)
    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _send)

    # Enforcement live condition is void. Comms must not consult it.
    main.PHONE_STATE["is_distracted"] = False
    main.PHONE_STATE["current_app"] = None

    queued = await main.enqueue_pane_write(
        instance_id="fg-1",
        tmux_pane="%9",
        source="brief",
        purpose="brief_send",
        payload="brief still delivers",
    )
    results = await main.process_pane_write_queue_once(queued["id"])

    assert results[0]["status"] == main.PANE_WRITE_SENT
    assert sent == [{"pane": "%9", "payload": "brief still delivers", "clear_prompt": True}]
    with sqlite3.connect(app_env.db_path) as conn:
        status = conn.execute(
            "SELECT status FROM pane_write_queue WHERE id = ?", (queued["id"],)
        ).fetchone()[0]
    assert status == "sent"
