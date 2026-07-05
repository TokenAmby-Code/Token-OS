from __future__ import annotations

import pytest
from tmuxctl import api
from tmuxctl.service import TmuxControlPlane


def test_fetch_session_doc_for_instance_uses_fk_not_pane_label(monkeypatch):
    calls: list[str] = []

    def fake_get(path: str):
        calls.append(path)
        if path == "/api/instances/inst-1":
            return {
                "id": "inst-1",
                "status": "working",
                "session_doc_id": 42,
                # Old failure mode: this field can be null/wrong and must not matter.
                "pane_label": None,
            }
        if path == "/api/session-docs/42":
            return {"id": 42, "title": "Needs Session Name", "file_path": "/tmp/doc.md"}
        raise AssertionError(path)

    monkeypatch.setattr(api, "_api_get_json", fake_get)

    doc = api.fetch_session_doc_for_instance_id("inst-1", pane_label="codex-undercount")

    assert doc["id"] == 42
    assert doc["instance_id"] == "inst-1"
    assert doc["pane_label"] == "codex-undercount"
    assert "/api/instances?status=processing&sort=recent_activity" not in calls


def test_fetch_session_doc_for_instance_reports_no_doc_bound(monkeypatch):
    monkeypatch.setattr(
        api,
        "_api_get_json",
        lambda path: {"id": "inst-1", "status": "working", "session_doc_id": None},
    )

    with pytest.raises(api.SessionDocResolutionError) as exc:
        api.fetch_session_doc_for_instance_id("inst-1")

    assert exc.value.reason == "no_doc_bound"
    assert "no_doc_bound" in str(exc.value)


def test_service_session_doc_for_pane_uses_stamp_instance_id(monkeypatch):
    control = TmuxControlPlane(adapter=None)
    monkeypatch.setattr(
        control,
        "instance_id_for_pane",
        lambda pane: {"found": True, "pane": "palace:N", "instance_id": "inst-1"},
    )

    seen = {}

    def fake_fetch(instance_id: str, *, pane_label: str = ""):
        seen["instance_id"] = instance_id
        seen["pane_label"] = pane_label
        return {"id": 7, "title": "Doc", "instance_id": instance_id, "pane_label": pane_label}

    monkeypatch.setattr("tmuxctl.service.fetch_session_doc_for_instance_id", fake_fetch)

    assert control.session_doc_for_pane("%99")["id"] == 7
    assert seen == {"instance_id": "inst-1", "pane_label": "palace:N"}
