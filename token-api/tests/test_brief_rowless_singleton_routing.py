from __future__ import annotations

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

    assert result["delivered"] == 1
    assert queued_ids == [
        {
            "instance_id": "%46",
            "tmux_pane": "%46",
            "source": "brief",
            "purpose": "brief_send",
            "payload": "row path",
            "hook_driven": True,
            "operation_id": None,
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

    queued_ids: list[str | None] = []

    async def _enqueue(**kwargs):
        queued_ids.append(kwargs.get("operation_id"))
        return {"id": kwargs.get("operation_id"), "status": main.PANE_WRITE_PENDING}

    async def _drain(queue_id):
        return [{"queue_id": queue_id, "status": main.PANE_WRITE_SENT, "tmux_pane": "%46"}]

    monkeypatch.setattr(main.talk_service, "resolve_brief_targets", _targets)
    monkeypatch.setattr(main.talk_service, "lookup_instance_for_pane", _row)
    monkeypatch.setattr(main, "enqueue_pane_write", _enqueue)
    monkeypatch.setattr(main, "process_pane_write_queue_once", _drain)

    req = main.BriefSendRequest(
        panes=["council:custodes"], payload="row path", idempotency_key="same-op"
    )
    first = await main.brief_send(req)
    second = await main.brief_send(req)

    assert first["delivered"] == second["delivered"] == 1
    assert len(queued_ids) == 2
    assert queued_ids[0] == queued_ids[1]
    assert queued_ids[0] is not None


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
