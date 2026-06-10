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

import pytest


@pytest.fixture(autouse=True)
def _resolve_to_queued_pane(app_env, monkeypatch):
    """Tier 2(b): ``process_pane_write_queue_once`` resolves ``instance_id`` ->
    pane live at dequeue (tmuxctl owns resolution). There is no tmux server in
    tests, so the real resolver would fail closed and cancel every item. Stub it
    to echo the queued row's stored pane — the dual-write reality where the live
    pane still matches the stored one — so these gate/delivery tests exercise
    their actual contract. File-scoped so the real resolver elsewhere is untouched;
    individual tests override this to assert live-resolution / fail-closed.
    """
    main = app_env.main

    async def _resolve(instance_id):
        with sqlite3.connect(app_env.db_path) as conn:
            row = conn.execute(
                "SELECT tmux_pane FROM pane_write_queue WHERE instance_id = ? "
                "ORDER BY created_at DESC LIMIT 1",
                (instance_id,),
            ).fetchone()
        pane = row[0] if row else None
        return (pane, None)

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve)


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


async def test_send_payload_tabs_codex_skill_before_submit(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    import tmuxctl.tmux_adapter as ta

    seen: dict[str, Any] = {}

    def _ok(self, target, text, **kwargs):
        seen["target"] = target
        seen["text"] = text
        seen["kwargs"] = kwargs
        return None

    monkeypatch.setattr(ta.TmuxAdapter, "send_text_then_submit", _ok)

    result = await main._tmux_send_payload_then_submit(
        "%9",
        '$golden-throne-sop victory condition "needs tests passing" is unmet',
        enable_skill_sink=True,
    )

    assert result["returncode"] == 0
    assert seen["kwargs"]["pre_submit_keys"] == ("Tab",)


async def test_send_payload_does_not_tab_dollar_text_without_skill_sink(
    app_env: Any, monkeypatch: Any
) -> None:
    main = app_env.main
    import tmuxctl.tmux_adapter as ta

    seen: dict[str, Any] = {}

    def _ok(self, target, text, **kwargs):
        seen["kwargs"] = kwargs
        return None

    monkeypatch.setattr(ta.TmuxAdapter, "send_text_then_submit", _ok)

    result = await main._tmux_send_payload_then_submit("%9", "$HOME is not a skill")

    assert result["returncode"] == 0
    assert seen["kwargs"]["pre_submit_keys"] == ()


async def test_send_payload_does_not_tab_claude_skill(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    import tmuxctl.tmux_adapter as ta

    seen: dict[str, Any] = {}

    def _ok(self, target, text, **kwargs):
        seen["kwargs"] = kwargs
        return None

    monkeypatch.setattr(ta.TmuxAdapter, "send_text_then_submit", _ok)

    result = await main._tmux_send_payload_then_submit("%9", "/golden-throne-sop needs x")

    assert result["returncode"] == 0
    assert seen["kwargs"]["pre_submit_keys"] == ()


async def test_queue_enables_skill_sink_only_for_codex_gt(app_env: Any, monkeypatch: Any) -> None:
    main = app_env.main
    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _no_pending_input)
    seen: dict[str, Any] = {}

    async def _ok(pane, payload, **kwargs):
        seen["pane"] = pane
        seen["payload"] = payload
        seen["kwargs"] = kwargs
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "gated": False,
            "verification_status": "unverified",
            "verified_by": None,
        }

    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _ok)
    with sqlite3.connect(app_env.db_path) as conn:
        conn.execute(
            """INSERT INTO claude_instances
               (id, session_id, tab_name, working_dir, origin_type, device_id,
                status, instance_type, engine, tmux_pane)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "gt-codex-skill",
                "gt-codex-skill",
                "GT Codex Skill",
                "/tmp",
                "local",
                "Mac-Mini",
                "idle",
                "golden_throne",
                "codex",
                "%9",
            ),
        )

    queued = await main.enqueue_pane_write(
        instance_id="gt-codex-skill",
        tmux_pane="%9",
        source="golden_throne",
        purpose="followup",
        payload='$golden-throne-sop victory condition "needs tests passing" is unmet',
    )
    results = await main.process_pane_write_queue_once(queued["id"])

    assert results[0]["status"] == main.PANE_WRITE_SENT
    assert seen["kwargs"]["enable_skill_sink"] is True


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


# ---- Tier 2(b): resolve instance_id -> pane LIVE at dequeue, fail closed ------


async def test_dequeue_sends_to_live_resolved_pane_not_stored_column(
    app_env: Any, monkeypatch: Any
) -> None:
    """The drain delivers to the pane resolved live by instance_id, never the
    stored column. A row enqueued with a now-stale ``%999`` must be sent to the
    live-resolved ``%77`` (pane moved/reused since enqueue)."""
    main = app_env.main
    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _no_pending_input)

    async def _resolve_live(_instance_id):
        return ("%77", "palace:N")

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve_live)

    seen: dict[str, Any] = {}

    async def _ok(pane, payload, *, clear_prompt=False):
        seen["pane"] = pane
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "gated": False,
            "verification_status": "unverified",
            "verified_by": None,
        }

    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _ok)

    queued = await main.enqueue_pane_write(
        instance_id="inst-moved",
        tmux_pane="%999",  # deliberately stale stored value
        source="enforcement",
        purpose="nudge",
        payload="resume",
    )
    results = await main.process_pane_write_queue_once(queued["id"])

    assert len(results) == 1
    assert results[0]["status"] == main.PANE_WRITE_SENT
    assert seen["pane"] == "%77", "must send to the live-resolved pane, not stored %999"
    assert results[0]["tmux_pane"] == "%77"
    assert results[0]["stored_pane"] == "%999"
    assert _fetch_status(app_env.db_path, queued["id"]) == "sent"


async def test_dequeue_fails_closed_when_pane_unresolved(app_env: Any, monkeypatch: Any) -> None:
    """If the instance's pane no longer resolves, the item is cancelled (terminal)
    and nothing is sent — no delivery to a vanished/reused pane."""
    main = app_env.main
    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _no_pending_input)

    async def _gone(_instance_id):
        return (None, None)

    monkeypatch.setattr(main.shared, "resolve_instance_pane", _gone)

    sent: list[Any] = []

    async def _send(pane, payload, *, clear_prompt=False):
        sent.append(pane)
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "gated": False,
            "verification_status": "unverified",
            "verified_by": None,
        }

    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _send)

    queued = await main.enqueue_pane_write(
        instance_id="inst-gone",
        tmux_pane="%999",
        source="enforcement",
        purpose="nudge",
        payload="resume",
    )
    results = await main.process_pane_write_queue_once(queued["id"])

    assert len(results) == 1
    assert results[0]["status"] == main.PANE_WRITE_CANCELLED
    assert results[0]["reason"] == "pane_unresolved"
    assert sent == [], "a vanished pane must not receive a send"
    assert _fetch_status(app_env.db_path, queued["id"]) == "cancelled"
