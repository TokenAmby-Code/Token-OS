#!/usr/bin/env python3
"""Regression validation for the brief/talk transport-severance verdict.

Standalone assert harness (NOT pytest — pytest was excised repo-wide by Emperor
ruling #703). Run directly:

    cd token-api && uv run python validate_delivery_severance.py

Repro class (dispatch-comms-failure-ledger `## 2026-07-14 13:3x`): a synchronous
Token-API->tmuxctld round-trip severs (read-timeout / connection reset / non-200
/ malformed read; ``shared._tmuxctld_post_json`` -> None) on a ``/send-text`` the
daemon actually COMPLETED server-side. The retired-410 fallback fabricated a
POSITIVE ``not_delivered`` verdict from that transport-negative, which tripped a
resend -> proven duplicate delivery. The fix maps a severance to an INDETERMINATE
``delivery_unknown`` verdict carrying the correlation handle — never ``sent``,
never ``failed``.

Covers:
1. ``_pane_send_terminal_status`` maps the severed shape to ``delivery_unknown``.
2. ``send_prompt_to_pane`` returns the indeterminate/do-not-retry shape (with the
   correlation id) when the daemon post severs — NOT the retired-410 failure.
3. ``_direct_tmux_pane_delivery`` (rowless brief/talk path) surfaces
   ``delivery_unknown`` end-to-end.
4. ``brief_send`` rolls the severed row up to ``brief_status="unknown"`` with an
   honest ``delivered=0`` — never ``failed``.
5. Loud paths preserved: happy delivery still maps to ``sent``; a genuine daemon
   drop (``delivered:False`` rc0, not severed) still maps to ``failed``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import main  # noqa: E402
import shared  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    marker = "PASS" if condition else "FAIL"
    print(f"[{marker}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


def run(coro):
    return asyncio.run(coro)


# --- Monkeypatch scaffolding (no live tmux / DB / socket) -------------------


def sever_transport() -> None:
    """Force every daemon post to sever (return None)."""
    shared._tmuxctld_post_json = lambda *a, **k: None  # type: ignore[assignment]


def restore_transport(fn) -> None:
    shared._tmuxctld_post_json = fn  # type: ignore[assignment]


async def _fake_engine(_pane):
    return "claude"


async def _fake_resolve_pane_id(pane):
    return pane


# --- Test 1: pure terminal-status mapping -----------------------------------


def test_terminal_status_mapping() -> None:
    severed = {
        "returncode": 0,
        "transport": "severed",
        "delivery": "unknown",
        "verification_status": "unknown",
        "delivered": None,
        "reason": "transport_severed",
    }
    status, _reason = main._pane_send_terminal_status(severed)
    check("severed shape -> delivery_unknown", status == main.PANE_WRITE_UNKNOWN, status)

    happy = {"returncode": 0, "delivered": True, "verification_status": "submitted"}
    status, _ = main._pane_send_terminal_status(happy)
    check("happy delivery -> sent", status == main.PANE_WRITE_SENT, status)

    # Genuine daemon drop: bytes suppressed (delivered False, rc0), NOT severed.
    drop = {"returncode": 0, "delivered": False, "status": "dropped"}
    status, reason = main._pane_send_terminal_status(drop)
    check(
        "genuine drop -> failed (loud path preserved)",
        status == main.PANE_WRITE_FAILED and "not_delivered" in str(reason or ""),
        f"{status}/{reason}",
    )


# --- Test 2: send_prompt_to_pane severance ----------------------------------


def test_send_prompt_severance() -> None:
    orig = shared._tmuxctld_post_json
    sever_transport()
    try:
        result = run(
            main.send_prompt_to_pane(
                "%1",
                "hello council",
                operation_id="op-1",
                correlation_id="brief:5f616f9d",
            )
        )
    finally:
        restore_transport(orig)
    check(
        "severed send: returncode 0 (not 410)",
        result.get("returncode") == 0,
        result.get("returncode"),
    )
    check(
        "severed send: transport=severed",
        result.get("transport") == "severed",
        result.get("transport"),
    )
    check(
        "severed send: delivery unknown",
        result.get("delivery") == "unknown",
        result.get("delivery"),
    )
    check(
        "severed send: delivered is None (not False)",
        result.get("delivered") is None,
        result.get("delivered"),
    )
    check(
        "severed send: do_not_retry", result.get("do_not_retry") is True, result.get("do_not_retry")
    )
    check(
        "severed send: carries correlation handle",
        result.get("correlation_id") == "brief:5f616f9d",
        result.get("correlation_id"),
    )
    status, _ = main._pane_send_terminal_status(result)
    check("severed send maps to delivery_unknown", status == main.PANE_WRITE_UNKNOWN, status)


# --- Test 3: _direct_tmux_pane_delivery (rowless path) ----------------------


def test_direct_delivery_severance() -> None:
    orig_post = shared._tmuxctld_post_json
    orig_resolve = shared.resolve_tmux_pane_id
    orig_engine = main._pane_live_agent_engine
    sever_transport()
    shared.resolve_tmux_pane_id = _fake_resolve_pane_id  # type: ignore[assignment]
    main._pane_live_agent_engine = _fake_engine  # type: ignore[assignment]
    try:
        result = run(
            main._direct_tmux_pane_delivery(
                "%1",
                "hello council",
                source="brief",
                purpose="brief_send",
                clear_prompt=True,
                operation_id="op-2",
                correlation_id="brief:bd53aa1f",
            )
        )
    finally:
        restore_transport(orig_post)
        shared.resolve_tmux_pane_id = orig_resolve  # type: ignore[assignment]
        main._pane_live_agent_engine = orig_engine  # type: ignore[assignment]
    check(
        "direct delivery severed -> delivery_unknown",
        result.get("status") == main.PANE_WRITE_UNKNOWN,
        result.get("status"),
    )
    check(
        "direct delivery keeps correlation handle",
        result.get("correlation_id") == "brief:bd53aa1f",
        result.get("correlation_id"),
    )


# --- Test 4: brief_send rollup ----------------------------------------------


def test_brief_send_rollup() -> None:
    orig_post = shared._tmuxctld_post_json
    orig_resolve = shared.resolve_tmux_pane_id
    orig_engine = main._pane_live_agent_engine
    orig_targets = main.talk_service.resolve_brief_targets
    orig_lookup = main.talk_service.lookup_instance_for_pane
    orig_publicize = main.talk_service.publicize_payload
    orig_custodes = main._pane_sender_is_custodes

    async def fake_targets(*, panes, pages):
        return ([{"pane_id": "%1", "spec": "council:pax", "source": "pane"}], [])

    async def fake_lookup(_pane):
        return None  # rowless -> _direct_tmux_pane_delivery

    async def fake_publicize(payload):
        return payload

    async def fake_custodes(_caller):
        return True  # sender is Custodes -> no hook_driven flag write

    sever_transport()
    shared.resolve_tmux_pane_id = _fake_resolve_pane_id  # type: ignore[assignment]
    main._pane_live_agent_engine = _fake_engine  # type: ignore[assignment]
    main.talk_service.resolve_brief_targets = fake_targets  # type: ignore[assignment]
    main.talk_service.lookup_instance_for_pane = fake_lookup  # type: ignore[assignment]
    main.talk_service.publicize_payload = fake_publicize  # type: ignore[assignment]
    main._pane_sender_is_custodes = fake_custodes  # type: ignore[assignment]
    try:
        request = main.BriefSendRequest(
            caller_pane=None,
            panes=["council:pax"],
            pages=[],
            payload="hello council",
            ephemeral=False,
            idempotency_key=None,
        )
        result = run(main.brief_send(request))
    finally:
        restore_transport(orig_post)
        shared.resolve_tmux_pane_id = orig_resolve  # type: ignore[assignment]
        main._pane_live_agent_engine = orig_engine  # type: ignore[assignment]
        main.talk_service.resolve_brief_targets = orig_targets  # type: ignore[assignment]
        main.talk_service.lookup_instance_for_pane = orig_lookup  # type: ignore[assignment]
        main.talk_service.publicize_payload = orig_publicize  # type: ignore[assignment]
        main._pane_sender_is_custodes = orig_custodes  # type: ignore[assignment]

    check(
        "brief_send status=unknown (not failed)",
        result.get("status") == "unknown",
        result.get("status"),
    )
    check("brief_send delivered=0 (honest)", result.get("delivered") == 0, result.get("delivered"))
    resolved = result.get("resolved") or []
    row_status = resolved[0].get("status") if resolved else None
    check(
        "brief_send row status=delivery_unknown", row_status == main.PANE_WRITE_UNKNOWN, row_status
    )


def main_entry() -> int:
    test_terminal_status_mapping()
    test_send_prompt_severance()
    test_direct_delivery_severance()
    test_brief_send_rollup()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): {', '.join(FAILURES)}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main_entry())
