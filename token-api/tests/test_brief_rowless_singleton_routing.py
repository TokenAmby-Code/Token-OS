from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_brief_rowless_live_codex_singleton_uses_tmuxctl_fallback(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No registry row + live Codex pane must still deliver via tmuxctl."""
    main = app_env.main
    target = {
        "pane_id": "%44",
        "position_id": "mechanicus:fabricator-general",
        "source": "pane",
        "spec": "mechanicus:fabricator-general",
    }

    async def _targets(**_kwargs):
        return [target], []

    async def _no_row(_pane):
        return None

    async def _resolve_live(pane):
        return "%44" if pane == "%44" else None

    async def _pane_rows():
        return [("%44", "codex", "/tmp/project", "mechanicus", "/dev/ttys044")]

    sent = []

    async def _send(pane, payload, *, clear_prompt=False, enable_skill_sink=False, **_kwargs):
        sent.append((pane, payload, clear_prompt))
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "operation": "tmuxctl.send_text_then_submit",
            "gated": False,
            "verification_status": "submitted",
            "verified_by": "composer_cleared",
        }

    monkeypatch.setattr(main.talk_service, "resolve_brief_targets", _targets)
    monkeypatch.setattr(main.talk_service, "lookup_instance_for_pane", _no_row)
    monkeypatch.setattr(main.shared, "resolve_tmux_pane_id", _resolve_live)
    monkeypatch.setattr(main, "_tmux_pane_rows", _pane_rows)
    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _send)

    result = await main.brief_send(
        main.BriefSendRequest(
            caller_pane="council:custodes",
            panes=["mechanicus:fabricator-general"],
            payload="PR-B probe",
        )
    )

    assert result["status"] == "ok"
    assert result["delivered"] == 1
    assert sent == [("%44", "PR-B probe", True)]
    receipt = result["resolved"][0]
    assert receipt["status"] == main.PANE_WRITE_SENT
    assert receipt["fallback"] == "tmuxctl_send_text_no_registry_row"
    assert receipt["operation"] == "tmuxctl.send_text_then_submit"
    assert receipt["position_id"] == "mechanicus:fabricator-general"


@pytest.mark.asyncio
async def test_brief_rowless_dead_pane_reports_no_delivery(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No registry row + no live pane/process is a real miss, not a success."""
    main = app_env.main
    target = {
        "pane_id": "%45",
        "position_id": "mechanicus:administratum",
        "source": "pane",
        "spec": "mechanicus:administratum",
    }

    async def _targets(**_kwargs):
        return [target], []

    async def _no_row(_pane):
        return None

    async def _resolve_dead(_pane):
        return None

    sent = []

    async def _send(*args, **kwargs):  # pragma: no cover - must not be called
        sent.append((args, kwargs))
        raise AssertionError("dead rowless pane must not receive bytes")

    monkeypatch.setattr(main.talk_service, "resolve_brief_targets", _targets)
    monkeypatch.setattr(main.talk_service, "lookup_instance_for_pane", _no_row)
    monkeypatch.setattr(main.shared, "resolve_tmux_pane_id", _resolve_dead)
    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _send)

    result = await main.brief_send(
        main.BriefSendRequest(panes=["mechanicus:administratum"], payload="probe")
    )

    assert result["status"] == "failed"
    assert result["delivered"] == 0
    assert sent == []
    assert result["resolved"][0]["status"] == main.PANE_WRITE_CANCELLED
    assert result["resolved"][0]["reason"] == "pane_unresolved"


@pytest.mark.asyncio
async def test_brief_registry_row_path_still_uses_queue(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target with a registry row keeps the existing queued registry path."""
    main = app_env.main
    target = {
        "pane_id": "%46",
        "position_id": "council:custodes",
        "source": "pane",
        "spec": "council:custodes",
    }

    async def _targets(**_kwargs):
        return [target], []

    async def _row(_pane):
        return {"id": "custodes-row", "engine": "claude"}

    queued_ids = []

    async def _enqueue(**kwargs):
        queued_ids.append(kwargs)
        return {"id": "queue-1", "status": main.PANE_WRITE_PENDING}

    async def _drain(queue_id):
        assert queue_id == "queue-1"
        return [{"queue_id": queue_id, "status": main.PANE_WRITE_SENT, "tmux_pane": "%46"}]

    async def _direct(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("registry-row target must not use rowless fallback")

    monkeypatch.setattr(main.talk_service, "resolve_brief_targets", _targets)
    monkeypatch.setattr(main.talk_service, "lookup_instance_for_pane", _row)
    monkeypatch.setattr(main, "enqueue_pane_write", _enqueue)
    monkeypatch.setattr(main, "process_pane_write_queue_once", _drain)
    monkeypatch.setattr(main, "_direct_tmux_pane_delivery", _direct)

    result = await main.brief_send(
        main.BriefSendRequest(panes=["council:custodes"], payload="row path")
    )

    # Idempotency-by-default: a keyless brief still derives a deterministic
    # operation_id from (pane, payload) so blind retries dedupe on the queue.
    expected_operation_id = main._scoped_send_operation_id(
        "brief",
        f"auto:%46:{main._prompt_payload_hash('row path')}",
        "%46",
        "row path",
    )
    assert expected_operation_id is not None
    assert result["delivered"] == 1
    assert queued_ids == [
        {
            "instance_id": "%46",
            "tmux_pane": "%46",
            "source": "brief",
            "purpose": "brief_send",
            "payload": "row path",
            "hook_driven": True,
            "operation_id": expected_operation_id,
        }
    ]
    assert "fallback" not in result["resolved"][0]


@pytest.mark.asyncio
async def test_brief_explicit_idempotency_key_reuses_queue_id(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Brief retries with the same explicit key are per-id, not blind re-sends."""
    main = app_env.main
    target = {
        "pane_id": "%46",
        "position_id": "council:custodes",
        "source": "pane",
        "spec": "council:custodes",
    }

    async def _targets(**_kwargs):
        return [target], []

    async def _row(_pane):
        return {"id": "custodes-row", "engine": "claude"}

    async def _resolve_instance_pane(_instance_id):
        return None, ""

    async def _resolve_tmux_pane_id(pane):
        return "%46" if pane == "%46" else None

    async def _no_pending(_pane):
        return False

    physical_sends: list[tuple[str, str, str | None]] = []

    async def _send(pane, payload, *, clear_prompt=False, operation_id=None, **_kwargs):
        physical_sends.append((pane, payload, operation_id))
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "gated": False,
            "verification_status": "submitted",
            "verified_by": "UserPromptSubmit",
            "operation_id": operation_id,
        }

    hook_flags: list[tuple[str, str]] = []

    async def _flag(instance_id, *, tmux_pane, actor):
        hook_flags.append((instance_id, actor))

    monkeypatch.setattr(main.talk_service, "resolve_brief_targets", _targets)
    monkeypatch.setattr(main.talk_service, "lookup_instance_for_pane", _row)
    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve_instance_pane)
    monkeypatch.setattr(main.shared, "resolve_tmux_pane_id", _resolve_tmux_pane_id)
    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _no_pending)
    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _send)
    monkeypatch.setattr(main, "_flag_hook_driven", _flag)

    req = main.BriefSendRequest(
        panes=["council:custodes"], payload="row path", idempotency_key="same-op"
    )
    first = await main.brief_send(req)
    second = await main.brief_send(req)

    assert first["delivered"] == second["delivered"] == 1
    assert len(physical_sends) == 1
    operation_id = physical_sends[0][2]
    assert operation_id is not None
    assert first["resolved"][0]["queue_id"] == second["resolved"][0]["id"] == operation_id
    # Event-level pause defers the hook_driven effect to release: it still fires
    # exactly once across the idempotent replay, now actored as the release.
    assert hook_flags == [("%46", "release:brief")]
    with sqlite3.connect(main.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, status, payload FROM pane_write_queue WHERE id = ?",
            (operation_id,),
        ).fetchall()
    assert rows == [(operation_id, main.PANE_WRITE_SENT, "row path")]


@pytest.mark.asyncio
async def test_brief_rowless_unverified_issued_bytes_stays_unverified(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-gated rc=0 rowless/codex brief is not enough without submit proof.

    Codex can miss UserPromptSubmit, but bytes-issued alone is still not delivery
    proof. The daemon must provide ``submitted`` or engine-specific ``likely``;
    otherwise brief reports UNVERIFIED so callers do not blind-retry.
    """
    main = app_env.main
    target = {
        "pane_id": "%44",
        "position_id": "council:pax",
        "source": "pane",
        "spec": "council:pax",
    }

    async def _targets(**_kwargs):
        return [target], []

    async def _no_row(_pane):
        return None

    async def _resolve_live(pane):
        return "%44" if pane == "%44" else None

    async def _codex_engine(_pane):
        return "codex"

    async def _sender_is_custodes(_pane):
        return True

    sent = []

    async def _send(pane, payload, *, clear_prompt=False, operation_id=None, **_kwargs):
        sent.append((pane, payload, clear_prompt, operation_id))
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "operation": "tmuxctl.send_text_then_submit",
            "gated": False,
            # Codex pane: bytes issued, but no UserPromptSubmit ack in the window.
            "verification_status": "unverified",
            "verified_by": None,
            "operation_id": operation_id,
        }

    monkeypatch.setattr(main.talk_service, "resolve_brief_targets", _targets)
    monkeypatch.setattr(main.talk_service, "lookup_instance_for_pane", _no_row)
    monkeypatch.setattr(main, "_pane_sender_is_custodes", _sender_is_custodes)
    monkeypatch.setattr(main.shared, "resolve_tmux_pane_id", _resolve_live)
    monkeypatch.setattr(main, "_pane_live_agent_engine", _codex_engine)
    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _send)

    result = await main.brief_send(
        main.BriefSendRequest(
            caller_pane="council:custodes",
            panes=["council:pax"],
            payload="codex probe",
        )
    )

    # The keyless brief still drives a deterministic auto operation_id through the
    # rowless/codex direct path so a blind retry would dedupe, but unverified bytes
    # are not promoted to delivered.
    expected_operation_id = main._scoped_send_operation_id(
        "brief",
        f"auto:%44:{main._prompt_payload_hash('codex probe')}",
        "%44",
        "codex probe",
    )
    assert expected_operation_id is not None
    assert result["status"] == "unverified"
    assert result["delivered"] == 0
    assert sent == [("%44", "codex probe", True, expected_operation_id)]
    receipt = result["resolved"][0]
    assert receipt["status"] == main.PANE_WRITE_UNVERIFIED
    assert receipt["fallback"] == "tmuxctl_send_text_no_registry_row"
    assert "submit_unverified" in receipt["reason"]


@pytest.mark.asyncio
async def test_brief_rowless_codex_likely_capture_reports_sent(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex rowless brief may report SENT when tmuxctld proves ingestion."""
    main = app_env.main
    target = {
        "pane_id": "%45",
        "position_id": "council:pax",
        "source": "pane",
        "spec": "council:pax",
    }

    async def _targets(**_kwargs):
        return [target], []

    async def _no_row(_pane):
        return None

    async def _resolve_live(pane):
        return "%45" if pane == "%45" else None

    async def _codex_engine(_pane):
        return "codex"

    async def _sender_is_custodes(_pane):
        return True

    async def _send(pane, payload, *, clear_prompt=False, operation_id=None, **_kwargs):
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "operation": "tmuxctld.send_text",
            "gated": False,
            "verification_status": "likely",
            "verified_by": "capture-pane:codex-user-message",
            "delivery": "likely",
            "operation_id": operation_id,
        }

    monkeypatch.setattr(main.talk_service, "resolve_brief_targets", _targets)
    monkeypatch.setattr(main.talk_service, "lookup_instance_for_pane", _no_row)
    monkeypatch.setattr(main, "_pane_sender_is_custodes", _sender_is_custodes)
    monkeypatch.setattr(main.shared, "resolve_tmux_pane_id", _resolve_live)
    monkeypatch.setattr(main, "_pane_live_agent_engine", _codex_engine)
    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _send)

    result = await main.brief_send(
        main.BriefSendRequest(
            caller_pane="council:custodes",
            panes=["council:pax"],
            payload="codex likely probe",
        )
    )

    assert result["status"] == "ok"
    assert result["delivered"] == 1
    receipt = result["resolved"][0]
    assert receipt["status"] == main.PANE_WRITE_SENT
    assert receipt["verification_status"] == "likely"
    assert receipt["verified_by"] == "capture-pane:codex-user-message"


@pytest.mark.asyncio
async def test_brief_rowless_claude_unverified_is_not_marked_sent(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rowless claude pane that issues bytes without an ack is unverified.

    The transport may have issued bytes, but without submit proof it must not be
    reported SENT. The top-level brief status stays UNVERIFIED so callers get the
    do-not-blind-retry state rather than a generic failure.
    """
    main = app_env.main
    target = {
        "pane_id": "%47",
        "position_id": "council:pax",
        "source": "pane",
        "spec": "council:pax",
    }

    async def _targets(**_kwargs):
        return [target], []

    async def _no_row(_pane):
        return None

    async def _resolve_live(pane):
        return "%47" if pane == "%47" else None

    async def _claude_engine(_pane):
        return "claude"

    async def _sender_is_custodes(_pane):
        return True

    async def _send(pane, payload, *, clear_prompt=False, operation_id=None, **_kwargs):
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "operation": "tmuxctl.send_text_then_submit",
            "gated": False,
            "verification_status": "unverified",
            "verified_by": None,
            "operation_id": operation_id,
        }

    monkeypatch.setattr(main.talk_service, "resolve_brief_targets", _targets)
    monkeypatch.setattr(main.talk_service, "lookup_instance_for_pane", _no_row)
    monkeypatch.setattr(main, "_pane_sender_is_custodes", _sender_is_custodes)
    monkeypatch.setattr(main.shared, "resolve_tmux_pane_id", _resolve_live)
    monkeypatch.setattr(main, "_pane_live_agent_engine", _claude_engine)
    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _send)

    result = await main.brief_send(
        main.BriefSendRequest(
            caller_pane="council:custodes",
            panes=["council:pax"],
            payload="claude probe",
        )
    )

    assert result["status"] == "unverified"
    assert result["delivered"] == 0
    receipt = result["resolved"][0]
    assert receipt["status"] == main.PANE_WRITE_UNVERIFIED


@pytest.mark.asyncio
async def test_brief_keyless_identical_sends_dedupe_to_one_delivery(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two identical keyless briefs dedupe to a single physical delivery.

    Idempotency-by-default: with no explicit key, brief derives a deterministic
    operation_id from (pane, payload) so a blind retry collapses onto the same
    queue row instead of double-firing (issue #480 lineage).
    """
    main = app_env.main
    target = {
        "pane_id": "%46",
        "position_id": "council:custodes",
        "source": "pane",
        "spec": "council:custodes",
    }

    async def _targets(**_kwargs):
        return [target], []

    async def _row(_pane):
        return {"id": "custodes-row", "engine": "claude"}

    async def _resolve_instance_pane(_instance_id):
        return None, ""

    async def _resolve_tmux_pane_id(pane):
        return "%46" if pane == "%46" else None

    async def _no_pending(_pane):
        return False

    physical_sends: list[tuple[str, str, str | None]] = []

    async def _send(pane, payload, *, clear_prompt=False, operation_id=None, **_kwargs):
        physical_sends.append((pane, payload, operation_id))
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "gated": False,
            "verification_status": "submitted",
            "verified_by": "UserPromptSubmit",
            "operation_id": operation_id,
        }

    async def _flag(instance_id, *, tmux_pane, actor):
        return None

    monkeypatch.setattr(main.talk_service, "resolve_brief_targets", _targets)
    monkeypatch.setattr(main.talk_service, "lookup_instance_for_pane", _row)
    monkeypatch.setattr(main.shared, "resolve_instance_pane", _resolve_instance_pane)
    monkeypatch.setattr(main.shared, "resolve_tmux_pane_id", _resolve_tmux_pane_id)
    monkeypatch.setattr(main, "_tmux_pane_has_pending_input", _no_pending)
    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _send)
    monkeypatch.setattr(main, "_flag_hook_driven", _flag)

    # No idempotency_key supplied: dedup must engage by default.
    req = main.BriefSendRequest(panes=["council:custodes"], payload="keyless dedupe")
    first = await main.brief_send(req)
    second = await main.brief_send(req)

    expected_operation_id = main._scoped_send_operation_id(
        "brief",
        f"auto:%46:{main._prompt_payload_hash('keyless dedupe')}",
        "%46",
        "keyless dedupe",
    )
    assert first["delivered"] == second["delivered"] == 1
    assert len(physical_sends) == 1
    assert physical_sends[0][2] == expected_operation_id
    assert first["resolved"][0]["queue_id"] == second["resolved"][0]["id"] == expected_operation_id
    with sqlite3.connect(main.DB_PATH) as conn:
        rows = conn.execute(
            "SELECT id, status, payload FROM pane_write_queue WHERE id = ?",
            (expected_operation_id,),
        ).fetchall()
    assert rows == [(expected_operation_id, main.PANE_WRITE_SENT, "keyless dedupe")]


@pytest.mark.asyncio
async def test_talk_rowless_live_codex_singleton_requires_verified_submit(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """talk returns no talk_id when rowless delivery is bytes-issued but unverified."""
    main = app_env.main

    async def _resolve(spec):
        return {"council:custodes": "%10", "mechanicus:fabricator-general": "%44"}.get(spec)

    async def _no_return(**_kwargs):
        return None

    async def _no_row(_pane):
        return None

    async def _sender_is_custodes(_pane):
        return True

    async def _resolve_live(pane):
        return pane if pane in {"%10", "%44"} else None

    async def _pane_rows():
        return [("%44", "codex", "/tmp/project", "mechanicus", "/dev/ttys044")]

    sent = []

    async def _send(pane, payload, *, clear_prompt=False, enable_skill_sink=False, **_kwargs):
        sent.append((pane, payload, clear_prompt))
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "operation": "tmuxctl.send_text_then_submit",
            "gated": False,
            "verification_status": "unverified",
            "verified_by": None,
        }

    monkeypatch.setattr(main.talk_service, "resolve_pane", _resolve)
    monkeypatch.setattr(main.talk_service, "return_talk", _no_return)
    monkeypatch.setattr(main.talk_service, "lookup_instance_for_pane", _no_row)
    monkeypatch.setattr(main, "_pane_sender_is_custodes", _sender_is_custodes)
    monkeypatch.setattr(main.shared, "resolve_tmux_pane_id", _resolve_live)
    monkeypatch.setattr(main, "_tmux_pane_rows", _pane_rows)
    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _send)

    with pytest.raises(main.HTTPException) as excinfo:
        await main.talk_send(
            main.TalkSendRequest(
                caller_pane="council:custodes",
                target_pane="mechanicus:fabricator-general",
                payload="talk probe",
            )
        )

    assert excinfo.value.status_code == 502
    assert "submit_unverified" in str(excinfo.value.detail)
    assert sent == [("%44", "talk probe", False)]


@pytest.mark.asyncio
async def test_talk_rowless_live_codex_accepts_likely_ingestion(
    app_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """talk into rowless Codex opens when tmuxctld proves real ingestion."""
    main = app_env.main

    async def _resolve(spec):
        return {"council:custodes": "%10", "mechanicus:fabricator-general": "%44"}.get(spec)

    async def _no_return(**_kwargs):
        return None

    async def _no_row(_pane):
        return None

    async def _sender_is_custodes(_pane):
        return True

    async def _resolve_live(pane):
        return pane if pane in {"%10", "%44"} else None

    async def _pane_rows():
        return [("%44", "codex", "/tmp/project", "mechanicus", "/dev/ttys044")]

    async def _send(pane, payload, *, clear_prompt=False, enable_skill_sink=False, **_kwargs):
        return {
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "operation": "tmuxctld.send_text",
            "gated": False,
            "verification_status": "likely",
            "verified_by": "capture-pane:codex-user-message",
            "delivery": "likely",
        }

    monkeypatch.setattr(main.talk_service, "resolve_pane", _resolve)
    monkeypatch.setattr(main.talk_service, "return_talk", _no_return)
    monkeypatch.setattr(main.talk_service, "lookup_instance_for_pane", _no_row)
    monkeypatch.setattr(main, "_pane_sender_is_custodes", _sender_is_custodes)
    monkeypatch.setattr(main.shared, "resolve_tmux_pane_id", _resolve_live)
    monkeypatch.setattr(main, "_tmux_pane_rows", _pane_rows)
    monkeypatch.setattr(main, "_tmux_send_payload_then_submit", _send)

    result = await main.talk_send(
        main.TalkSendRequest(
            caller_pane="council:custodes",
            target_pane="mechanicus:fabricator-general",
            payload="talk likely probe",
        )
    )

    assert result["status"] == "open"
    assert result["delivery"]["status"] == main.PANE_WRITE_SENT
    assert result["delivery"]["verification_status"] == "likely"


@pytest.mark.asyncio
async def test_talk_resolve_pane_accepts_unique_label_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persona shorthand like `pax` resolves when it uniquely matches a pane label."""
    import talk

    async def _panes():
        return [
            {
                "pane_id": "%10",
                "position_id": "council:pax",
                "session": "main",
                "window_index": "1",
                "window_name": "council",
            },
            {
                "pane_id": "%44",
                "position_id": "mechanicus:orchestrator",
                "session": "main",
                "window_index": "4",
                "window_name": "mechanicus",
            },
        ]

    monkeypatch.setattr(talk, "_tmux_list_panes", _panes)

    assert await talk.resolve_pane("pax") == "%10"
    assert await talk.resolve_pane("orchestrator") == "%44"
