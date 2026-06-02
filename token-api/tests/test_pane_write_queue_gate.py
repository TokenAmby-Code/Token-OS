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


# ---- brief clears/replaces a stale composer instead of deferring forever -----


async def _has_pending_input(_pane: str) -> bool:
    return True


async def test_brief_clears_stale_composer_instead_of_deferring(
    app_env: Any, monkeypatch: Any
) -> None:
    """A brief to a composer that already holds text must NOT wedge as deferred.

    Live symptom: a leftover draft in the target composer kept the additive
    deferral returning pending forever, so the brief never delivered. brief now
    clears/replaces (clear_prompt) and submits.
    """
    main = app_env.main
    # Composer has text — the old behavior deferred here permanently.
    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _has_pending_input)

    seen: dict[str, Any] = {}

    async def _ok(pane, payload, *, clear_prompt=False):
        seen["clear_prompt"] = clear_prompt
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "gated": False,
            "verification_status": "submitted",
            "verified_by": "composer_cleared",
        }

    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _ok)

    queued = await main.enqueue_pane_write(
        instance_id="fg-1",
        tmux_pane="%9",
        source="brief",
        purpose="brief_send",
        payload="brief for FG",
    )
    results = await main.process_pane_write_queue_once(queued["id"])

    assert len(results) == 1
    assert results[0]["status"] == main.PANE_WRITE_SENT
    assert seen["clear_prompt"] is True, "brief must clear/replace the composer"
    assert _fetch_status(app_env.db_path, queued["id"]) == "sent"


async def test_nonbrief_still_defers_on_pending_input(app_env: Any, monkeypatch: Any) -> None:
    """Regression guard: non-brief writes keep deferring on a busy composer."""
    main = app_env.main
    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _has_pending_input)

    queued = await main.enqueue_pane_write(
        instance_id="fg-1",
        tmux_pane="%9",
        source="enforcement",
        purpose="nudge",
        payload="nudge",
    )
    results = await main.process_pane_write_queue_once(queued["id"])

    assert len(results) == 1
    assert results[0]["status"] == main.PANE_WRITE_PENDING
    assert results[0]["reason"] == "dispatch_deferred"
    assert _fetch_status(app_env.db_path, queued["id"]) == "pending"


# ---- submission confirmation: clear_prompt upgrades verification ------------


async def test_clear_prompt_confirms_submission_when_composer_clears(
    app_env: Any, monkeypatch: Any
) -> None:
    main = app_env.main
    import tmuxctl.tmux_adapter as ta

    monkeypatch.setattr(ta.TmuxAdapter, "send_text_then_submit", lambda self, t, x, **k: None)
    # After submit the composer is empty -> submission confirmed.
    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _no_pending_input)

    result = await main._tmux_send_payload_then_submit("%9", "hello FG", clear_prompt=True)

    assert result["returncode"] == 0
    assert result["verification_status"] == "submitted"
    assert result["verified_by"] == "composer_cleared"


async def test_clear_prompt_stays_unverified_when_composer_not_cleared(
    app_env: Any, monkeypatch: Any
) -> None:
    main = app_env.main
    import tmuxctl.tmux_adapter as ta

    monkeypatch.setattr(ta.TmuxAdapter, "send_text_then_submit", lambda self, t, x, **k: None)
    # Composer still holds text -> submit not proven, stays unverified.
    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _has_pending_input)

    result = await main._tmux_send_payload_then_submit("%9", "hello FG", clear_prompt=True)

    assert result["returncode"] == 0
    assert result["verification_status"] == "unverified"
    assert result["verified_by"] is None


# ---- end-to-end incident guard: real gate -> never "sent" -------------------


async def test_real_gate_suppression_never_reports_sent_end_to_end(
    app_env: Any, monkeypatch: Any
) -> None:
    """The 2026-05-30 incident, pinned end-to-end through production code.

    Unlike the translation tests above (which stub the adapter), this drives the
    REAL ``TmuxAdapter`` gate: ``send_gate.evaluate`` suppresses the byte-bearing
    literal send, ``run()`` writes nothing, ``send_text_then_submit`` raises
    ``TmuxSendGated``, and the translation layer must surface ``gated`` — never
    the false ``sent`` that reported a brief delivered three times when it never
    reached the pane. No real ``tmux`` byte is ever issued.
    """
    main = app_env.main
    import tmuxctl.tmux_adapter as ta

    real_sends: list[list[str]] = []

    def _suppress(args_tuple):
        # The live gate suppresses every pane send (the C-u clear AND the
        # byte-bearing literal) while quiet hours / the typing guard is active.
        if "send-keys" in args_tuple:
            return {"reason": "typing_guard", "suppressed": True}
        return None

    def _fake_subprocess_run(cmd, *a, **k):  # pragma: no cover - must never fire for a send
        real_sends.append(cmd)
        raise AssertionError(f"a gated send must issue zero tmux bytes, got: {cmd}")

    monkeypatch.setattr(ta.send_gate, "evaluate", _suppress)
    monkeypatch.setattr(ta.send_gate, "record_suppression", lambda *a, **k: None)
    monkeypatch.setattr(ta.subprocess, "run", _fake_subprocess_run)
    monkeypatch.setattr(ta.time, "sleep", lambda _s: None)

    result = await main._tmux_send_payload_then_submit("%9", "brief for FG")

    assert result["gated"] is True
    assert result["gate_reason"] == "typing_guard"
    assert result["verification_status"] == "gated"
    assert result["verification_status"] != "sent"
    assert result["verified_by"] is None
    assert result["returncode"] != 0
    assert real_sends == [], "the suppressed send must reach no tmux subprocess"

    # And the queue must keep it pending (re-queueable), never mark it sent.
    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _no_pending_input)
    queued = await main.enqueue_pane_write(
        instance_id="fg-1",
        tmux_pane="%9",
        source="brief",
        purpose="dispatch",
        payload="brief for FG",
    )
    results = await main.process_pane_write_queue_once(queued["id"])

    assert results[0]["status"] == main.PANE_WRITE_PENDING
    assert results[0]["status"] != main.PANE_WRITE_SENT
    assert results[0]["reason"].startswith("send_gated")
    assert _fetch_status(app_env.db_path, queued["id"]) == "pending"
